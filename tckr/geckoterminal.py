"""GeckoTerminal API v2 — DEX pools, tokens, and OHLCV.

Free, no API key. Covers every network GeckoTerminal indexes; this module is
used primarily for Base and Solana, but `network` accepts any GT network id
(and common aliases like "sol" via settings.normalize_network).

Public functions return plain dicts/lists with a uniform, flattened shape —
the JSON:API envelope (data / attributes / relationships / included) is parsed
away. Everything is cached and degrades gracefully (None / [] on failure).

Docs: https://www.geckoterminal.com/dex-api
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.geckoterminal")

_BASE = "https://api.geckoterminal.com/api/v2"
# Pin the API version per GT's guidance so a server-side bump can't surprise us.
_HEADERS = {"accept": "application/json;version=20230302"}
_cache = TTLCache()

_TIMEFRAMES = {"day", "hour", "minute"}


# --------------------------- parsing helpers ---------------------------

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _f(v) -> float | None:
    """Coerce GT's stringified numbers to float; None on anything unparseable."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _index_included(included: list | None) -> dict[tuple[str, str], dict]:
    """Map (type, id) -> attributes for JSON:API `included` resolution."""
    out: dict[tuple[str, str], dict] = {}
    for it in included or []:
        t, i = it.get("type"), it.get("id")
        if t and i:
            out[(t, i)] = it.get("attributes") or {}
    return out


