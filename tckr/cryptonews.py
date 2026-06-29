"""Crypto news — keyless headline aggregator over major outlets' RSS feeds.

Most "crypto news API" providers now require a key (CryptoCompare/CCData moved
behind CoinDesk Data; CryptoPanic gates its developer API). The outlets
themselves, however, still publish open RSS/Atom feeds with no signup and no
rate limit worth worrying about. This module fetches a curated set of those
feeds, parses them with the standard library (no new dependency), and
normalizes every item to the shared tckr news shape so it composes with the
keyed providers (`finnhub`) and the keyless event firehose (`gdelt`) behind the
`tckr.news` cascade.

Feeds (all keyless, English):
- cointelegraph  — https://cointelegraph.com/rss
- decrypt        — https://decrypt.co/feed
- theblock       — https://www.theblock.co/rss.xml
- coindesk       — https://www.coindesk.com/arc/outboundfeeds/rss

Normalized item shape (the shared tckr news shape):

    {
      "title":        "Bitcoin reclaims $80k as ...",
      "url":          "https://...",
      "source":       "cointelegraph",        # feed id (also the publisher)
      "published_at": "2026-06-28T22:59:38+00:00",
      "published_ts": 1782... ,               # epoch seconds (int) | None
      "summary":      "plain-text lede, HTML stripped",
      "author":       "Cointelegraph by ...", # may be None
      "categories":   ["Features"],           # may be []
      "image":        "https://...",          # may be None
    }

Everything degrades gracefully: a dead or malformed feed contributes nothing
rather than raising, matching every other tckr fetcher.
"""
from __future__ import annotations

import asyncio
import logging
import re
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.cryptonews")

# feed id -> RSS url. The id doubles as the `source` on every emitted item.
FEEDS: dict[str, str] = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt":       "https://decrypt.co/feed",
    "theblock":      "https://www.theblock.co/rss.xml",
    "coindesk":      "https://www.coindesk.com/arc/outboundfeeds/rss",
}

_cache = TTLCache()

# RSS namespaces we read beyond the bare RSS 2.0 elements.
_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str | None, *, limit: int = 400) -> str:
    """Collapse an HTML description blob to a plain-text lede."""
    if not text:
        return ""
    plain = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
    return plain[:limit]


def _parse_date(raw: str | None) -> tuple[str | None, int | None]:
    """RFC-822 pubDate -> (iso8601, epoch_seconds). (None, None) on failure."""
    if not raw:
        return None, None
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError):
        return None, None
    if dt is None:
        return None, None
    return dt.isoformat(), int(dt.timestamp())


def _first_image(item: ET.Element) -> str | None:
    """Best-effort image URL from media:content / media:thumbnail / enclosure."""
    for tag in ("media:content", "media:thumbnail"):
        el = item.find(tag, _NS)
        if el is not None and el.get("url"):
            return el.get("url")
    enc = item.find("enclosure")
    if enc is not None and enc.get("url"):
        return enc.get("url")
    return None


def _text(item: ET.Element, tag: str, ns: dict | None = None) -> str | None:
    el = item.find(tag, ns) if ns else item.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _parse_feed(xml: str, source: str) -> list[dict]:
    """Parse an RSS 2.0 document into normalized news items."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("cryptonews %s -> XML parse error: %s", source, e)
        return []
    items: list[dict] = []
    # RSS 2.0 nests items under channel; be lenient and search descendants.
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        if not title or not link:
            continue
        iso, ts = _parse_date(_text(it, "pubDate"))
        categories = [c.text.strip() for c in it.findall("category")
                      if c.text and c.text.strip()]
        items.append({
            "title": title,
            "url": link,
            "source": source,
            "published_at": iso,
            "published_ts": ts,
            "summary": _strip_html(_text(it, "description")),
            "author": _text(it, "dc:creator", _NS) or _text(it, "author"),
            "categories": categories,
            "image": _first_image(it),
        })
    return items


async def feed(source: str) -> list[dict] | None:
    """Fetch + parse a single outlet's feed by id (see `FEEDS`). None if the
    id is unknown or the fetch fails."""
    url = FEEDS.get(source)
    if url is None:
        log.warning("cryptonews: unknown feed %r (known: %s)",
                    source, ", ".join(FEEDS))
        return None
    ttl = settings.CRYPTONEWS_TTL_S
    key = ("feed", source)
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        xml = await _http.get_text(url, label=f"cryptonews {source}")
        if not xml:
            return None
        items = _parse_feed(xml, source)
        _cache.put(key, items)
        return items


async def latest(*, sources: list[str] | None = None, limit: int = 30,
                 query: str | None = None) -> list[dict]:
    """Merged, recency-sorted headlines across `sources` (default: all feeds).

    `query` (optional) filters client-side: case-insensitive substring match on
    title + summary (RSS has no server-side search). Items are de-duplicated by
    URL and sorted newest-first; ones without a parseable date sort last.
    """
    wanted = [s for s in (sources or list(FEEDS)) if s in FEEDS]
    if not wanted:
        return []
    results = await asyncio.gather(*(feed(s) for s in wanted))

    merged: list[dict] = []
    seen: set[str] = set()
    terms = (query or "").lower().split()
    for rows in results:
        for item in rows or []:
            url = item.get("url")
            if not url or url in seen:
                continue
            if terms:
                hay = f"{item.get('title', '')} {item.get('summary', '')}".lower()
                if not all(t in hay for t in terms):
                    continue
            seen.add(url)
            merged.append(item)

    merged.sort(key=lambda r: (r.get("published_ts") is not None,
                               r.get("published_ts") or 0), reverse=True)
    return merged[:max(1, int(limit))]
