"""Finnhub — tradfi + crypto market news (free key, generous limits).

Finnhub's free tier is one of the more generous in financial data: ~60 req/min
with no card required, and the news endpoints are included. This is tckr's
window onto market-moving *tradfi* events — general macro headlines, company
news, FX, and M&A — alongside the crypto-native feeds (`cryptonews`) and the
keyless global event firehose (`gdelt`).

API base: `https://finnhub.io/api/v1`. Auth via the `X-Finnhub-Token` header
(keeps the key out of URLs/logs; Finnhub also accepts a `token` query param).
Free signup at finnhub.io.

Endpoints wrapped:
- `market_news(category)` — `/news?category=` for general | forex | crypto |
  merger. The broad market firehose.
- `company_news(symbol, days)` — `/company-news` for a single US ticker over a
  date window.

Both normalize to the shared tckr news shape (title/url/source/published_at/
published_ts/summary/image) while preserving Finnhub's own fields (id, category,
related). Degrades gracefully: no key, or any upstream error, returns None.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.finnhub")

_BASE = "https://finnhub.io/api/v1"
_cache = TTLCache()

# Accepted categories for the market-news firehose.
NEWS_CATEGORIES = ("general", "forex", "crypto", "merger")


def _headers() -> dict | None:
    if not settings.FINNHUB_API_KEY:
        log.warning("FINNHUB_API_KEY not set — finnhub skipped")
        return None
    return {"X-Finnhub-Token": settings.FINNHUB_API_KEY}


async def _get(path: str, params: dict | None = None,
               label: str | None = None):
    headers = _headers()
    if headers is None:
        return None
    ttl = settings.FINNHUB_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(f"{_BASE}{path}", params=params, headers=headers,
                                    label=label or f"finnhub {path}")
        # News endpoints return a JSON array; an error body is a dict.
        if not isinstance(data, list):
            return None
        _cache.put(key, data)
        return data


def _normalize(row: dict) -> dict:
    """Map a Finnhub news row onto the shared tckr news shape."""
    ts = row.get("datetime")
    iso = None
    try:
        ts = int(ts) if ts is not None else None
        if ts:
            iso = datetime.fromtimestamp(ts, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        ts = None
    return {
        "title": row.get("headline"),
        "url": row.get("url"),
        "source": row.get("source"),
        "published_at": iso,
        "published_ts": ts,
        "summary": row.get("summary") or "",
        "image": row.get("image") or None,
        # Finnhub-native extras worth keeping.
        "id": row.get("id"),
        "category": row.get("category"),
        "related": row.get("related") or "",
    }


async def market_news(category: str = "general") -> list[dict] | None:
    """Broad market news firehose.

    `category` ∈ {general, forex, crypto, merger}. `general` is the macro/tradfi
    headline stream most useful for "what's moving markets". Returns newest-first
    normalized items, or None if no key / upstream error.
    """
    cat = (category or "general").strip().lower()
    if cat not in NEWS_CATEGORIES:
        log.warning("finnhub market_news: unknown category %r (known: %s)",
                    category, ", ".join(NEWS_CATEGORIES))
        cat = "general"
    rows = await _get("/news", params={"category": cat},
                      label=f"finnhub news {cat}")
    if not isinstance(rows, list):
        return None
    items = [_normalize(r) for r in rows if isinstance(r, dict) and r.get("url")]
    items.sort(key=lambda r: (r.get("published_ts") is not None,
                              r.get("published_ts") or 0), reverse=True)
    return items


async def company_news(symbol: str, *, days: int = 7) -> list[dict] | None:
    """News for a single US-listed ticker over the trailing `days` window.

    `symbol` is a US stock symbol (e.g. 'AAPL', 'TSLA', 'NVDA'). Returns
    newest-first normalized items, or None if no key / upstream error.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    days = max(1, min(int(days), 365))
    today = datetime.now(UTC).date()
    frm = (today - timedelta(days=days)).isoformat()
    to = today.isoformat()
    rows = await _get("/company-news",
                      params={"symbol": sym, "from": frm, "to": to},
                      label=f"finnhub company-news {sym}")
    if not isinstance(rows, list):
        return None
    items = [_normalize(r) for r in rows if isinstance(r, dict) and r.get("url")]
    items.sort(key=lambda r: (r.get("published_ts") is not None,
                              r.get("published_ts") or 0), reverse=True)
    return items
