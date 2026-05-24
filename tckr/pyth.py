"""Pyth Network Hermes — keyless on-chain oracle price feeds.

Pyth is the dominant on-chain oracle on Solana + many EVMs. Hermes is the
public REST gateway that mirrors the on-chain price updates (publisher-signed,
aggregated, with confidence intervals).

Endpoints:
- `feeds(query=None, asset_type=None)` — list available feeds + metadata
  (id, symbol, asset_type, base, quote). Cached long (catalog rarely changes).
- `latest_price(feed_ids, parsed=True)` — current price for one or many feeds.
- `latest_price_for_symbols(symbols)` — convenience: pass symbols like
  "BTC/USD", "ETH/USD"; resolves to feed ids via the catalog then fetches.
- `feed_id_for_symbol(symbol)` — single-feed id lookup.

Price shape (parsed): {price, conf, expo, publish_time}. Convert to a real
number with `price * 10**expo`. `conf` is the publisher-aggregated confidence
interval — wide conf relative to price = data quality is shaky right now.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.pyth")

_BASE = "https://hermes.pyth.network"
_cache = TTLCache()


def _scaled(p: dict) -> float | None:
    """Convert Pyth's int + expo encoding to a real number, or None on bad input."""
    try:
        return int(p["price"]) * (10 ** int(p["expo"]))
    except (KeyError, TypeError, ValueError):
        return None


async def _get(path: str, params: list[tuple[str, str]] | None = None,
               ttl_s: float | None = None, label: str | None = None):
    ttl = ttl_s if ttl_s is not None else settings.PYTH_PRICE_TTL_S
    # Use tuple-of-pairs for params so multi-valued keys (`ids[]=...`) survive.
    key = (path, tuple(params or ()))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        # Build a query string manually so we can repeat `ids[]=...`.
        url = f"{_BASE}{path}"
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"
        data = await _http.get_json(url, label=label or f"pyth {path}")
        if data is not None:
            _cache.put(key, data)
        return data


async def feeds(query: str | None = None,
                asset_type: str | None = None) -> list[dict] | None:
    """List Pyth price feeds. `asset_type` ∈ {crypto, equity, fx, metal, rates}."""
    params: list[tuple[str, str]] = []
    if query:
        params.append(("query", query))
    if asset_type:
        params.append(("asset_type", asset_type))
    data = await _get("/v2/price_feeds", params=params,
                      ttl_s=settings.PYTH_CATALOG_TTL_S,
                      label="pyth feeds")
    if not isinstance(data, list):
        return None
    out = []
    for f in data:
        attrs = f.get("attributes") or {}
        out.append({
            "id":           f.get("id"),
            "symbol":       attrs.get("symbol"),
            "asset_type":   attrs.get("asset_type"),
            "base":         attrs.get("base"),
            "quote_currency": attrs.get("quote_currency"),
            "description":  attrs.get("description"),
            "display_symbol": attrs.get("display_symbol"),
        })
    return out


async def feed_id_for_symbol(symbol: str) -> str | None:
    """Map a Pyth-style symbol (e.g. 'BTC/USD', 'Crypto.BTC/USD') to a feed id."""
    s = (symbol or "").strip()
    if not s:
        return None
    all_feeds = await feeds(query=s.split("/")[0]) or []
    # Match on attributes.symbol exact (case-insensitive), preferring crypto.
    s_norm = s.upper()
    crypto_hits = [f for f in all_feeds if f.get("asset_type") == "crypto"]
    pool = crypto_hits or all_feeds
    for f in pool:
        sym = (f.get("symbol") or "").upper()
        # Pyth's canonical "Crypto.BTC/USD" form has a prefix; strip it.
        if sym == s_norm or sym.endswith(f".{s_norm}") or sym.endswith(f"/{s_norm}"):
            return f.get("id")
    # Lenient fallback: any symbol ending with "/USD" matching base
    base = s_norm.split("/")[0]
    for f in pool:
        sym = (f.get("symbol") or "").upper()
        if sym.endswith(f".{base}/USD") or sym.endswith(f"{base}/USD"):
            return f.get("id")
    return None


async def latest_price(feed_ids: Iterable[str] | str) -> list[dict] | None:
    """Latest parsed prices for one or more feed ids.

    Returns a list of {id, symbol_metadata, price_usd_approx, raw_price,
    confidence, expo, publish_time}.
    """
    if isinstance(feed_ids, str):
        feed_ids = [feed_ids]
    ids = [fid for fid in feed_ids if fid]
    if not ids:
        return []
    params = [("ids[]", fid) for fid in ids] + [("parsed", "true")]
    data = await _get("/v2/updates/price/latest", params=params,
                      label="pyth latest_price")
    if not isinstance(data, dict):
        return None
    parsed = data.get("parsed") or []
    out = []
    for entry in parsed:
        price = entry.get("price") or {}
        out.append({
            "id":            entry.get("id"),
            "price":         _scaled(price),
            "confidence":    _scaled({"price": price.get("conf"),
                                       "expo": price.get("expo")}),
            "expo":          price.get("expo"),
            "publish_time":  price.get("publish_time"),
            "raw":           price,
        })
    return out


async def latest_price_for_symbols(symbols: Iterable[str]) -> list[dict] | None:
    """Convenience: pass symbols like 'BTC/USD'; resolves + fetches in one go."""
    ids = []
    sym_by_id: dict[str, str] = {}
    for s in symbols:
        fid = await feed_id_for_symbol(s)
        if fid:
            ids.append(fid)
            sym_by_id[fid] = s
    if not ids:
        return []
    rows = await latest_price(ids) or []
    for r in rows:
        r["symbol"] = sym_by_id.get(r["id"])
    return rows
