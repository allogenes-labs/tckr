"""Hyperliquid info API — perps marks, funding, open interest, order books, candles.

Free, no API key. Single POST /info endpoint with a {"type": ...} payload.
This module exposes the market-data subset (no user/account queries).

Conventions:
- Symbols are the perp "coin" names ("BTC", "ETH", "SOL", ...).
- Upstream numbers are strings; parsers cast to float.
- Funding rate is per-hour (Hyperliquid charges hourly). `funding_apr_pct` is
  the linear-annualization convenience field.

Failure modes & coverage:
- Coverage is the ~230 tokens listed as perps — majors + most active mid-caps.
  Long-tail alts (a token that only has a DEX pool) are NOT here; route those
  to `geckoterminal.pool_ohlcv` instead.
- No observed rate limit at typical reading volume (10-30 req/sec is fine).
  HL is the canonical free-tier fallback when CoinGecko is 429-ing — both
  `tckr.quotes` and `tckr.history` cascade through HL when CG fails.
- Unknown symbol on `candleSnapshot` returns HTTP 500 (not 404). Our wrapper
  treats both the same: callers get None.

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.hyperliquid")

_BASE = "https://api.hyperliquid.xyz"
_cache = TTLCache()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()
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


_INTERVAL_MS = {
    "1m":  60_000,         "3m":  180_000,        "5m":  300_000,
    "15m": 900_000,        "30m": 1_800_000,
    "1h":  3_600_000,      "2h":  7_200_000,      "4h":  14_400_000,
    "8h":  28_800_000,     "12h": 43_200_000,
    "1d":  86_400_000,     "3d":  259_200_000,    "1w":  604_800_000,    "1M": 2_592_000_000,
}


async def candles(
    symbol: str,
    *,
    interval: str = "1d",
    limit: int = 30,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict | None:
    """Hyperliquid candle history for `symbol`.

    Wraps the `/info` `candleSnapshot` payload. Returns a dict shaped to match
    `geckoterminal.pool_ohlcv` so cascade callers can swap between sources:

        {"symbol": "BTC", "interval": "1d", "candles": [
            {"t": "2026-04-26T00:00:00+00:00",
             "o": 66231.0, "h": 67120.0, "l": 65890.5, "c": 67005.0, "v": 18234.4},
            ...
        ]}

    `interval`: one of {1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d, 3d, 1w, 1M}.
    `limit`: number of candles to ask for (counted back from `end_ms`); ignored
             if both `start_ms` and `end_ms` are supplied.

    No auth, no observed rate limit at typical reading volume. The series is
    chronological (oldest → newest). Returns None on HTTP failure; empty
    candle list when the symbol is unknown to HL.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    interval = (interval or "1d").strip()
    if interval not in _INTERVAL_MS:
        log.warning("hyperliquid candles: unsupported interval %r", interval)
        return None
    ms_per = _INTERVAL_MS[interval]
    if end_ms is None:
        end_ms = _now_ms()
    if start_ms is None:
        # +1 buffer candle so we don't drop the most recent one on partial bar.
        start_ms = end_ms - (max(1, int(limit)) + 1) * ms_per

    ck = ("candles", sym, interval, int(start_ms), int(end_ms))
    cached = _cache.get(ck, settings.PERPS_TTL_S)
    if cached is not None:
        return cached

    body = await _info({
        "type": "candleSnapshot",
        "req": {"coin": sym, "interval": interval,
                "startTime": int(start_ms), "endTime": int(end_ms)},
    }, label=f"hyperliquid candleSnapshot {sym} {interval}")
    if not isinstance(body, list):
        return None

    rows: list[dict] = []
    for r in body:
        if not isinstance(r, dict):
            continue
        t_iso = _ms_to_iso(r.get("t"))
        if t_iso is None:
            continue
        rows.append({
            "t": t_iso,
            "o": _f(r.get("o")),
            "h": _f(r.get("h")),
            "l": _f(r.get("l")),
            "c": _f(r.get("c")),
            "v": _f(r.get("v")),
        })
    rows.sort(key=lambda x: x["t"])  # ensure chronological

    out = {"symbol": sym, "interval": interval, "candles": rows}
    _cache.put(ck, out)
    return out


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
