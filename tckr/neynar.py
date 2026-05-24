"""Neynar — Farcaster API for cast search, channel feeds, trending content,
and Farcaster-native token signals.

Neynar acquired the Farcaster protocol in Jan 2026, so this IS Farcaster's
canonical API. For the new-pair / early-stage Sol+Base trading thesis, the
edge is: tokens on Base often get discussed on Farcaster before they pump,
and Neynar exposes both the social signal (casts, channel activity, KOL
mentions) and a curated "trending fungibles" feed ranked by Farcaster buy
activity.

Eight public functions, grouped by surface:

    # Cast / channel feeds (the social signal)
    search_casts(q)                find casts matching a query (symbol/CA/keyword)
    channel_feed(channel_ids)      recent casts in one or more channels
    trending_casts(channel_id=)    what's hot right now globally or per-channel

    # Token-native helpers (the trading signal)
    trending_fungibles()           tokens ranked by Farcaster buy activity
    token_metadata(address, net=)  metadata + price for one contract

    # User / KOL helpers
    user_by_username(name)         resolve handle -> FID (and the user object)
    user_popular_casts(fid)        recent popular casts for one user
    user_balance(fid)              token holdings of one Farcaster user

Auth: free signup at dev.neynar.com; `NEYNAR_API_KEY` env var; sent as
`x-api-key` header. The cast-search endpoint is rate-limited 5x tighter than
other endpoints across all Neynar tiers — cache aggressively or batch.

**Free-tier limitation (as of May 2026):** only `user_by_username` is on the
free tier. The other 7 functions return HTTP 402 PaymentRequired without an
upgrade. All functions degrade gracefully on 402 — they return [] / None
rather than raising — so calling code stays the same when you upgrade and
the functions start returning data. See `https://dev.neynar.com/pricing`
for current tier pricing.

Docs: https://docs.neynar.com/reference/
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.neynar")

_BASE = "https://api.neynar.com"
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


# ---------- shared HTTP ----------

async def _get(path: str, *, params: dict | None = None,
               label: str = "") -> object | None:
    if not settings.NEYNAR_API_KEY:
        log.warning("NEYNAR_API_KEY not set — neynar.%s skipped", label or path)
        return None
    headers = {"x-api-key": settings.NEYNAR_API_KEY, "accept": "application/json"}
    return await _http.get_json(f"{_BASE}{path}", params=params, headers=headers,
                                label=label or f"neynar {path}")


# ---------- normalized parsers ----------

def _parse_user(u: dict | None) -> dict | None:
    """Flatten a Neynar user object to a callable-friendly shape."""
    if not isinstance(u, dict):
        return None
    profile = (u.get("profile") or {})
    bio = (profile.get("bio") or {})
    exp = (u.get("experimental") or {})
    return {
        "fid": _i(u.get("fid")),
        "username": u.get("username"),
        "display_name": u.get("display_name"),
        "pfp_url": u.get("pfp_url"),
        "bio": bio.get("text"),
        "follower_count": _i(u.get("follower_count")),
        "following_count": _i(u.get("following_count")),
        "custody_address": u.get("custody_address"),
        "verified_eth_addresses": ((u.get("verified_addresses") or {}).get("eth_addresses") or []),
        "verified_sol_addresses": ((u.get("verified_addresses") or {}).get("sol_addresses") or []),
        "neynar_user_score": _f(exp.get("neynar_user_score")),
    }


def _parse_cast(c: dict) -> dict:
    """Flatten a Neynar cast object to the rows callers actually need."""
    channel = c.get("channel") or {}
    reactions = c.get("reactions") or {}
    return {
        "hash": c.get("hash"),
        "thread_hash": c.get("thread_hash"),
        "parent_hash": c.get("parent_hash"),
        "text": c.get("text"),
        "timestamp": _ts_to_iso(c.get("timestamp")),
        "channel_id": channel.get("id") if isinstance(channel, dict) else None,
        "channel_name": channel.get("name") if isinstance(channel, dict) else None,
        "author": _parse_user(c.get("author")),
        "likes": _i(reactions.get("likes_count")),
        "recasts": _i(reactions.get("recasts_count")),
        "replies": _i((c.get("replies") or {}).get("count")),
        "embeds": c.get("embeds") or [],
        "mentioned_channels": [(ch or {}).get("id") for ch in (c.get("mentioned_channels") or [])
                                if isinstance(ch, dict)],
    }


def _parse_token(t: dict) -> dict:
    """Flatten a Neynar fungible / token object.

    Neynar's exact field names vary by endpoint (trending vs metadata vs
    fungibles-by-id). We pull through both common forms and let unknown
    keys fall through to None.
    """
    addr = t.get("contract_address") or t.get("address")
    net = t.get("network") or t.get("chain")
    return {
        "address": addr,
        "network": net,
        "symbol": t.get("symbol"),
        "name": t.get("name"),
        "decimals": _i(t.get("decimals")),
        "image_url": t.get("image_url") or t.get("logo_url"),
        "price_usd": _f(t.get("price_usd") or (t.get("price") or {}).get("usd")
                         if isinstance(t.get("price"), dict) else t.get("price_usd")),
        "market_cap_usd": _f(t.get("market_cap")),
        "volume_24h_usd": _f(t.get("volume_24h") or t.get("volume_24h_usd")),
        "price_change_24h_pct": _f(t.get("price_change_24h") or t.get("price_change_24h_pct")),
        # Farcaster-native signals (only present on trending feed rows)
        "unique_buyers_24h": _i(t.get("unique_buyers_24h")),
        "buys_24h": _i(t.get("buys_24h")),
        "buy_volume_24h_usd": _f(t.get("buy_volume_24h")),
    }


# ---------- cast / channel feeds ----------

async def search_casts(q: str, *, channel_id: str | None = None,
                       author_fid: int | None = None,
                       mode: str = "literal", limit: int = 25) -> list[dict]:
    """Search Farcaster casts. The primary "find me mentions of X" function.

    `q` accepts Neynar's search operators: `+` (must contain), `|` (or),
    `*` (wildcard), `"phrase"` (exact), `~N` (fuzzy distance), `-term`
    (exclude), `before:DATE` / `after:DATE`.

    `mode`: "literal" (default), "semantic" (embedding-based), or "hybrid".
    `channel_id`: restrict to one channel (e.g., "base", "memes", "crypto").
    `author_fid`: restrict to one author — useful with a KOL watchlist.

    Returns up to `limit` casts, newest first (sort_type=desc_chron).
    Note: this endpoint is rate-limited 5x tighter than other Neynar
    endpoints across all tiers — cache aggressively.
    """
    if not (q or "").strip():
        return []
    capped = max(1, min(int(limit), 100))
    ck = ("search_casts", q, channel_id or "", author_fid or 0, mode, capped)
    cached = _cache.get(ck, settings.NEYNAR_FEED_TTL_S)
    if cached is not None:
        return cached

    params: dict = {"q": q, "limit": capped, "mode": mode}
    if channel_id:
        params["channel_id"] = channel_id
    if author_fid:
        params["author_fid"] = int(author_fid)

    body = await _get("/v2/farcaster/cast/search/", params=params,
                      label=f"search_casts q={q[:30]}")
    rows: list[dict] = []
    if isinstance(body, dict):
        casts = ((body.get("result") or {}).get("casts") or [])
        rows = [_parse_cast(c) for c in casts if isinstance(c, dict)]
    _cache.put(ck, rows)
    return rows


async def channel_feed(channel_ids: str | list[str], *,
                       limit: int = 25) -> list[dict]:
    """Recent casts in one or more channels.

    For the new-pair Base thesis the canonical channel is `"base"`, but
    `"memes"`, `"crypto"`, `"degen"` are also useful watch channels.
    Pass a list to merge multiple channels into one feed.
    """
    ids = [channel_ids] if isinstance(channel_ids, str) else list(channel_ids or [])
    ids = [s.strip() for s in ids if s and s.strip()]
    if not ids:
        return []
    capped = max(1, min(int(limit), 100))
    ck = ("channel_feed", ",".join(sorted(ids)), capped)
    cached = _cache.get(ck, settings.NEYNAR_FEED_TTL_S)
    if cached is not None:
        return cached

    body = await _get("/v2/farcaster/feed/channels",
                      params={"channel_ids": ",".join(ids), "limit": capped,
                              "with_recasts": "true"},
                      label=f"channel_feed {','.join(ids)[:30]}")
    rows: list[dict] = []
    if isinstance(body, dict):
        casts = body.get("casts") or []
        rows = [_parse_cast(c) for c in casts if isinstance(c, dict)]
    _cache.put(ck, rows)
    return rows


async def trending_casts(*, channel_id: str | None = None,
                          limit: int = 10) -> list[dict]:
    """Trending casts globally or in one channel."""
    capped = max(1, min(int(limit), 50))
    ck = ("trending_casts", channel_id or "", capped)
    cached = _cache.get(ck, settings.NEYNAR_FEED_TTL_S)
    if cached is not None:
        return cached

    params: dict = {"limit": capped}
    if channel_id:
        params["channel_id"] = channel_id
    body = await _get("/v2/farcaster/feed/trending", params=params,
                      label=f"trending_casts ch={channel_id or 'global'}")
    rows: list[dict] = []
    if isinstance(body, dict):
        casts = body.get("casts") or []
        rows = [_parse_cast(c) for c in casts if isinstance(c, dict)]
    _cache.put(ck, rows)
    return rows


# ---------- token-native ----------

async def trending_fungibles(*, limit: int = 20,
                              time_window: str = "24h") -> list[dict]:
    """Tokens ranked by Farcaster-native buy activity.

    This is the closest single-call edge signal for "what tokens have
    Farcaster momentum right now?" — Neynar tracks Base-side buys from
    Farcaster-linked wallets and ranks tokens by that activity. For the
    Base early-stage thesis, this is the killer endpoint.

    `time_window`: "1h", "6h", "24h", "7d" (endpoint-defined; default 24h).
    """
    capped = max(1, min(int(limit), 100))
    ck = ("trending_fungibles", time_window, capped)
    cached = _cache.get(ck, settings.NEYNAR_TOKEN_TTL_S)
    if cached is not None:
        return cached

    body = await _get("/v2/farcaster/fungible/trending",
                      params={"limit": capped, "time_window": time_window},
                      label=f"trending_fungibles {time_window}")
    rows: list[dict] = []
    if isinstance(body, dict):
        # Field name varies — try common alternatives.
        items = (body.get("fungibles") or body.get("tokens")
                  or body.get("trending") or [])
        rows = [_parse_token(t) for t in items if isinstance(t, dict)]
    _cache.put(ck, rows)
    return rows


async def token_metadata(token_address: str, *,
                          network: str = "base") -> dict | None:
    """Metadata + price for one token contract on a given network."""
    addr = (token_address or "").strip()
    if not addr:
        return None
    ck = ("token_metadata", network, addr.lower())
    cached = _cache.get(ck, settings.NEYNAR_TOKEN_TTL_S)
    if cached is not None:
        return cached

    body = await _get("/v2/farcaster/fungible",
                      params={"addresses": addr, "networks": network},
                      label=f"token_metadata {network} {addr[:10]}")
    out: dict | None = None
    if isinstance(body, dict):
        # Response is either {fungibles: {...}} keyed by address, or a list.
        fung = body.get("fungibles")
        if isinstance(fung, dict):
            inner = fung.get(addr.lower()) or fung.get(addr) or next(iter(fung.values()), None)
            if isinstance(inner, dict):
                out = _parse_token(inner)
        elif isinstance(fung, list) and fung:
            out = _parse_token(fung[0])
    _cache.put(ck, out)
    return out


# ---------- user / KOL ----------

async def user_by_username(username: str) -> dict | None:
    """Resolve a Farcaster handle to a user object (with FID).

    Use this once to bootstrap a KOL watchlist (handle -> fid mapping), then
    pass the fid to `user_popular_casts` / `user_balance` / `search_casts`.
    """
    name = (username or "").strip().lstrip("@")
    if not name:
        return None
    ck = ("user_by_username", name.lower())
    cached = _cache.get(ck, settings.NEYNAR_USER_TTL_S)
    if cached is not None:
        return cached

    body = await _get("/v2/farcaster/user/by_username",
                      params={"username": name},
                      label=f"user_by_username {name}")
    out: dict | None = None
    if isinstance(body, dict):
        out = _parse_user(body.get("user"))
    _cache.put(ck, out)
    return out


async def user_popular_casts(fid: int, *, limit: int = 10) -> list[dict]:
    """The 10 most popular casts (by engagement) for a given FID.

    KOL tracking primitive: "what is @username actually saying that landed?"
    """
    try:
        fid_i = int(fid)
    except (TypeError, ValueError):
        return []
    if fid_i < 1:
        return []
    capped = max(1, min(int(limit), 50))
    ck = ("user_popular_casts", fid_i, capped)
    cached = _cache.get(ck, settings.NEYNAR_FEED_TTL_S)
    if cached is not None:
        return cached

    body = await _get("/v2/farcaster/feed/user/popular",
                      params={"fid": fid_i, "limit": capped},
                      label=f"user_popular_casts fid={fid_i}")
    rows: list[dict] = []
    if isinstance(body, dict):
        casts = body.get("casts") or []
        rows = [_parse_cast(c) for c in casts if isinstance(c, dict)]
    _cache.put(ck, rows)
    return rows


async def user_balance(fid: int) -> list[dict] | None:
    """Token holdings of a Farcaster user (across their verified addresses).

    Useful for "is this KOL actually buying what they shill?" — pull the
    user's recent popular casts, extract mentioned tokens, cross-check
    against this list.
    """
    try:
        fid_i = int(fid)
    except (TypeError, ValueError):
        return None
    if fid_i < 1:
        return None
    ck = ("user_balance", fid_i)
    cached = _cache.get(ck, settings.NEYNAR_USER_TTL_S)
    if cached is not None:
        return cached

    body = await _get("/v2/farcaster/user/balance",
                      params={"fid": fid_i},
                      label=f"user_balance fid={fid_i}")
    rows: list[dict] = []
    if isinstance(body, dict):
        items = body.get("balances") or body.get("user_balance") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            tok = it.get("token") or it.get("fungible") or it
            row = _parse_token(tok)
            row["balance"] = _f(it.get("balance") or it.get("amount"))
            row["balance_usd"] = _f(it.get("balance_usd") or it.get("value_usd"))
            rows.append(row)
    _cache.put(ck, rows)
    return rows
