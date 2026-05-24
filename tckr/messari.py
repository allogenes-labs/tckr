"""Messari — research-grade asset profiles, metrics, news.

Base: `https://data.messari.io/api`. Auth via `x-messari-api-key` header.

Tier caveat: Messari moved most data behind paid plans in 2024-2025. The free
tier ("Hobbyist") gives ~20 req/min on a small subset of endpoints. Many of
the high-value ones (full profile, fundraising rounds, research) are paid.
This module surfaces both — calls degrade gracefully (return None) when an
upstream returns 401/402/429.

Endpoints wrapped:
- `asset(slug)` — basic asset by slug or id (free tier OK for a few fields)
- `asset_metrics(slug)` — full metrics: price, mcap, supply, ROI, mining stats,
  marketcap dominance, all-time-high. (paywalled on free tier post-2024)
- `asset_profile(slug)` — long-form research profile w/ team, tokenomics. (paid)
- `news_feed(limit, page, fields)` — research / news feed. (free tier OK)
- `assets(limit, page)` — paginated asset list with key metrics

Common slugs match common-knowledge tickers: 'bitcoin', 'ethereum', 'solana'.
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.messari")

_BASE = "https://data.messari.io/api"
_cache = TTLCache()


def _headers() -> dict | None:
    if not settings.MESSARI_API_KEY:
        log.warning("MESSARI_API_KEY not set — messari skipped")
        return None
    return {"x-messari-api-key": settings.MESSARI_API_KEY}


async def _get(path: str, params: dict | None = None,
               label: str | None = None):
    headers = _headers()
    if headers is None:
        return None
    ttl = settings.MESSARI_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(f"{_BASE}{path}", params=params, headers=headers,
                                    label=label or f"messari {path}")
        if not isinstance(data, dict):
            return None
        # Messari returns {data: ..., status: ...}. Unwrap; on error, status.code != 200.
        status = data.get("status") or {}
        if status.get("error_code") or (status.get("error_message") and not data.get("data")):
            log.warning("messari %s -> %s", path,
                        status.get("error_message") or status.get("error_code"))
            return None
        result = data.get("data")
        if result is not None:
            _cache.put(key, result)
        return result


# ============================================================================
# Public API
# ============================================================================

async def asset(slug: str) -> dict | None:
    """Basic asset info (id, slug, symbol, name) — free tier."""
    return await _get(f"/v1/assets/{slug}", label=f"messari asset {slug}")


async def asset_metrics(slug: str) -> dict | None:
    """Full metrics (price, mcap, supply, ROI, dominance, ATH). Paid post-2024."""
    return await _get(f"/v1/assets/{slug}/metrics",
                      label=f"messari metrics {slug}")


async def asset_profile(slug: str) -> dict | None:
    """Long-form research profile (team, tokenomics, governance). Paid."""
    return await _get(f"/v2/assets/{slug}/profile",
                      label=f"messari profile {slug}")


async def assets(limit: int = 20, page: int = 1) -> list[dict] | None:
    """Paginated asset list with key metrics."""
    r = await _get("/v2/assets",
                   params={"limit": min(max(int(limit), 1), 500),
                           "page":  max(int(page), 1)},
                   label="messari assets")
    if isinstance(r, list):
        return r
    return None


async def news_feed(*, limit: int = 25, page: int = 1) -> list[dict] | None:
    """Research + news items. Free tier returns curated items."""
    r = await _get("/v1/news",
                   params={"limit": min(max(int(limit), 1), 100),
                           "page":  max(int(page), 1)},
                   label="messari news_feed")
    return r if isinstance(r, list) else None
