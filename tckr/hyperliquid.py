"""Hyperliquid info API — perps marks, funding, open interest, order books.

Free, no API key. Single POST /info endpoint with a {"type": ...} payload.
This module exposes the market-data subset (no user/account queries).

Conventions:
- Symbols are the perp "coin" names ("BTC", "ETH", "SOL", ...).
- Upstream numbers are strings; parsers cast to float.
- Funding rate is per-hour (Hyperliquid charges hourly). `funding_apr_pct` is
  the linear-annualization convenience field.

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.hyperliquid")

_BASE = "https://api.hyperliquid.xyz"
_cache = TTLCache()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


async def _info(payload: dict, *, label: str = ""):
    return await _http.post_json(
        f"{_BASE}/info",
        payload,
        label=label or f"hyperliquid {payload.get('type')}",
    )


def _parse_perp(meta_row: dict, ctx_row: dict) -> dict:
    name = meta_row.get("name")
    mark = _f(ctx_row.get("markPx"))
    prev_day = _f(ctx_row.get("prevDayPx"))
    oi = _f(ctx_row.get("openInterest"))
    day_change_pct = None
    if mark is not None and prev_day not in (None, 0.0):
        day_change_pct = (mark / prev_day - 1.0) * 100.0
    oi_usd = (oi * mark) if (oi is not None and mark is not None) else None
    funding_hr = _f(ctx_row.get("funding"))
    funding_apr_pct = (funding_hr * 24 * 365 * 100.0) if funding_hr is not None else None
    return {
        "symbol": name,
        "mark_px": mark,
        "oracle_px": _f(ctx_row.get("oraclePx")),
        "mid_px": _f(ctx_row.get("midPx")),
        "prev_day_px": prev_day,
        "day_change_pct": day_change_pct,
        "funding_rate_hourly": funding_hr,
        "funding_apr_pct": funding_apr_pct,
        "open_interest": oi,
        "open_interest_usd": oi_usd,
        "day_notional_volume_usd": _f(ctx_row.get("dayNtlVlm")),
        "day_base_volume": _f(ctx_row.get("dayBaseVlm")),
        "premium": _f(ctx_row.get("premium")),
        "impact_pxs": [_f(x) for x in (ctx_row.get("impactPxs") or [])],
        "max_leverage": meta_row.get("maxLeverage"),
        "is_delisted": bool(meta_row.get("isDelisted")),
        "ts": _now_iso(),
    }


async def perps_universe() -> list[dict]:
    """Snapshot of every perp on Hyperliquid: marks, funding, OI, 24h volume."""
    ck = ("universe",)
    cached = _cache.get(ck, settings.PERPS_TTL_S)
    if cached is not None:
        return cached
    body = await _info({"type": "metaAndAssetCtxs"})
    if not isinstance(body, list) or len(body) != 2:
        return []
    meta, ctxs = body[0] or {}, body[1] or []
    universe = meta.get("universe") or []
    out: list[dict] = []
    for i, m in enumerate(universe):
        if i >= len(ctxs):
            break
        out.append(_parse_perp(m, ctxs[i] or {}))
    _cache.put(ck, out)
    return out


async def perp(symbol: str) -> dict | None:
    """Single perp snapshot by symbol. Convenience over `perps_universe`."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    for p in await perps_universe():
        if (p.get("symbol") or "").upper() == sym:
            return p
    return None


async def all_mids() -> dict[str, float]:
    """Mid prices for everything Hyperliquid quotes (perps + spot identifiers)."""
    ck = ("mids",)
    cached = _cache.get(ck, settings.PERPS_TTL_S)
    if cached is not None:
        return cached
    body = await _info({"type": "allMids"})
    if not isinstance(body, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in body.items():
        f = _f(v)
        if f is not None:
            out[k] = f
    _cache.put(ck, out)
    return out


async def funding_history(symbol: str, *, hours: int = 24) -> list[dict]:
    """Recent funding rates for `symbol`: [{t, funding_rate_hourly, premium}, ...].

    `hours` sets the lookback window; default 24h ≈ 24 funding intervals.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return []
    hours = max(1, int(hours))
    ck = ("funding_history", sym, hours)
    cached = _cache.get(ck, settings.PERPS_TTL_S)
    if cached is not None:
        return cached
    start_ms = _now_ms() - hours * 3600 * 1000
    body = await _info({"type": "fundingHistory", "coin": sym, "startTime": start_ms})
    if not isinstance(body, list):
        return []
    rows = [{
        "t": _ms_to_iso(r.get("time")),
        "funding_rate_hourly": _f(r.get("fundingRate")),
        "premium": _f(r.get("premium")),
    } for r in body if isinstance(r, dict)]
    rows.sort(key=lambda x: x["t"] or "")
    _cache.put(ck, rows)
    return rows


async def l2_book(symbol: str, *, depth: int = 5) -> dict | None:
    """Top-of-book order book for `symbol`. Returns {symbol, ts, bids, asks}.

    Each level is {px, sz, n}. `depth` truncates each side client-side.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    ck = ("l2_book", sym, depth)
    cached = _cache.get(ck, settings.PERPS_TTL_S)
    if cached is not None:
        return cached
    body = await _info({"type": "l2Book", "coin": sym})
    if not isinstance(body, dict):
        return None
    levels = body.get("levels") or [[], []]
    bids_raw = levels[0] if len(levels) > 0 else []
    asks_raw = levels[1] if len(levels) > 1 else []

    def _lvl(rows):
        return [{"px": _f(r.get("px")), "sz": _f(r.get("sz")), "n": r.get("n")}
                for r in (rows or [])[:depth] if isinstance(r, dict)]

    out = {
        "symbol": sym,
        "ts": _ms_to_iso(body.get("time")) or _now_iso(),
        "bids": _lvl(bids_raw),
        "asks": _lvl(asks_raw),
    }
    _cache.put(ck, out)
    return out
