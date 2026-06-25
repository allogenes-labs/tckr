"""Unified crypto candle/history cascade.

Like `tckr.quotes` but for time-series. Consumers that want "30 days of daily
closes for symbol X" don't have to know which provider has it or which is
currently rate-limited.

Cascade order (per symbol):

  1. Hyperliquid `candles` — preferred for the ~230 perps in HL's universe.
     No rate limit, true daily OHLC, fresher than CG free-tier history.
  2. CoinGecko `market_chart` — broadest coverage (resolves via
     `coin_id_from_symbol`), used for long-tail symbols HL doesn't list
     and as a backstop when HL returns nothing for a covered symbol.

Returned shape per resolved symbol:

    {
      "symbol":  "NEAR",
      "interval": "1d",
      "closes":  [2.51, 2.63, 2.71, ...],
      "volumes": [18_234_000.0, ...],   # USD; may be empty if source lacks volume
      "source":  "coingecko" | "hyperliquid",
    }

Volume units are USD regardless of source: CoinGecko returns USD total_volumes
directly; Hyperliquid base-asset volume is multiplied by the bar's close so
the units line up. This means `volume_last`/`volume_avg_20d` are comparable
across symbols even when the cascade picks different sources for each.

For full daily OHLC (open/high/low, needed for range indicators like ATR) use
`ohlc` / `ohlc_one` instead of `candles`. Those are Hyperliquid-only (HL returns
true OHLC; CoinGecko `market_chart` is closes-only) so they cover the ~230 HL
perps and leave the long tail to the closes-only `candles` cascade.
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
    rows = r.get("candles") or []
    closes_all: list[float] = []
    vols_all: list[float] = []
    for c in rows:
        close = c.get("c")
        if close is None:
            continue
        closes_all.append(float(close))
        # HL `v` is base-asset volume; convert to USD using the bar close so
        # callers get comparable volume scales across mixed-source symbols.
        base_vol = c.get("v") or 0.0
        vols_all.append(float(base_vol) * float(close))
    if not closes_all:
        return None
    return closes_all[-days:], vols_all[-days:]


async def _hl_universe_syms() -> set[str]:
    """Set of uppercase symbols HL has perp coverage for."""
    perps = await hl.perps_universe()
    return {(p.get("symbol") or "").upper()
            for p in (perps or [])
            if p.get("symbol")}


async def candles(symbols: list[str] | str, *, days: int = 30) -> dict[str, dict]:
    """Resolve daily history for `symbols`, cascading Hyperliquid → CoinGecko.

    Returns `{symbol: {symbol, interval, closes, volumes, source}}`. Symbols
    no source could resolve are absent. Volumes are USD for both sources
    (HL base-asset volume is normalized to USD via the bar close).
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}
    days = max(1, int(days))

    hl_syms = await _hl_universe_syms()
    out: dict[str, dict] = {}

    async def _one(sym: str) -> None:
        if sym in hl_syms:
            r = await _hl_history(sym, days)
            if r is not None:
                closes, volumes = r
                out[sym] = {"symbol": sym, "interval": "1d",
                            "closes": closes, "volumes": volumes,
                            "source": "hyperliquid"}
                return
            # HL covers it but returned nothing — fall through.
        r = await _cg_history(sym, days)
        if r is not None:
            closes, volumes = r
            out[sym] = {"symbol": sym, "interval": "1d",
                        "closes": closes, "volumes": volumes,
                        "source": "coingecko"}
            return
        log.debug("history: unresolved %s", sym)

    await asyncio.gather(*(_one(s) for s in syms))
    return out


async def candles_one(symbol: str, *, days: int = 30) -> dict | None:
    """Single-symbol convenience over `candles([symbol])`."""
    d = await candles([symbol], days=days)
    return d.get(symbol.strip().upper()) if symbol else None


async def ohlc(symbols: list[str] | str, *, days: int = 30) -> dict[str, dict]:
    """Daily **OHLC** bars for `symbols`, from sources that expose full candles.

    Like `candles` but preserves open/high/low (not just closes) — needed for
    range indicators such as ATR. Returns
    `{symbol: {symbol, interval, candles: [{t, o, h, l, c, v}, ...], source}}`
    (same shape as `hyperliquid.candles` / `geckoterminal.pool_ohlcv`).

    Sourced from Hyperliquid, which returns true daily OHLC for its ~230-symbol
    perp universe. Symbols HL doesn't cover are **absent** — CoinGecko's
    `market_chart` (the `candles` fallback) is closes-only, so there is no clean
    daily-OHLC fallback for the long tail; use `candles` when you only need
    closes. Volume `v` is USD (HL base-asset volume * bar close), matching the
    `candles` cascade's volume convention.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}
    days = max(1, int(days))

    hl_syms = await _hl_universe_syms()
    out: dict[str, dict] = {}

    async def _one(sym: str) -> None:
        if sym not in hl_syms:
            return  # no full-OHLC source covers this symbol — caller uses `candles`
        r = await hl.candles(sym, interval="1d", limit=days)
        rows = (r or {}).get("candles") or []
        bars: list[dict] = []
        for c in rows:
            close = c.get("c")
            if close is None:
                continue
            base_vol = c.get("v") or 0.0
            bars.append({
                "t": c.get("t"),
                "o": c.get("o"),
                "h": c.get("h"),
                "l": c.get("l"),
                "c": float(close),
                "v": float(base_vol) * float(close),  # USD, matches `candles`
            })
        if bars:
            out[sym] = {"symbol": sym, "interval": "1d",
                        "candles": bars[-days:], "source": "hyperliquid"}

    await asyncio.gather(*(_one(s) for s in syms))
    return out


async def ohlc_one(symbol: str, *, days: int = 30) -> dict | None:
    """Single-symbol convenience over `ohlc([symbol])`. None if no OHLC source
    covers the symbol (fall back to `candles_one` for closes)."""
    d = await ohlc([symbol], days=days)
    return d.get(symbol.strip().upper()) if symbol else None
