"""GDELT — keyless global news/event firehose for market-moving context.

The GDELT Project monitors world news in ~65 languages and exposes it through
the DOC 2.0 API with no key and no signup. For tckr it fills the slot the
crypto-native feeds can't: macro and tradfi events that move every market —
central-bank decisions, regulation, geopolitics, conflict — searchable by
keyword across the entire global press, not just crypto outlets.

API base: `https://api.gdeltproject.org/api/v2/doc/doc` (keyless).

Rate limit: GDELT asks for **no more than one request every 5 seconds**. When
exceeded it returns an HTTP-200 *plain-text* notice instead of JSON; that fails
to parse and surfaces here as None (graceful, like any other upstream miss). The
per-source TTL cache keeps normal usage well under the limit — but a burst of
distinct queries can still trip it, so space them out.

Two modes wrapped:
- `articles(query, ...)`  — ArtList: latest matching articles.
- `tone_timeline(query)`  — TimelineTone: average sentiment (tone) of coverage
  over time. Useful for "is the macro narrative on X turning negative?".

Query syntax is GDELT's own: space-separated terms are AND, `OR` must be
explicit and uppercased, `"quoted phrases"` match exactly, and operators like
`sourcecountry:US` / `sourcelang:english` / `domain:reuters.com` can be embedded
directly in `query`.

`articles()` normalizes each row to the shared tckr news shape:

    {
      "title":        "...",
      "url":          "https://...",
      "source":       "reuters.com",          # publisher domain
      "published_at": "2026-06-28T20:15:00+00:00",
      "published_ts": 1782... ,               # epoch seconds | None
      "summary":      "",                     # GDELT ArtList carries no body
      "image":        "https://...",          # socialimage | None
      "language":     "English",
      "source_country": "United States",
    }
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.gdelt")

_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
_cache = TTLCache()

# GDELT caps a single ArtList request at 250 records.
_MAX_RECORDS = 250
_SORTS = ("datedesc", "dateasc", "tonedesc", "toneasc", "hybridrel")

# Process-wide rate gate. GDELT 429s a burst of *distinct* queries (the TTL cache
# only absorbs repeats), so we serialize uncached fetches and space them by
# settings.GDELT_MIN_INTERVAL_S to honor the documented ~1 req / 5s limit. This
# turns "31 of 47 calls 429'd" into reliable (if slower) delivery.
_rate_lock = asyncio.Lock()
_last_fetch_mono = 0.0


async def _get(params: dict, label: str):
    global _last_fetch_mono
    ttl = settings.GDELT_TTL_S
    key = tuple(sorted(params.items()))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        # Serialize cold fetches process-wide and space them END-TO-END by
        # GDELT_MIN_INTERVAL_S. Spacing must be measured from the *completion* of
        # the previous request, not its start: a 429 inside one call triggers
        # _http's rapid 0.5/1s retries, so timing from the start would let the
        # next call fire <5s after those retries and 429 again.
        async with _rate_lock:
            gap = settings.GDELT_MIN_INTERVAL_S
            if gap > 0:
                wait = gap - (time.monotonic() - _last_fetch_mono)
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                data = await _http.get_json(_BASE, params=params, label=label)
            finally:
                _last_fetch_mono = time.monotonic()
        # Throttle / empty responses come back as text (-> None here) — don't
        # cache those, so the next call retries instead of serving an empty hit.
        if not isinstance(data, dict):
            return None
        _cache.put(key, data)
        return data


def _parse_seendate(raw: str | None) -> tuple[str | None, int | None]:
    """GDELT 'YYYYMMDDTHHMMSSZ' -> (iso8601, epoch_seconds)."""
    if not raw:
        return None, None
    try:
        dt = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None, None
    return dt.isoformat(), int(dt.timestamp())


def _normalize(row: dict) -> dict:
    iso, ts = _parse_seendate(row.get("seendate"))
    return {
        "title": row.get("title"),
        "url": row.get("url"),
        "source": row.get("domain"),
        "published_at": iso,
        "published_ts": ts,
        "summary": "",  # ArtList has no article body
        "image": row.get("socialimage") or None,
        "language": row.get("language"),
        "source_country": row.get("sourcecountry"),
    }


async def articles(query: str, *, timespan: str = "3d",
                   max_records: int = 30, sort: str = "datedesc") -> list[dict] | None:
    """Latest articles across the global press matching `query`.

    `timespan` is a GDELT window like '1d', '3d', '1w', '24h', '30min'.
    `sort` ∈ {datedesc, dateasc, tonedesc, toneasc, hybridrel}.
    Returns newest-first normalized items (default sort), or None on miss.
    """
    q = (query or "").strip()
    if not q:
        log.warning("gdelt articles: empty query")
        return None
    sort = sort if sort in _SORTS else "datedesc"
    params = {
        "query": q,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max(1, min(int(max_records), _MAX_RECORDS)),
        "sort": sort,
        "timespan": timespan,
    }
    data = await _get(params, label=f"gdelt artlist {q[:40]}")
    if not isinstance(data, dict):
        return None
    rows = data.get("articles") or []
    return [_normalize(r) for r in rows if isinstance(r, dict) and r.get("url")]


async def tone_timeline(query: str, *, timespan: str = "1w") -> list[dict] | None:
    """Average sentiment (tone) of global coverage of `query` over time.

    Returns `[{date, tone}, ...]` where `tone` is GDELT's average document tone
    (negative = more negative coverage, ~0 neutral, positive = upbeat). Handy as
    a macro-narrative gauge. None on miss.
    """
    q = (query or "").strip()
    if not q:
        log.warning("gdelt tone_timeline: empty query")
        return None
    params = {
        "query": q,
        "mode": "TimelineTone",
        "format": "json",
        "timespan": timespan,
    }
    data = await _get(params, label=f"gdelt tone {q[:40]}")
    if not isinstance(data, dict):
        return None
    series = data.get("timeline") or []
    if not series:
        return []
    points = series[0].get("data") or []
    out: list[dict] = []
    for p in points:
        iso, _ = _parse_seendate(p.get("date"))
        try:
            tone = float(p.get("value")) if p.get("value") is not None else None
        except (TypeError, ValueError):
            tone = None
        out.append({"date": iso or p.get("date"), "tone": tone})
    return out
