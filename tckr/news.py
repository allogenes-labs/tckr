"""Unified news cascade — best-available headlines across every news provider.

Like `tckr.quotes` / `tckr.history` but for news: callers that just want "what's
happening" (optionally about a topic) shouldn't have to know which providers are
keyless, which need a key, or how each shapes its payload. This module fans out
across the news sources that are usable in the current environment, merges and
de-duplicates the results, and tags each item with the `provider` that produced
it.

Providers merged:
  - `cryptonews` (keyless) — crypto-native outlet RSS (Cointelegraph, Decrypt,
    The Block, CoinDesk). Crypto-first, always on.
  - `gdelt`      (keyless) — global news/event firehose. Macro + tradfi context;
    only queried when a `query` is given (it needs a search term).
  - `finnhub`    (keyed-free) — tradfi/market + crypto headlines. Included only
    when FINNHUB_API_KEY is configured.

Every item carries the shared news shape plus a `provider` field:

    {
      "title", "url", "source", "published_at", "published_ts",
      "summary", "image",
      "provider": "cryptonews" | "gdelt" | "finnhub",
    }

Results are de-duplicated by URL, sorted newest-first (undated items last), and
capped to `limit`. As with the other cascades, an unavailable provider simply
contributes nothing — never an error.
"""
from __future__ import annotations

import asyncio
import logging

from tckr import cryptonews, finnhub, gdelt, registry

log = logging.getLogger("tckr.news")

# When no topic is given, GDELT still needs a query — use a crypto-first but
# tradfi-inclusive default so the macro firehose contributes market-movers.
_DEFAULT_GDELT_QUERY = (
    '(cryptocurrency OR bitcoin OR "stock market" OR "federal reserve" '
    'OR "interest rates")'
)

_COMMON_KEYS = ("title", "url", "source", "published_at", "published_ts",
                "summary", "image")


def _tag(items: list[dict] | None, provider: str) -> list[dict]:
    """Project each item onto the shared shape + a provider tag."""
    out: list[dict] = []
    for it in items or []:
        if not it.get("url"):
            continue
        row = {k: it.get(k) for k in _COMMON_KEYS}
        row["provider"] = provider
        out.append(row)
    return out


async def latest(query: str | None = None, *, limit: int = 30,
                 include: list[str] | None = None) -> list[dict]:
    """Merged, recency-sorted news across all available providers.

    `query` (optional) narrows the topic: it filters the crypto RSS client-side
    and drives GDELT's global search. With no query, returns the latest crypto +
    (if keyed) tradfi headlines, plus GDELT's default market-movers feed.

    `include` (optional) restricts which providers run, e.g. ['cryptonews'] or
    ['gdelt', 'finnhub']. Default: every provider usable in this environment.
    """
    limit = max(1, int(limit))
    want = set(include) if include else {"cryptonews", "gdelt", "finnhub"}

    tasks: list[tuple[str, asyncio.Future]] = []
    if "cryptonews" in want:
        tasks.append(("cryptonews",
                      cryptonews.latest(limit=limit * 2, query=query)))
    if "gdelt" in want:
        gq = query or _DEFAULT_GDELT_QUERY
        tasks.append(("gdelt",
                      gdelt.articles(gq, max_records=min(limit * 2, 250))))
    # Finnhub only contributes when its key is configured (registry is the
    # single source of truth for that).
    if "finnhub" in want and registry.configured("finnhub"):
        cat = "crypto" if (query and _looks_crypto(query)) else "general"
        tasks.append(("finnhub", finnhub.market_news(cat)))

    results = await asyncio.gather(*(t[1] for t in tasks))

    merged: list[dict] = []
    seen: set[str] = set()
    terms = (query or "").lower().split()
    for (provider, _), rows in zip(tasks, results, strict=True):
        tagged = _tag(rows, provider)
        # gdelt/finnhub-general aren't topic-filtered upstream; apply the same
        # token-AND filter cryptonews already did so a query means the same
        # thing across providers. GDELT already searched server-side (and its
        # ArtList carries no summary), so it's exempt from the client filter.
        for item in tagged:
            url = item["url"]
            if url in seen:
                continue
            if terms and provider != "gdelt":
                hay = f"{item.get('title', '')} {item.get('summary', '')}".lower()
                if not all(t in hay for t in terms):
                    continue
            seen.add(url)
            merged.append(item)

    merged.sort(key=lambda r: (r.get("published_ts") is not None,
                               r.get("published_ts") or 0), reverse=True)
    return merged[:limit]


_CRYPTO_HINTS = ("crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "sol",
                 "defi", "token", "altcoin", "stablecoin", "memecoin", "nft")


def _looks_crypto(query: str) -> bool:
    q = query.lower()
    return any(h in q for h in _CRYPTO_HINTS)
