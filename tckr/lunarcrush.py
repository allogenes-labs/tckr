"""LunarCrush — social-sentiment scoring across crypto + topics.

LunarCrush aggregates Twitter/X, Reddit, news, YouTube, and on-chain signals
into per-coin "Galaxy Score" and "AltRank" metrics that have historically
correlated with retail attention.

API base: `https://lunarcrush.com/api4`. Auth via `Authorization: Bearer
<LUNARCRUSH_API_KEY>`. Free tier (Discover plan, formerly "Individual")
gives ~100 req/day on `/public/*` endpoints; paid tiers unlock higher
quotas and the `/v4/*` endpoints.

Endpoints wrapped:
- `coins_list_v1()` — all tracked coins with Galaxy Score, AltRank, price,
  social volume + sentiment.
- `coin(coin_id_or_symbol)` — single-coin detail
- `coin_time_series(coin, bucket, interval)` — historical Galaxy/AltRank/etc.
- `topic(topic_slug)` — topic-level social (e.g. "ai", "rwa", "memecoins")
- `topics_list()` — currently-trending topics
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.lunarcrush")

_BASE = "https://lunarcrush.com/api4"
_cache = TTLCache()


def _headers() -> dict | None:
    if not settings.LUNARCRUSH_API_KEY:
        log.warning("LUNARCRUSH_API_KEY not set — lunarcrush skipped")
        return None
    return {"Authorization": f"Bearer {settings.LUNARCRUSH_API_KEY}"}


async def _get(path: str, params: dict | None = None,
               label: str | None = None):
    headers = _headers()
    if headers is None:
        return None
    ttl = settings.LUNARCRUSH_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(f"{_BASE}{path}", params=params, headers=headers,
                                    label=label or f"lunarcrush {path}")
        if not isinstance(data, dict):
            return None
        # LunarCrush wraps payload in {data: [...]} for list endpoints, {data: {...}}
        # for single-item. A body with no `data` key is the 200-OK error/rate-limit
        # envelope — return None (unknown) so single-item callers like coin()/topic()
        # degrade gracefully instead of handing back the error envelope as data. Not
        # cached, so a transient throttle doesn't poison the TTL.
        if "data" not in data:
            return None
        result = data["data"]
        _cache.put(key, result)
        return result


async def coins_list() -> list[dict] | None:
    """All tracked coins with social + market metrics.

    Each row carries: id, symbol, name, price, market_cap, galaxy_score, alt_rank,
    sentiment, interactions_24h, social_dominance, social_volume_24h.
    """
    r = await _get("/public/coins/list/v1", label="lunarcrush coins_list")
    return r if isinstance(r, list) else None


async def coin(coin_id_or_symbol: str | int) -> dict | None:
    """Single-coin detail by LunarCrush id or symbol."""
    return await _get(f"/public/coins/{coin_id_or_symbol}/v1",
                      label=f"lunarcrush coin {coin_id_or_symbol}")


async def coin_time_series(coin_id_or_symbol: str | int, *,
                            bucket: str = "day",
                            interval: str = "1m") -> list[dict] | None:
    """Time series of coin metrics.

    `bucket` ∈ {hour, day}, `interval` ∈ {1d, 1w, 1m, 3m, 6m, 1y, all}.
    """
    r = await _get(f"/public/coins/{coin_id_or_symbol}/time-series/v2",
                   params={"bucket": bucket, "interval": interval},
                   label=f"lunarcrush time-series {coin_id_or_symbol}")
    return r if isinstance(r, list) else None


async def topic(topic_slug: str) -> dict | None:
    """Topic-level social metrics (e.g. 'ai', 'memecoins', 'rwa')."""
    return await _get(f"/public/topic/{topic_slug}/v1",
                      label=f"lunarcrush topic {topic_slug}")


async def topics_list() -> list[dict] | None:
    """All tracked topics with their current social metrics."""
    r = await _get("/public/topics/list/v1", label="lunarcrush topics_list")
    return r if isinstance(r, list) else None
