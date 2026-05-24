"""CoinGecko v3 — canonical spot prices, market data, historical OHLC.

CoinGecko is the most-used free crypto price API; until now `tckr` had
DEX-pool prices (geckoterminal) and spot/derivatives prices via exchange APIs
(hyperliquid, coinalyze) but no aggregated cross-exchange spot price + market
cap + ranks. This module fills that gap.

Three tier paths, picked automatically:
- **Public** (no key) — works at `api.coingecko.com`, ~10-30 req/min hard limit.
  Sufficient for low-volume use; surfaces all endpoints.
- **Demo key** (`COINGECKO_DEMO_API_KEY`) — same endpoints, ~30 req/min, slightly
  higher quota. Free signup at coingecko.com.
- **Pro key** (`COINGECKO_API_KEY`) — `pro-api.coingecko.com`, 500 req/min+,
  unlocks several Pro-only endpoints. Paid.

Endpoints wrapped:
- `simple_price(ids, vs_currencies)` — fast multi-coin spot
- `coin_markets(vs_currency, ids|category|order)` — top-N with full market data
- `coin(coin_id)` — full coin profile (description, links, market data, devs)
- `market_chart(coin_id, days)` — historical price / mcap / volume timeseries
- `search(query)` — autosuggest matching coins / exchanges / categories
- `trending()` — currently-trending search queries
- `global_stats()` — total mcap, BTC dominance, active coins, exchange count
- `categories()` — categories sorted by 24h mcap change
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.coingecko")

_PUBLIC_BASE = "https://api.coingecko.com/api/v3"
_PRO_BASE    = "https://pro-api.coingecko.com/api/v3"

_cache = TTLCache()


def _base_and_headers() -> tuple[str, dict | None]:
    """Pick the API base + auth header based on configured keys.

    Precedence: Pro (paid) > Demo (free signup) > Public (no key).
    """
    if settings.COINGECKO_API_KEY:
        return _PRO_BASE, {"x-cg-pro-api-key": settings.COINGECKO_API_KEY}
    if settings.COINGECKO_DEMO_API_KEY:
        return _PUBLIC_BASE, {"x-cg-demo-api-key": settings.COINGECKO_DEMO_API_KEY}
    return _PUBLIC_BASE, None


async def _get(path: str, params: dict | None = None, ttl_s: float | None = None,
               label: str | None = None):
    """Cached GET wrapper. Caches the parsed JSON; None on failure."""
    base, headers = _base_and_headers()
    url = f"{base}{path}"
    ttl = ttl_s if ttl_s is not None else settings.COINGECKO_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(url, params=params, headers=headers,
                                    label=label or f"coingecko {path}")
        if data is not None:
            _cache.put(key, data)
        return data


# ============================================================================
# Public API
# ============================================================================

async def simple_price(ids: list[str] | str, vs_currencies: list[str] | str = "usd",
                       *, include_market_cap: bool = False,
                       include_24h_vol: bool = False,
                       include_24h_change: bool = False,
                       include_last_updated: bool = False) -> dict | None:
    """Multi-coin spot price lookup.

    `ids` are CoinGecko ids (e.g. "bitcoin", not "BTC"). For symbol lookups
    use `search()` first to translate symbol -> id.

    Returns `{coin_id: {currency: price, currency_market_cap: ..., ...}}` or
    None on failure.
    """
    if isinstance(ids, list):
        ids = ",".join(ids)
    if isinstance(vs_currencies, list):
        vs_currencies = ",".join(vs_currencies)
    params = {
        "ids": ids,
        "vs_currencies": vs_currencies,
        "include_market_cap": str(bool(include_market_cap)).lower(),
        "include_24hr_vol": str(bool(include_24h_vol)).lower(),
        "include_24hr_change": str(bool(include_24h_change)).lower(),
        "include_last_updated_at": str(bool(include_last_updated)).lower(),
    }
    return await _get("/simple/price", params=params, label="coingecko simple_price")


async def coin_markets(vs_currency: str = "usd", *,
                       ids: list[str] | str | None = None,
                       category: str | None = None,
                       order: str = "market_cap_desc",
                       per_page: int = 100, page: int = 1,
                       sparkline: bool = False,
                       price_change_pct: str | None = "1h,24h,7d") -> list[dict] | None:
    """Top coins by market cap (or filter by ids/category).

    Returns a list of dicts with: id, symbol, name, current_price, market_cap,
    market_cap_rank, fully_diluted_valuation, total_volume, high_24h, low_24h,
    price_change_*_pct, ath, ath_date, atl, atl_date, image, last_updated.
    """
    params: dict = {
        "vs_currency": vs_currency,
        "order": order,
        "per_page": min(max(int(per_page), 1), 250),
        "page": max(int(page), 1),
        "sparkline": str(bool(sparkline)).lower(),
    }
    if ids:
        params["ids"] = ids if isinstance(ids, str) else ",".join(ids)
    if category:
        params["category"] = category
    if price_change_pct:
        params["price_change_percentage"] = price_change_pct
    return await _get("/coins/markets", params=params, label="coingecko coin_markets")


async def coin(coin_id: str, *, localization: bool = False,
               tickers: bool = False, market_data: bool = True,
               community_data: bool = False, developer_data: bool = False) -> dict | None:
    """Full single-coin profile (description, links, market data, devs/community).

    Heavy payload — use `simple_price` if you only need the price.
    """
    params = {
        "localization": str(bool(localization)).lower(),
        "tickers": str(bool(tickers)).lower(),
        "market_data": str(bool(market_data)).lower(),
        "community_data": str(bool(community_data)).lower(),
        "developer_data": str(bool(developer_data)).lower(),
    }
    return await _get(f"/coins/{coin_id}", params=params,
                      label=f"coingecko coin {coin_id}")


async def market_chart(coin_id: str, *, days: int | str = 30,
                       vs_currency: str = "usd",
                       interval: str | None = None) -> dict | None:
    """Historical price / market cap / total volume timeseries.

    `days`: 1, 7, 14, 30, 90, 180, 365, or "max". Sub-daily granularity is
    auto-selected by CoinGecko based on the lookback window:
      1 day      -> 5 min candles
      2-90 days  -> hourly
      91+ days   -> daily

    Returns {prices: [[ts_ms, price], ...], market_caps: [...], total_volumes: [...]}.
    """
    params: dict = {"vs_currency": vs_currency, "days": str(days)}
    if interval:
        params["interval"] = interval
    return await _get(f"/coins/{coin_id}/market_chart", params=params,
                      ttl_s=settings.COINGECKO_HISTORY_TTL_S,
                      label=f"coingecko market_chart {coin_id}")


async def search(query: str) -> dict | None:
    """Autosuggest matches across coins, exchanges, and categories.

    Returns {coins: [...], exchanges: [...], categories: [...]}. Each `coins`
    row has id / symbol / name / market_cap_rank / thumb — use the `id` for
    other endpoints (`simple_price`, `coin`, etc.).
    """
    return await _get("/search", params={"query": query},
                      label=f"coingecko search {query}")


async def trending() -> dict | None:
    """Currently trending search queries (top 7 coins + top 5 NFTs / categories).

    Returns {coins: [...], nfts: [...], categories: [...]}.
    """
    return await _get("/search/trending", label="coingecko trending")


async def global_stats() -> dict | None:
    """Total crypto market cap, BTC dominance, active coins, exchange count.

    Returns {data: {total_market_cap, total_volume, market_cap_percentage, ...}}.
    """
    return await _get("/global", label="coingecko global")


async def categories(*, order: str = "market_cap_desc") -> list[dict] | None:
    """All CoinGecko categories with rollup market cap + 24h change.

    Useful for sector rotations (e.g. spot AI-token-category mcap change).
    """
    return await _get("/coins/categories", params={"order": order},
                      label="coingecko categories")


async def coin_id_from_symbol(symbol: str) -> str | None:
    """Convenience: best-effort map from a ticker symbol (e.g. 'BTC') to a
    CoinGecko id (e.g. 'bitcoin'). Returns the highest-mcap match, or None."""
    s = (symbol or "").strip().lower()
    if not s:
        return None
    hits = await search(s)
    if not hits or not hits.get("coins"):
        return None
    # CoinGecko returns matches in roughly relevance order; symbol-exact matches
    # first. The first row whose symbol matches exactly is the best bet.
    for c in hits["coins"]:
        if (c.get("symbol") or "").lower() == s:
            return c.get("id")
    # Fallback: the highest-ranked match overall.
    return hits["coins"][0].get("id")
