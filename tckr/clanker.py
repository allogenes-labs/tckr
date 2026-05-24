"""Clanker — Farcaster-native token launcher (Base, Arbitrum, BNB, others).

Clanker is the dominant Farcaster-native token launcher; tokens are deployed
by tagging the @clanker bot on Warpcast/Supercast. As of early 2026 it ranks
in the top of Base token-launch volume (~$8B all-time, $600K+ daily fees in
peak weeks). Acquired by Farcaster.

Pre-pool detection value: Clanker deploys to a Uniswap V4 pool by default
(`type=clanker_v4`, `pool_address` is a bytes32 PoolId). Catching tokens here
gives you the deployment before any DEX aggregator indexes them. The
`requestor_fid` field on every token directly cross-links with [[neynar]]
helpers for KOL-aware deployment tracking.

Public surface:

    new_tokens(limit=25, chain_id=8453)        recently deployed tokens
    trending_tokens(limit=25)                  trending via Clanker's CoinGecko feed
    tokens_by_fid(fid, limit=25)               tokens deployed by a Farcaster FID
    tokens_by_deployer(addr, limit=25)         tokens deployed by a wallet
    token_info(pool_address)                   one token's details by pool address
    holders(token_address)                     holder count + top-10 concentration

Public API at `https://www.clanker.world/api` — no auth required for the
endpoints exposed here.

Docs: https://github.com/clanker-devco/DOCS
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.clanker")

_BASE = "https://www.clanker.world/api"
_cache = TTLCache()

# Chain IDs Clanker supports — mirrors what their factory deploys on.
_SUPPORTED_CHAIN_IDS = {
    8453: "base",
    42161: "arbitrum",
    56: "bsc",
    1: "eth",
}


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


def _parse_row(r: dict) -> dict:
    """Flatten one Clanker token row to the unified schema.

    Preserves `requestor_fid` and `cast_hash` so callers can pipe directly
    into [[neynar]] user_popular_casts / search_casts for KOL context.
    """
    pool_config = r.get("pool_config") or {}
    extensions  = r.get("extensions") or {}
    tags        = r.get("tags") or {}
    social_ctx  = r.get("social_context") or {}
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "symbol": r.get("symbol"),
        "contract_address": r.get("contract_address"),
        "pool_address": r.get("pool_address"),         # bytes32 PoolId for V4
        "type": r.get("type"),                          # 'clanker_v4', etc.
        "chain_id": _i(r.get("chain_id")),
        "chain": _SUPPORTED_CHAIN_IDS.get(_i(r.get("chain_id")) or -1),
        "pair_symbol": r.get("pair"),
        "paired_token_address": pool_config.get("pairedToken"),
        "supply": r.get("supply"),
        "starting_market_cap_usd": _f(r.get("starting_market_cap")),
        "img_url": r.get("img_url"),
        "deployer_address": r.get("msg_sender"),
        "admin_address": r.get("admin"),
        "factory_address": r.get("factory_address"),
        "locker_address": r.get("locker_address"),
        # Farcaster cross-link — pipe into [[neynar]].user_popular_casts(fid)
        # to see what the deployer is saying about this token.
        "requestor_fid": _i(r.get("requestor_fid")),
        "cast_hash": r.get("cast_hash"),
        "social_platform": social_ctx.get("platform"),
        "deployed_at_iso": r.get("deployed_at"),
        "created_at_iso": r.get("created_at"),
        "tx_hash": r.get("tx_hash"),
        "is_verified": bool(tags.get("verified")),
        "is_champagne": bool(tags.get("champagne")),  # Clanker's "VIP" indicator
        "warnings": r.get("warnings") or [],
        "fee_config": extensions.get("fees"),
    }


# ---------- shared HTTP ----------

async def _get(path: str, *, params: dict | None = None,
               label: str = "") -> object | None:
    return await _http.get_json(f"{_BASE}{path}", params=params,
                                label=label or f"clanker {path}")


async def _post(path: str, body: object, *,
                label: str = "") -> object | None:
    return await _http.post_json(f"{_BASE}{path}", body,
                                  label=label or f"clanker POST {path}")


def _extract_rows(body) -> list[dict]:
    if not isinstance(body, dict):
        if isinstance(body, list):
            return [_parse_row(r) for r in body if isinstance(r, dict)]
        return []
    data = body.get("data")
    if isinstance(data, list):
        return [_parse_row(r) for r in data if isinstance(r, dict)]
    return []


# ---------- public functions ----------

async def new_tokens(limit: int = 25, *, chain_id: int = 8453) -> list[dict]:
    """Recently deployed Clanker tokens, newest first.

    Default `chain_id=8453` (Base). Pass 42161 (Arbitrum), 1 (Ethereum), or
    56 (BSC) for other chains Clanker deploys on. The Clanker API itself
    doesn't filter by chain server-side here — we filter client-side after
    fetch.
    """
    capped = max(1, min(int(limit), 100))
    ck = ("new", capped, chain_id)
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    # Fetch a wider window so client-side chain filter still hits `limit` rows.
    body = await _get("/tokens", params={"page": 1, "limit": min(100, capped * 4)},
                       label="clanker new")
    rows = _extract_rows(body)
    if chain_id:
        rows = [r for r in rows if r.get("chain_id") == chain_id]
    rows = rows[:capped]
    _cache.put(ck, rows)
    return rows


def _parse_trending_pool(r: dict) -> dict:
    """Normalize one Clanker /trending row (CoinGecko-pool schema, very
    different from /tokens) to a row that's useful alongside new_tokens."""
    attrs = r.get("attributes") or {}
    rel   = r.get("relationships") or {}
    rid   = r.get("id") or ""
    # `id` is like 'base_0x...' — strip the chain prefix for the bare address.
    pool_address = attrs.get("address") or (rid.split("_", 1)[1] if "_" in rid else rid)
    return {
        "name": attrs.get("name"),
        "symbol": attrs.get("base_token_symbol") or attrs.get("symbol"),
        "pool_address": pool_address,
        "type": "trending_pool",
        "chain": rid.split("_", 1)[0] if "_" in rid else None,
        "price_usd": _f(attrs.get("base_token_price_usd")),
        "fdv_usd": _f(attrs.get("fdv_usd")),
        "market_cap_usd": _f(attrs.get("market_cap_usd")),
        "volume_24h_usd": _f((attrs.get("volume_usd") or {}).get("h24")
                              if isinstance(attrs.get("volume_usd"), dict) else None),
        "reserve_usd": _f(attrs.get("reserve_in_usd")),
        "price_change_pct_24h": _f((attrs.get("price_change_percentage") or {}).get("h24")
                                    if isinstance(attrs.get("price_change_percentage"), dict) else None),
        "transactions_24h": (attrs.get("transactions") or {}).get("h24")
                              if isinstance(attrs.get("transactions"), dict) else None,
        "pool_created_at_iso": attrs.get("pool_created_at"),
        "dex_id": ((rel.get("dex") or {}).get("data") or {}).get("id"),
        "_raw": r,
    }


