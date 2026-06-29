"""Solscan — Solana block-explorer convenience.

NOTE (2026-06): Solscan **retired the public no-key API** — `public-api.solscan.io`
now returns 404. The public (`pro=False`) code path is kept for backward-compat
but degrades to None; a Pro key (`SOLSCAN_API_KEY`) is now required for live
data. Set the key and pass `pro=True` (or rely on functions that auto-upgrade).

Solscan's Pro API returns richer endpoints with higher rate limits. Functions
try the public path first; if SOLSCAN_API_KEY is set, they upgrade to the Pro
endpoints.

Public endpoints (`https://public-api.solscan.io`, RETIRED — see note) covered:
- token metadata
- account info / token balances
- simple tx lookups

Pro endpoints (`https://pro-api.solscan.io/v2.0`, header `token: <key>`) cover:
- richer account analytics
- DeFi positions
- portfolio rollups
- token-holder distributions

The functions exposed here favor the public endpoints so the module is useful
without a key. When Pro is configured, callers can opt into the richer
versions via `pro=True`.
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.solscan")

_PUBLIC_BASE = "https://public-api.solscan.io"
_PRO_BASE    = "https://pro-api.solscan.io/v2.0"

_cache = TTLCache()


def _headers(pro: bool) -> dict | None:
    if pro:
        if not settings.SOLSCAN_API_KEY:
            log.warning("SOLSCAN_API_KEY not set — Pro endpoint skipped")
            return None
        return {"token": settings.SOLSCAN_API_KEY}
    # Public endpoint accepts an optional User-Agent; default httpx UA is fine.
    return None


async def _get(path: str, params: dict | None = None,
               pro: bool = False, label: str | None = None):
    base = _PRO_BASE if pro else _PUBLIC_BASE
    headers = _headers(pro)
    if pro and headers is None:
        return None
    url = f"{base}{path}"
    ttl = settings.SOLSCAN_TTL_S
    key = (pro, path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(url, params=params, headers=headers,
                                    label=label or f"solscan {path}")
        if data is not None:
            _cache.put(key, data)
        return data


# ============================================================================
# Public API
# ============================================================================

async def token_meta(mint: str, *, pro: bool = False) -> dict | None:
    """Token metadata by SPL mint address.

    Public path returns {symbol, name, decimals, holder, market_cap, ...};
    Pro adds richer market data.
    """
    if pro:
        return await _get("/token/meta", params={"address": mint},
                          pro=True, label="solscan token/meta(pro)")
    return await _get("/token/meta", params={"tokenAddress": mint},
                      pro=False, label="solscan token/meta")


async def account_info(address: str, *, pro: bool = False) -> dict | None:
    """Account-level info (lamports balance, type, executable, owner)."""
    if pro:
        return await _get("/account/detail", params={"address": address},
                          pro=True, label="solscan account/detail(pro)")
    return await _get("/account", params={"address": address},
                      pro=False, label="solscan account")


async def account_tokens(address: str, *, pro: bool = False) -> list[dict] | None:
    """SPL token balances for an address."""
    if pro:
        data = await _get("/account/token-accounts",
                          params={"address": address, "type": "token"},
                          pro=True, label="solscan token-accounts(pro)")
        if isinstance(data, dict):
            return data.get("data")
        return data if isinstance(data, list) else None
    return await _get("/account/tokens", params={"account": address},
                      pro=False, label="solscan account/tokens")


async def token_holders(mint: str, *, limit: int = 20, offset: int = 0,
                        pro: bool = False) -> list[dict] | None:
    """Top holders of an SPL token by balance. Pro endpoint is more reliable."""
    params = {
        "tokenAddress": mint if not pro else None,
        "address":      mint if pro else None,
        "limit":        min(max(int(limit), 1), 100),
        "offset":       max(int(offset), 0),
    }
    params = {k: v for k, v in params.items() if v is not None}
    if pro:
        data = await _get("/token/holders", params=params, pro=True,
                          label="solscan token/holders(pro)")
        if isinstance(data, dict):
            return data.get("data")
        return data
    return await _get("/token/holders", params=params, pro=False,
                      label="solscan token/holders")


async def tx_detail(signature: str, *, pro: bool = False) -> dict | None:
    """Parsed transaction detail by signature.

    Pro returns much richer parsing (DeFi action labels, IDL-decoded args).
    """
    if pro:
        return await _get("/transaction/detail", params={"tx": signature},
                          pro=True, label="solscan tx/detail(pro)")
    return await _get("/transaction", params={"tx": signature},
                      pro=False, label="solscan transaction")
