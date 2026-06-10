"""Etherscan V2 — unified EVM block explorer API across ~70 chains.

As of 2024, Etherscan switched to a single V2 API endpoint that covers all
supported chains via a `chainid` query parameter. One API key works for every
chain. This replaces the older per-chain APIs (basescan.org/api,
etherscan.io/api, polygonscan.com/api, …) — those still work but V2 is the
recommended path.

Chain IDs (subset we care about):
  1     ethereum mainnet
  8453  base mainnet
  10    optimism mainnet
  42161 arbitrum one
  137   polygon
  56    bnb chain
  43114 avalanche c-chain
  324   zksync era

Auth: set ETHERSCAN_API_KEY. The legacy BASESCAN_API_KEY is accepted as a
fallback (the V2 key from etherscan.io works for both — we just look at
whichever env is set so existing configs keep working).

Endpoints wrapped (kept tight — etherscan has ~40 actions; the high-value ones
for an agent / research tool are):
- `balance(address, chain)` — native balance (wei)
- `token_transfers(address, chain, ...)` — ERC20 tx history for an EOA
- `contract_source(address, chain)` — verified source / ABI / compiler info
- `contract_abi(address, chain)` — verified ABI only (shorter response)
- `gas_oracle(chain)` — gas-price oracle (safe/propose/fast in gwei + base fee)
- `eth_supply(chain)` — native-token total supply
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.etherscan")

_BASE = "https://api.etherscan.io/v2/api"
_cache = TTLCache()

# Canonical chain names -> chain ids. Accepts both for ergonomics.
CHAIN_IDS: dict[str | int, int] = {
    "ethereum": 1, "eth": 1, "mainnet": 1, 1: 1,
    "base":     8453, 8453: 8453,
    "optimism": 10, "op": 10, 10: 10,
    "arbitrum": 42161, "arb": 42161, 42161: 42161,
    "polygon":  137, 137: 137,
    "bnb":      56, "bsc": 56, "binance": 56, 56: 56,
    "avalanche": 43114, "avax": 43114, 43114: 43114,
    "zksync":   324, 324: 324,
}


def _resolve_chain(chain: str | int) -> int:
    key = chain.lower() if isinstance(chain, str) else chain
    if key in CHAIN_IDS:
        return CHAIN_IDS[key]
    # Numeric string fallback
    try:
        return int(chain)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError(f"unknown chain: {chain!r}") from e


def _api_key() -> str | None:
    return settings.ETHERSCAN_API_KEY or settings.BASESCAN_API_KEY or None


async def _get(params: dict, ttl_s: float | None = None,
               label: str | None = None):
    """Etherscan V2 request. Returns the `result` field on status==1, else None."""
    key = _api_key()
    if not key:
        log.warning("ETHERSCAN_API_KEY (or BASESCAN_API_KEY fallback) not set "
                    "— etherscan.%s skipped", label or params.get("action"))
        return None
    full = dict(params)
    full["apikey"] = key
    ttl = ttl_s if ttl_s is not None else settings.ETHERSCAN_TTL_S
    # Drop apikey from cache key so different env vars sharing the same data
    # path don't fragment the cache.
    cache_key = tuple(sorted((k, v) for k, v in params.items()))
    cached = _cache.get(cache_key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(cache_key):
        cached = _cache.get(cache_key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(_BASE, params=full,
                                    label=label or f"etherscan {params.get('action')}")
        if not isinstance(data, dict):
            return None
        if str(data.get("status")) != "1":
            log.warning("etherscan %s -> non-OK: %s",
                        params.get("action"), data.get("message"))
            return None
        result = data.get("result")
        _cache.put(cache_key, result)
        return result


# ============================================================================
# Public API
# ============================================================================

async def balance(address: str, chain: str | int = "ethereum") -> int | None:
    """Native balance in wei. Returns int or None on failure."""
    r = await _get({
        "chainid": _resolve_chain(chain),
        "module":  "account",
        "action":  "balance",
        "address": address,
        "tag":     "latest",
    }, label=f"balance/{chain}")
    if r is None:
        return None
    try:
        return int(r)
    except (TypeError, ValueError):
        return None


async def token_transfers(address: str, chain: str | int = "ethereum", *,
                          page: int = 1, offset: int = 50,
                          sort: str = "desc") -> list[dict] | None:
    """ERC20 token transfers for an address (both directions)."""
    r = await _get({
        "chainid": _resolve_chain(chain),
        "module":  "account",
        "action":  "tokentx",
        "address": address,
        "page":    max(1, int(page)),
        "offset":  min(max(1, int(offset)), 1000),  # etherscan max 10_000, we cap lower
        "sort":    sort,
    }, label=f"tokentx/{chain}")
    if not isinstance(r, list):
        return None
    out = []
    for t in r:
        out.append({
            # Etherscan sends epoch-seconds strings; normalize to the ISO
            # form every other tckr module returns for `ts`.
            "ts":           _epoch_to_iso(t.get("timeStamp")),
            "hash":         t.get("hash"),
            "from":         t.get("from"),
            "to":           t.get("to"),
            "value":        t.get("value"),
            "token_name":   t.get("tokenName"),
            "token_symbol": t.get("tokenSymbol"),
            "token_decimal": t.get("tokenDecimal"),
            "contract":     t.get("contractAddress"),
            "block":        _to_i(t.get("blockNumber")),
        })
    return out


async def contract_source(address: str, chain: str | int = "ethereum") -> dict | None:
    """Verified-contract source + ABI + compiler. Heavy payload."""
    r = await _get({
        "chainid": _resolve_chain(chain),
        "module":  "contract",
        "action":  "getsourcecode",
        "address": address,
    }, ttl_s=settings.ETHERSCAN_CONTRACT_TTL_S,
        label=f"getsourcecode/{chain}")
    if not isinstance(r, list) or not r:
        return None
    row = r[0] or {}
    return {
        "address":         address,
        "contract_name":   row.get("ContractName"),
        "compiler":        row.get("CompilerVersion"),
        "optimization":    row.get("OptimizationUsed"),
        "runs":            row.get("Runs"),
        "evm_version":     row.get("EVMVersion"),
        "license":         row.get("LicenseType"),
        "proxy":           row.get("Proxy") == "1",
        "implementation":  row.get("Implementation") or None,
        "source_code":     row.get("SourceCode"),
        "abi":             row.get("ABI"),
        "constructor_args": row.get("ConstructorArguments"),
    }


async def contract_abi(address: str, chain: str | int = "ethereum") -> str | None:
    """Just the ABI string (much smaller than `contract_source`)."""
    r = await _get({
        "chainid": _resolve_chain(chain),
        "module":  "contract",
        "action":  "getabi",
        "address": address,
    }, ttl_s=settings.ETHERSCAN_CONTRACT_TTL_S, label=f"getabi/{chain}")
    return r if isinstance(r, str) else None


async def gas_oracle(chain: str | int = "ethereum") -> dict | None:
    """Current gas oracle in gwei: {safe, propose, fast, base_fee, gas_used_ratio}."""
    r = await _get({
        "chainid": _resolve_chain(chain),
        "module":  "gastracker",
        "action":  "gasoracle",
    }, ttl_s=settings.ETHERSCAN_GAS_TTL_S, label=f"gasoracle/{chain}")
    if not isinstance(r, dict):
        return None
    return {
        "safe_gwei":     _to_f(r.get("SafeGasPrice")),
        "propose_gwei":  _to_f(r.get("ProposeGasPrice")),
        "fast_gwei":     _to_f(r.get("FastGasPrice")),
        "base_fee_gwei": _to_f(r.get("suggestBaseFee")),
        "gas_used_ratio": r.get("gasUsedRatio"),
    }


async def eth_supply(chain: str | int = "ethereum") -> int | None:
    """Native-token total supply in wei (or chain-equivalent base unit)."""
    r = await _get({
        "chainid": _resolve_chain(chain),
        "module":  "stats",
        "action":  "ethsupply",
    }, ttl_s=settings.ETHERSCAN_STATS_TTL_S, label=f"ethsupply/{chain}")
    try:
        return int(r) if r is not None else None
    except (TypeError, ValueError):
        return None


def _to_f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_i(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(ts) -> str | None:
    """Epoch-seconds (int or string) → ISO 8601 UTC, None if unparseable."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None
