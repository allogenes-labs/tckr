"""Polymarket Gamma API — prediction-market odds (binary YES/NO markets).

Public, keyless. Composes with macro context: a Fed-rates Polymarket and DXY
trend together is a richer signal than either alone.

Endpoints:
- `markets(...)` — list markets, filter by tag / closed / volume
- `events(...)` — events grouping related markets
- `market(slug_or_id)` — full single-market data
- `top_volume(limit)` — convenience filter on `markets` sorted by 24h volume

The Gamma API returns prices in [0, 1] for YES tokens; NO = 1 - YES. Volume
fields are in USDC.
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.polymarket")

_BASE = "https://gamma-api.polymarket.com"

_cache = TTLCache()


async def _get(path: str, params: dict | None = None, label: str | None = None):
    ttl = settings.POLYMARKET_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(f"{_BASE}{path}", params=params,
                                    label=label or f"polymarket {path}")
        if data is not None:
            _cache.put(key, data)
        return data


def _shape_market(m: dict) -> dict:
    """Pick the fields actually useful for a trading agent; drop the long tail."""
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices")
    # The API serializes these as JSON strings — parse if so.
    if isinstance(outcomes, str):
        try:
            import json
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = None
    if isinstance(outcome_prices, str):
        try:
            import json
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = None
    # YES price = the first outcome's price by convention.
    yes_price = None
    if isinstance(outcome_prices, list) and outcome_prices:
        try:
            yes_price = float(outcome_prices[0])
        except (TypeError, ValueError):
            yes_price = None
    return {
        "id":            m.get("id"),
        "slug":          m.get("slug"),
        "question":      m.get("question"),
        "description":   (m.get("description") or "")[:400],
        "yes_price":     yes_price,
        "outcomes":      outcomes,
        "outcome_prices": outcome_prices,
        "volume":        _to_float(m.get("volume")),
        "volume_24h":    _to_float(m.get("volumeNum") or m.get("volume24hr")),
        "liquidity":     _to_float(m.get("liquidity")),
        "end_date":      m.get("endDate"),
        "closed":        bool(m.get("closed")),
        "active":        bool(m.get("active")),
        "category":      m.get("category"),
        "tags":          m.get("tags") or [],
    }


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ============================================================================
# Public API
# ============================================================================

async def markets(*, limit: int = 50, offset: int = 0,
                  active: bool | None = True, closed: bool | None = False,
                  tag: str | None = None,
                  order: str = "volume", ascending: bool = False) -> list[dict] | None:
    """List markets. Filter on active/closed; sort by `order` (volume, liquidity,
    endDate, startDate). Returns shaped market dicts."""
    params: dict = {
        "limit": min(max(int(limit), 1), 500),
        "offset": max(int(offset), 0),
        "order": order,
        "ascending": "true" if ascending else "false",
    }
    if active is not None:
        params["active"] = "true" if active else "false"
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    if tag:
        params["tag"] = tag
    data = await _get("/markets", params=params, label="polymarket markets")
    if not isinstance(data, list):
        return None
    return [_shape_market(m) for m in data]


async def market(slug_or_id: str) -> dict | None:
    """Single market by slug or numeric id."""
    # Gamma supports both /markets/{id} and a slug query — use slug query for
    # robustness across both inputs.
    rows = await _get("/markets", params={"slug": slug_or_id},
                      label=f"polymarket market {slug_or_id}")
    if isinstance(rows, list) and rows:
        return _shape_market(rows[0])
    # Fallback: try as numeric id path
    data = await _get(f"/markets/{slug_or_id}", label=f"polymarket market id {slug_or_id}")
    if isinstance(data, dict):
        return _shape_market(data)
    return None


async def top_volume(limit: int = 20) -> list[dict] | None:
    """Active markets sorted by 24h volume — a discovery pass for what's hot."""
    return await markets(limit=limit, active=True, closed=False, order="volume")


async def events(*, limit: int = 25, active: bool | None = True,
                 closed: bool | None = False, tag: str | None = None) -> list[dict] | None:
    """Events grouping related markets. Less useful for trading than `markets`
    but exposed for completeness."""
    params: dict = {
        "limit": min(max(int(limit), 1), 500),
    }
    if active is not None:
        params["active"] = "true" if active else "false"
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    if tag:
        params["tag"] = tag
    data = await _get("/events", params=params, label="polymarket events")
    if not isinstance(data, list):
        return None
    return data
