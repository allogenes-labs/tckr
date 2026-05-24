"""Virtuals Protocol — AI-agent token launchpad on Base.

Virtuals is the agent-token launchpad on Base (and now multi-chain). Each
agent token launches into a bonding curve paired with `$VIRTUAL` (not
WETH — distinct from pump.fun's SOL-pairing). Tokens must accumulate
42,000 VIRTUAL on the curve before graduating to a Uniswap V2 pool with
10-year locked LP. The value for new-pair traders is **pre-graduation
discovery** — catching agent tokens in the bonding-curve phase before
they hit DEX aggregators.

Public surface:

    new_tokens(limit=20, chain="base")        recently created agent tokens
    about_to_graduate(limit=20, chain="base") close to 42K VIRTUAL threshold
    recently_graduated(limit=20, chain="base") already on a Uniswap pool
    genesis_launches(limit=20, chain="base")  Genesis-style (lottery) launches
    token_info(token_address, chain="base")   full agent details by address

Each row carries the canonical Virtuals fields (mcap in VIRTUAL, liquidity
USD, holder count, dev-holding %, launched_at, etc.) plus our normalized
keys for cross-module consistency.

No API key required — Virtuals exposes `api.virtuals.io/api/virtuals` as
a public Strapi-style endpoint. Cache aggressively to be a good citizen.

Docs (informal): https://whitepaper.virtuals.io
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.virtuals")

_BASE = "https://api.virtuals.io/api/virtuals"
_cache = TTLCache()


# ---------- small parse helpers ----------

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


def _ts_to_iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        ts = int(v)
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_row(r: dict) -> dict:
    """Flatten one Virtuals agent record to the unified schema.

    Virtuals' raw schema is rich (~60+ fields); we surface the ones useful
    for trading + cross-module joining. Caller can inspect `_raw` for the
    full record if needed.
    """
    return {
        "id": r.get("id"),
        "uid": r.get("uid"),
        "name": r.get("name"),
        "symbol": r.get("symbol"),
        "description": r.get("description"),
        "category": r.get("category"),  # 'PRODUCTIVITY', 'ENTERTAINMENT', ...
        "status": r.get("status"),       # 'UNDERGRAD'|'AVAILABLE'|'SENTIENT'|...
        "chain": r.get("chain"),
        "image_url": r.get("image"),
        "creator_address": r.get("walletAddress"),
        "token_address": r.get("tokenAddress"),
        "pre_token_address": r.get("preToken"),
        "pre_pair_address": r.get("preTokenPair"),
        "lp_address": r.get("lpAddress"),
        "lp_created_at_iso": _ts_to_iso(r.get("lpCreatedAt")),
        "launched_at_iso": _ts_to_iso(r.get("launchedAt")),
        "created_at_iso": _ts_to_iso(r.get("createdAt")),
        "mcap_in_virtual": _f(r.get("mcapInVirtual")),
        "fdv_in_virtual": _f(r.get("fdvInVirtual")),
        "liquidity_usd": _f(r.get("liquidityUsd")),
        "tvl_usd": _f(r.get("totalValueLocked")),
        "virtual_token_value": _f(r.get("virtualTokenValue")),
        "volume_24h": _f(r.get("volume24h")),
        "net_volume_24h": _f(r.get("netVolume24h")),
        "price_change_pct_24h": _f(r.get("priceChangePercent24h")),
        "holder_count": _i(r.get("holderCount")),
        "holder_count_change_pct_24h": _f(r.get("holderCountPercent24h")),
        "top_10_holder_pct": _f(r.get("top10HolderPercentage")),
        "dev_holding_pct": _f(r.get("devHoldingPercentage")),
        "mindshare": _f(r.get("mindshare")),
        "level": _i(r.get("level")),
        "is_verified": bool(r.get("isVerified")),
        "is_dev_committed": bool(r.get("isDevCommitted")),
        "is_graduated": bool(r.get("lpAddress")),  # presence of LP = graduated
        "is_genesis": bool(r.get("genesis")),
        "factory": r.get("factory"),
        "socials": r.get("socials"),
        "_raw": r,  # full Virtuals record for callers that want the long tail
    }


# ---------- shared HTTP ----------

async def _get(params: dict, *, label: str) -> list[dict]:
    """Strapi-style GET. Returns the `data` list (parsed rows)."""
    body = await _http.get_json(_BASE, params=params, label=label)
    if not isinstance(body, dict):
        return []
    rows = body.get("data") or []
    return [_parse_row(r) for r in rows if isinstance(r, dict)]


def _strapi_params(*, page_size: int, sort: str,
                    chain: str | None = None,
                    extra_filters: dict | None = None) -> dict:
    """Build Strapi v4-style query params. Strapi uses bracket syntax for
    nested filters (e.g. `filters[status][$eq]=AVAILABLE`)."""
    params: dict = {
        "pagination[page]": 1,
        "pagination[pageSize]": page_size,
        "sort[0]": sort,
    }
    if chain:
        params["filters[chain][$eq]"] = chain.upper()
    if extra_filters:
        params.update(extra_filters)
    return params


# ---------- public functions ----------

async def new_tokens(limit: int = 20, *, chain: str = "base") -> list[dict]:
    """Recently created Virtuals agent tokens, newest first.

    Includes both undergrad (still on curve) and graduated agents. Filter
    via `is_graduated` / `status` on each row, or use `about_to_graduate`
    / `recently_graduated` for pre-segmented views.
    """
    capped = max(1, min(int(limit), 100))
    ck = ("new", capped, chain.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    params = _strapi_params(page_size=capped, sort="createdAt:desc", chain=chain)
    rows = await _get(params, label=f"virtuals new ({chain})")
    _cache.put(ck, rows)
    return rows


async def about_to_graduate(limit: int = 20, *, chain: str = "base") -> list[dict]:
    """Agents still on the bonding curve (UNDERGRAD), sorted by closest to
    graduation — i.e. highest `mcap_in_virtual` (graduation triggers at 42K
    VIRTUAL). Each row also gets a derived `bonding_progress_pct` field
    (mcap_in_virtual / 42000 * 100).

    Implementation note: Virtuals' API silently ignores Strapi `filters[]`
    parameters, so we fetch the newest 100 rows server-side (newest are the
    UNDERGRADs — graduates are older) then filter + re-sort client-side.
    Caveat: an UNDERGRAD that's been sitting near 41K for weeks may fall
    outside this window. In practice graduation happens within hours of
    that threshold so the window covers the live frontier.
    """
    capped = max(1, min(int(limit), 100))
    ck = ("about_to_graduate", capped, chain.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached

    params = _strapi_params(page_size=100, sort="createdAt:desc", chain=chain)
    all_rows = await _get(params, label=f"virtuals about_to_graduate ({chain})")
    undergrads = [r for r in all_rows if r.get("status") == "UNDERGRAD"]
    # Sort by mcap desc — highest is closest to the 42K graduation threshold.
    undergrads.sort(key=lambda r: r.get("mcap_in_virtual") or 0, reverse=True)
    for r in undergrads:
        mv = r.get("mcap_in_virtual")
        r["bonding_progress_pct"] = (mv / 42000.0 * 100.0) if mv is not None else None
    out = undergrads[:capped]
    _cache.put(ck, out)
    return out


async def recently_graduated(limit: int = 20, *, chain: str = "base") -> list[dict]:
    """Agents that have completed the bonding curve and have a live LP pool,
    sorted by most recent graduation."""
    capped = max(1, min(int(limit), 100))
    ck = ("recently_graduated", capped, chain.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    params = _strapi_params(
        page_size=capped, sort="lpCreatedAt:desc", chain=chain,
        extra_filters={"filters[lpAddress][$notNull]": "true"},
    )
    rows = await _get(params, label=f"virtuals recently_graduated ({chain})")
    _cache.put(ck, rows)
    return rows


async def genesis_launches(limit: int = 20, *, chain: str = "base") -> list[dict]:
    """Genesis-style launches (Virtuals' lottery / fair-launch model).
    Sorted newest first."""
    capped = max(1, min(int(limit), 100))
    ck = ("genesis", capped, chain.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    params = _strapi_params(
        page_size=capped, sort="createdAt:desc", chain=chain,
        extra_filters={"filters[genesis][$notNull]": "true"},
    )
    rows = await _get(params, label=f"virtuals genesis ({chain})")
    _cache.put(ck, rows)
    return rows


async def token_info(token_address: str, *, chain: str = "base") -> dict | None:
    """Full agent record for one token address. Accepts either the live
    `tokenAddress` (graduated) or `preToken` (still on curve)."""
    addr = (token_address or "").strip()
    if not addr:
        return None
    ck = ("token_info", addr.lower(), chain.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_TOKEN_TTL_S)
    if cached is not None:
        return cached
    # Try matching tokenAddress first (graduated), then preToken (undergrad).
    for field in ("tokenAddress", "preToken"):
        params = {
            "pagination[pageSize]": 1,
            f"filters[{field}][$eqi]": addr,  # eqi = case-insensitive
        }
        rows = await _get(params, label=f"virtuals token_info {field} {addr[:10]}")
        if rows:
            _cache.put(ck, rows[0])
            return rows[0]
    _cache.put(ck, None)
    return None