def _rel_id(raw: dict, key: str):
    """Return the related resource id(s) for `key`, or None."""
    data = ((raw.get("relationships") or {}).get(key) or {}).get("data")
    if isinstance(data, list):
        return [d.get("id") for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        return data.get("id")
    return None


def _addr_from_id(gid: str | None) -> str | None:
    """GT ids look like `base_0xabc…` or `solana_ETMh…` — strip the network."""
    if not gid:
        return None
    return gid.split("_", 1)[1] if "_" in gid else gid


def _resolve_token(raw: dict, key: str, inc: dict) -> dict | None:
    tid = _rel_id(raw, key)
    if not isinstance(tid, str):
        return None
    attrs = inc.get(("token", tid), {})
    return {
        "address": attrs.get("address") or _addr_from_id(tid),
        "symbol": attrs.get("symbol"),
        "name": attrs.get("name"),
    }


def _parse_pool(raw: dict, inc: dict) -> dict:
    a = raw.get("attributes") or {}
    return {
        "network": (raw.get("id") or "").split("_", 1)[0] or None,
        "pool_address": a.get("address"),
        "name": a.get("name"),
        "dex": _rel_id(raw, "dex"),
        "base_token": _resolve_token(raw, "base_token", inc),
        "quote_token": _resolve_token(raw, "quote_token", inc),
        "price_usd": _f(a.get("base_token_price_usd")),
        "fdv_usd": _f(a.get("fdv_usd")),
        "market_cap_usd": _f(a.get("market_cap_usd")),
        "reserve_usd": _f(a.get("reserve_in_usd")),
        "volume_24h_usd": _f((a.get("volume_usd") or {}).get("h24")),
        "price_change_pct": a.get("price_change_percentage") or {},
        "transactions": a.get("transactions") or {},
        "created_at": a.get("pool_created_at"),
        "ts": _now_iso(),
    }


def _parse_token(raw: dict) -> dict:
    a = raw.get("attributes") or {}
    return {
        "network": (raw.get("id") or "").split("_", 1)[0] or None,
        "address": a.get("address"),
        "symbol": a.get("symbol"),
        "name": a.get("name"),
        "decimals": a.get("decimals"),
        "price_usd": _f(a.get("price_usd")),
        "fdv_usd": _f(a.get("fdv_usd")),
        "market_cap_usd": _f(a.get("market_cap_usd")),
        "total_reserve_usd": _f(a.get("total_reserve_in_usd")),
        "volume_24h_usd": _f((a.get("volume_usd") or {}).get("h24")),
        "total_supply": a.get("normalized_total_supply") or a.get("total_supply"),
        "coingecko_id": a.get("coingecko_coin_id"),
        "image_url": a.get("image_url"),
        "ts": _now_iso(),
    }


# --------------------------- pool lists ---------------------------

async def _pool_list(kind: str, network: str, page: int,
                     limit: int | None) -> list[dict]:
    net = settings.normalize_network(network) or settings.NETWORK_BASE
    ck = (kind, net, page)
    pools = _cache.get(ck, settings.DEX_TTL_S)
    if pools is None:
        body = await _http.get_json(
            f"{_BASE}/networks/{net}/{kind}",
            params={"page": page, "include": "base_token,quote_token,dex"},
            headers=_HEADERS,
            label=f"geckoterminal {kind} {net}",
        )
        if not body or "data" not in body:
            return []
        inc = _index_included(body.get("included"))
        pools = [_parse_pool(p, inc) for p in body.get("data") or []]
        _cache.put(ck, pools)
    return pools[:limit] if limit else pools


async def trending_pools(network: str = "base", *, page: int = 1,
                         limit: int | None = None) -> list[dict]:
    """Pools trending on `network` right now, strongest signal first."""
    return await _pool_list("trending_pools", network, page, limit)


async def new_pools(network: str = "base", *, page: int = 1,
                    limit: int | None = None) -> list[dict]:
    """Most recently created pools on `network` — a new-launch radar."""
    return await _pool_list("new_pools", network, page, limit)


async def top_pools(network: str = "base", *, page: int = 1,
                    limit: int | None = None) -> list[dict]:
    """Highest liquidity / volume pools on `network`."""
    return await _pool_list("pools", network, page, limit)


# --------------------------- token info ---------------------------

async def token_info(network: str, address: str) -> dict | None:
    """Token snapshot by contract address: price, FDV, market cap, 24h volume."""
    net = settings.normalize_network(network) or settings.NETWORK_BASE
    address = (address or "").strip()
    if not address:
        return None
    ck = ("token", net, address.lower())
    cached = _cache.get(ck, settings.DEX_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(
        f"{_BASE}/networks/{net}/tokens/{address}",
        headers=_HEADERS,
        label=f"geckoterminal token {net}/{address}",
    )
    data = (body or {}).get("data")
    if not data:
        return None
    tok = _parse_token(data)
    _cache.put(ck, tok)
    return tok


# --------------------------- OHLCV ---------------------------

async def pool_ohlcv(
    network: str,
    pool_address: str,
    *,
    timeframe: str = "day",
    aggregate: int = 1,
    limit: int = 100,
    currency: str = "usd",
) -> dict | None:
    """OHLCV candles for a pool. `timeframe` is one of {day, hour, minute}.

    Returns {network, pool_address, timeframe, base, quote, candles}, where
    `candles` is chronological [{t, o, h, l, c, v}, ...].
    """
    net = settings.normalize_network(network) or settings.NETWORK_BASE
    timeframe = (timeframe or "day").strip().lower()
    if timeframe not in _TIMEFRAMES:
        log.warning("geckoterminal: unsupported timeframe %r", timeframe)
        return None
    pool_address = (pool_address or "").strip()
    if not pool_address:
        return None

    ck = ("ohlcv", net, pool_address.lower(), timeframe, aggregate, limit, currency)
    cached = _cache.get(ck, settings.DEX_OHLCV_TTL_S)
    if cached is not None:
        return cached

    body = await _http.get_json(
        f"{_BASE}/networks/{net}/pools/{pool_address}/ohlcv/{timeframe}",
        params={"aggregate": aggregate, "limit": limit, "currency": currency},
        headers=_HEADERS,
        label=f"geckoterminal ohlcv {net}/{pool_address}",
    )
    if not body:
        return None

    attrs = ((body.get("data") or {}).get("attributes")) or {}
    candles: list[dict] = []
    for row in attrs.get("ohlcv_list") or []:
        if not row or len(row) < 6:
            continue
        ts, o, hi, lo, c, v = row[:6]
        try:
            t_iso = datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
        except (TypeError, ValueError, OSError):
            continue
        candles.append({"t": t_iso, "o": _f(o), "h": _f(hi),
                         "l": _f(lo), "c": _f(c), "v": _f(v)})
    candles.sort(key=lambda x: x["t"])  # GT returns newest-first; make chronological

    meta = body.get("meta") or {}
    out = {
        "network": net,
        "pool_address": pool_address,
        "timeframe": timeframe,
        "base": meta.get("base"),
        "quote": meta.get("quote"),
        "candles": candles,
    }
    _cache.put(ck, out)
    return out
