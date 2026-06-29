"""Birdeye API — Solana-focused token data (and lightweight EVM support).

Where geckoterminal + dexscreener give you pool-level data, Birdeye gives you
token-level analytics: top holders, top trader PnL, holder change rate, and a
Solana-specific security scan. For the new-pair / early-stage trading
dimension on Solana, this is the load-bearing analytics source.

Auth: API key required (`X-API-KEY` header). Free tier supports the endpoints
exposed here at ~30 req/min. Set `BIRDEYE_API_KEY` in env.

Chain parameter is sent via `x-chain` header. Defaults to `solana` since that's
the primary use case; pass `chain="ethereum"` (or base/bsc/arbitrum/optimism/
polygon/avalanche/zksync) for EVM tokens.

Docs: https://docs.birdeye.so/reference
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.birdeye")

_BASE = "https://public-api.birdeye.so"
_cache = TTLCache()

_SUPPORTED_CHAINS = {
    "solana", "ethereum", "base", "bsc", "arbitrum",
    "optimism", "polygon", "avalanche", "zksync",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct100(v) -> float | None:
    """Fraction-of-1 → 0-100 percent."""
    f = _f(v)
    return f * 100.0 if f is not None else None


def _ms_to_iso(ts) -> str | None:
    """Epoch → ISO. Birdeye is inconsistent — some time fields are in seconds,
    others in milliseconds — so detect by magnitude rather than assuming ms:
    a value >= 1e11 can only be milliseconds (1e11 s ≈ year 5138), anything
    smaller is seconds. Prevents ~1970 dates from dividing seconds by 1000."""
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    secs = v / 1000 if v >= 1e11 else v
    try:
        return datetime.fromtimestamp(secs, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _normalize_chain(chain: str) -> str | None:
    key = (chain or "").strip().lower()
    if not key:
        return "solana"
    if key in _SUPPORTED_CHAINS:
        return key
    if key == "eth":
        return "ethereum"
    return None


def _headers(chain: str) -> dict | None:
    if not settings.BIRDEYE_API_KEY:
        log.warning("BIRDEYE_API_KEY not set — birdeye skipped")
        return None
    return {
        "X-API-KEY": settings.BIRDEYE_API_KEY,
        "x-chain": chain,
        "accept": "application/json",
    }


# --------------------------- token overview ---------------------------

async def token_overview(address: str, *, chain: str = "solana") -> dict | None:
    """Price + market cap + liquidity + 24h volume + holder count + creation
    timestamp for one token. Returns:
        {chain, address, symbol, name, decimals, price_usd, liquidity_usd,
         market_cap_usd, fdv_usd, supply, holder_count, num_markets,
         created_at, price_change_pct, volume_usd, ts}
    """
    net = _normalize_chain(chain)
    addr = (address or "").strip()
    if not net or not addr:
        return None
    headers = _headers(net)
    if not headers:
        return None
    ck = ("token_overview", net, addr.lower())

    async def _fetch() -> dict | None:
      body = await _http.get_json(
        f"{_BASE}/defi/token_overview",
        params={"address": addr},
        headers=headers,
        label=f"birdeye token_overview {net}/{addr[:10]}",
      )
      if not isinstance(body, dict) or not body.get("success"):
        return None
      d = body.get("data") or {}
      return {
        "chain": net,
        "address": addr,
        "symbol": d.get("symbol"),
        "name": d.get("name"),
        "decimals": _i(d.get("decimals")),
        "price_usd": _f(d.get("price")),
        "liquidity_usd": _f(d.get("liquidity")),
        "market_cap_usd": _f(d.get("mc")),
        "fdv_usd": _f(d.get("realMc") or d.get("fdv")),
        "supply": _f(d.get("supply")),
        "circulating_supply": _f(d.get("circulatingSupply")),
        "holder_count": _i(d.get("holder")),
        "num_markets": _i(d.get("numberMarkets")),
        "created_at": _ms_to_iso(d.get("createdAt")) if d.get("createdAt") else None,
        "price_change_pct": {
            "1h":  _f(d.get("priceChange1hPercent")),
            "4h":  _f(d.get("priceChange4hPercent")),
            "24h": _f(d.get("priceChange24hPercent")),
        },
        "volume_usd": {
            "1h":  _f(d.get("v1hUSD")),
            "4h":  _f(d.get("v4hUSD")),
            "24h": _f(d.get("v24hUSD")),
        },
        "trade_count": {
            "1h":  _i(d.get("trade1h")),
            "4h":  _i(d.get("trade4h")),
            "24h": _i(d.get("trade24h")),
        },
        "unique_wallet_count_24h": _i(d.get("uniqueWallet24h")),
        "ts": _now_iso(),
      }

    return await _cache.cached(ck, settings.BIRDEYE_TTL_S, _fetch)


# --------------------------- token security (Solana-only) ---------------------------

async def token_security(address: str) -> dict | None:
    """Solana-only security scan. EVM tokens use tckr.goplus instead.

    Returns Birdeye's structured security info including:
        - creator info + creator balance
        - top10 holder percentage
        - mint authority status (renounced or not)
        - freeze authority status
        - mutable metadata flag
        - true_holder vs spammy_wallets count
    """
    addr = (address or "").strip()
    if not addr:
        return None
    headers = _headers("solana")
    if not headers:
        return None
    ck = ("token_security_sol", addr.lower())

    async def _fetch() -> dict | None:
      body = await _http.get_json(
        f"{_BASE}/defi/token_security",
        params={"address": addr},
        headers=headers,
        label=f"birdeye token_security {addr[:10]}",
      )
      if not isinstance(body, dict) or not body.get("success"):
        return None
      d = body.get("data") or {}
      return {
        "chain": "solana",
        "address": addr,
        "creator_address": d.get("creatorAddress"),
        "creator_balance": _f(d.get("creatorBalance")),
        "creator_percent": _f(d.get("creatorPercentage")),
        "creation_time": _ms_to_iso(d.get("creationTime")) if d.get("creationTime") else None,
        "mintable": d.get("mintAuthority") not in (None, "", "null"),
        "freezeable": d.get("freezeAuthority") not in (None, "", "null"),
        "mutable_metadata": bool(d.get("mutableMetadata")),
        # Birdeye reports these as fractions of 1; scale to 0-100 per `_pct`.
        "top10_holder_pct": _pct100(d.get("top10HolderPercent")),
        "top10_user_pct": _pct100(d.get("top10UserPercent")),
        "non_transferable": bool(d.get("nonTransferable")),
        "transfer_fee_enable": d.get("transferFeeEnable"),
        "is_true_token": bool(d.get("isTrueToken")) if "isTrueToken" in d else None,
        "true_token_owners_count": _i(d.get("trueTokenHolderCount")),
        "raw": d,
        "ts": _now_iso(),
      }

    return await _cache.cached(ck, settings.SECURITY_TTL_S, _fetch)


# --------------------------- top holders ---------------------------

async def top_holders(address: str, *, chain: str = "solana",
                      limit: int = 20) -> list[dict]:
    """Top N holders by balance. Returns
        [{owner, balance, percent, ui_amount, decimals}, ...]
    sorted by percent descending. Use to spot single-address concentration
    that wasn't surfaced in the token overview.
    """
    net = _normalize_chain(chain)
    addr = (address or "").strip()
    if not net or not addr:
        return []
    headers = _headers(net)
    if not headers:
        return []
    limit = max(1, min(int(limit), 100))
    ck = ("top_holders", net, addr.lower(), limit)

    async def _fetch() -> list[dict] | None:
        body = await _http.get_json(
            f"{_BASE}/defi/v3/token/holder",
            params={"address": addr, "offset": 0, "limit": limit},
            headers=headers,
            label=f"birdeye holders {net}/{addr[:10]}",
        )
        if not isinstance(body, dict) or not body.get("success"):
            return None  # failure — not cached
        items = (body.get("data") or {}).get("items") or []
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            rows.append({
                "owner": it.get("owner"),
                "balance": _f(it.get("amount")),
                "ui_amount": _f(it.get("ui_amount") or it.get("uiAmount")),
                "decimals": _i(it.get("decimals")),
                "percent": _f(it.get("percentage") or it.get("percent")),
            })
        return rows

    return await _cache.cached(ck, settings.BIRDEYE_TTL_S, _fetch) or []


# --------------------------- trade data ---------------------------

async def trade_data(address: str, *, chain: str = "solana") -> dict | None:
    """Aggregated trade stats for one token: buy/sell counts, volumes, and
    unique-wallet activity across 30m / 1h / 2h / 4h / 8h / 24h windows.
    Useful for spotting a token that's all-buy or all-sell over the last hour.
    """
    net = _normalize_chain(chain)
    addr = (address or "").strip()
    if not net or not addr:
        return None
    headers = _headers(net)
    if not headers:
        return None
    ck = ("trade_data", net, addr.lower())

    async def _fetch() -> dict | None:
        body = await _http.get_json(
            f"{_BASE}/defi/v3/token/trade-data/single",
            params={"address": addr},
            headers=headers,
            label=f"birdeye trade_data {net}/{addr[:10]}",
        )
        if not isinstance(body, dict) or not body.get("success"):
            return None
        d = body.get("data") or {}
        out = {"chain": net, "address": addr, "ts": _now_iso()}
        # Volumes + counts keyed by window suffix; pull a useful subset and let
        # consumers read `raw` for everything.
        for window in ("30m", "1h", "2h", "4h", "8h", "24h"):
            out[f"buy_volume_usd_{window}"] = _f(d.get(f"buy_volume_usd_{window}") or d.get(f"buyVolume{window}"))
            out[f"sell_volume_usd_{window}"] = _f(d.get(f"sell_volume_usd_{window}") or d.get(f"sellVolume{window}"))
            out[f"buy_count_{window}"] = _i(d.get(f"buy_{window}") or d.get(f"buy{window}"))
            out[f"sell_count_{window}"] = _i(d.get(f"sell_{window}") or d.get(f"sell{window}"))
            out[f"unique_wallets_{window}"] = _i(d.get(f"unique_wallet_{window}") or d.get(f"uniqueWallet{window}"))
        out["raw"] = d
        return out

    return await _cache.cached(ck, settings.BIRDEYE_TTL_S, _fetch)