async def trending_tokens(limit: int = 25) -> list[dict]:
    """Trending Clanker tokens (CoinGecko-backed pool ranking).

    Schema differs from `new_tokens` because Clanker's trending endpoint
    proxies CoinGecko's pool data; this returns the parsed pool view rather
    than the deploy view. Useful for "what's hot right now," not for
    deployment metadata.
    """
    capped = max(1, min(int(limit), 100))
    ck = ("trending", capped)
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    body = await _get("/tokens/trending", params={"limit": capped},
                       label="clanker trending")
    rows: list[dict] = []
    if isinstance(body, dict):
        trending = body.get("trending")
        if isinstance(trending, list):
            rows = [_parse_trending_pool(r) for r in trending if isinstance(r, dict)]
    rows = rows[:capped]
    _cache.put(ck, rows)
    return rows


async def tokens_by_fid(fid: int, *, limit: int = 25) -> list[dict]:
    """Tokens deployed by a specific Farcaster FID.

    Bridge between [[neynar]] user lookups and Clanker deployments: given a
    KOL's FID, see every token they've launched.
    """
    try:
        fid_i = int(fid)
    except (TypeError, ValueError):
        return []
    if fid_i < 1:
        return []
    capped = max(1, min(int(limit), 100))
    ck = ("by_fid", fid_i, capped)
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    body = await _get("/tokens/fetch-deployed-by-fid",
                       params={"fid": fid_i, "limit": capped},
                       label=f"clanker by_fid {fid_i}")
    rows = _extract_rows(body)[:capped]
    _cache.put(ck, rows)
    return rows


async def tokens_by_deployer(deployer_address: str, *,
                              limit: int = 25) -> list[dict]:
    """Tokens deployed by a specific wallet address (EVM)."""
    addr = (deployer_address or "").strip()
    if not addr:
        return []
    capped = max(1, min(int(limit), 100))
    ck = ("by_deployer", addr.lower(), capped)
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    body = await _get("/tokens/fetch-deployed-by-address",
                       params={"address": addr, "limit": capped},
                       label=f"clanker by_deployer {addr[:10]}")
    rows = _extract_rows(body)[:capped]
    _cache.put(ck, rows)
    return rows


async def token_info(pool_address: str) -> dict | None:
    """Full token record by pool address.

    **Currently fragile**: Clanker's `/tokens/fetch-by-pool-address` endpoint
    rejects both V4 PoolIds and EVM contract addresses in our testing — it
    appears to want a specific Uniswap-pool-address format we haven't
    isolated. Returns None gracefully for unrecognized inputs. For per-token
    detail today, the practical workaround is `tokens_by_deployer(addr)` or
    `tokens_by_fid(fid)` and filtering client-side.
    """
    addr = (pool_address or "").strip()
    if not addr:
        return None
    ck = ("token_info", addr.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_TOKEN_TTL_S)
    if cached is not None:
        return cached
    body = await _get("/tokens/fetch-by-pool-address",
                       params={"poolAddress": addr},
                       label=f"clanker token_info {addr[:14]}")
    if isinstance(body, dict):
        if isinstance(body.get("data"), dict):
            out = _parse_row(body["data"])
        elif body.get("contract_address") or body.get("pool_address"):
            out = _parse_row(body)
        else:
            out = None
    else:
        out = None
    _cache.put(ck, out)
    return out


async def holders(token_address: str) -> dict | None:
    """Holder count + top-10 concentration for one Clanker token contract.

    Complements per-token Birdeye/Bitquery holder data with the Clanker
    metric so callers don't need a second module for this one field.
    """
    addr = (token_address or "").strip()
    if not addr:
        return None
    ck = ("holders", addr.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_TOKEN_TTL_S)
    if cached is not None:
        return cached
    body = await _get(f"/tokens/{addr}/holders",
                       label=f"clanker holders {addr[:10]}")
    if not isinstance(body, dict):
        return None
    out = {
        "token_address": addr,
        "holder_count": _i(body.get("holderCount") or body.get("holder_count")),
        "top_10_pct": _f(body.get("top10Concentration")
                         or body.get("top_10_concentration")
                         or body.get("top10Pct")),
        "_raw": body,
    }
    _cache.put(ck, out)
    return out
