"""Unified crypto candle/history cascade.

Like `tckr.quotes` but for time-series. Consumers that want "30 days of daily
closes for symbol X" don't have to know which provider has it or which is
currently rate-limited.

Cascade order (per symbol):

  1. CoinGecko `market_chart` — broadest coverage (resolves via
     `coin_id_from_symbol`), but rate-limits hard on the free tier.
  2. Hyperliquid `candles` — ~230 perps, cheap and rarely throttled.

Returned shape per resolved symbol:

    {
      "symbol":  "NEAR",
      "interval": "1d",
      "closes":  [2.51, 2.63, 2.71, ...],
      "volumes": [18_234_000.0, ...],   # may be empty if source lacks volume
      "source":  "coingecko" | "hyperliquid",
    }

Volumes carry whatever the source provides:
  - CoinGecko returns USD total_volumes
  - Hyperliquid returns base-asset volume (NOT USD)
Inspect `source` if you need to interpret volume scale.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from tckr import coingecko as cg
from tckr import hyperliquid as hl

log = logging.getLogger("tckr.history")


def _downsample_to_daily(rows: list) -> dict[str, float]:
    """Group `[[ts_ms, value], ...]` to one value per UTC day (last wins)."""
    out: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, list) or len(row) < 2:
            continue
        try:
            ts_ms = int(row[0])
            v = float(row[1])
        except (TypeError, ValueError):
            continue
        day = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()
        out[day] = v  # later points overwrite — gives us the day's close
    return out


async def _cg_history(symbol: str, days: int) -> tuple[list[float], list[float]] | None:
    cid = await cg.coin_id_from_symbol(symbol)
    if not cid:
        return None
    # CoinGecko returns hourly data for days<91 on the free tier (interval=daily
    # is paid-tier only). Always downsample to daily by UTC date so the cascade
    # contract ("daily closes") holds regardless of which CG tier we're on.
    body = await cg.market_chart(cid, days=days)
    if not isinstance(body, dict):
        return None
    daily_close = _downsample_to_daily(body.get("prices") or [])
    daily_vol = _downsample_to_daily(body.get("total_volumes") or [])
    if not daily_close:
        return None
    ordered_days = sorted(daily_close.keys())
    closes = [daily_close[d] for d in ordered_days]
    volumes = [daily_vol.get(d, 0.0) for d in ordered_days]
    return closes[-days:], volumes[-days:]


async def _hl_history(symbol: str, days: int) -> tuple[list[float], list[float]] | None:
    r = await hl.candles(symbol, interval="1d", limit=days)
    if not r:
        return None
    closes_all = [c.get("c") for c in r.get("candles") or [] if c.get("c") is not None]
    vols_all = [c.get("v") or 0.0 for c in r.get("candles") or []]
    if not closes_all:
        return None
    return closes_all[-days:], vols_all[-days:]


async def candles(symbols: list[str] | str, *, days: int = 30) -> dict[str, dict]:
    """Resolve daily history for `symbols`, cascading CoinGecko → Hyperliquid.

    Returns `{symbol: {symbol, interval, closes, volumes, source}}`. Symbols
    no source could resolve are absent.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}
    days = max(1, int(days))

    out: dict[str, dict] = {}

    async def _one(sym: str) -> None:
        r = await _cg_history(sym, days)
        if r is not None:
            closes, volumes = r
            out[sym] = {"symbol": sym, "interval": "1d",
                        "closes": closes, "volumes": volumes,
                        "source": "coingecko"}
            return
        r = await _hl_history(sym, days)
        if r is not None:
            closes, volumes = r
            out[sym] = {"symbol": sym, "interval": "1d",
                        "closes": closes, "volumes": volumes,
                        "source": "hyperliquid"}
            return
        log.debug("history: unresolved %s", sym)

    await asyncio.gather(*(_one(s) for s in syms))
    return out


async def candles_one(symbol: str, *, days: int = 30) -> dict | None:
    """Single-symbol convenience over `candles([symbol])`."""
    d = await candles([symbol], days=days)
    return d.get(symbol.strip().upper()) if symbol else None
