"""Yahoo Finance chart API — keyless daily history for non-crypto assets.

The crypto candle cascade (`tckr.history`, Hyperliquid → CoinGecko) only covers
crypto. This module fills the non-crypto half: free, no key, no signup, daily OHLC
for US equities/ETFs, metals, energy, FX, and indices via Yahoo's public v8 chart
endpoint. It is the keyless history backstop that lets `ta_risk` / `ta_indicators`
work for MU, Gold, SPY, etc. instead of silently degrading to a same-ticker
crypto. (Stooq, the obvious alternative, now gates its CSV behind a JS
proof-of-work wall and is unusable keyless.)

Endpoint: `https://query1.finance.yahoo.com/v8/finance/chart/<sym>?interval=1d&
range=<r>` → JSON `chart.result[0].{timestamp[], indicators.quote[0].{o,h,l,c,v}}`.
A browser User-Agent is required (Yahoo 4xxs the default client UA).

`map_symbol` translates a plain ticker + Pyth-style asset class to Yahoo's
symbology:
  - equity / etf  → ticker as-is        (MU, AAPL, SPY)
  - metal         → futures GC=F / SI=F / PL=F / PA=F
  - energy        → futures CL=F (WTI) / BZ=F (Brent) / NG=F (NatGas)
  - fx            → `<pair>=X`           (EURUSD=X)
Unknown classes pass the ticker through unchanged (best-effort equity lookup).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.yahoo")

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_cache = TTLCache()

# Yahoo sends a challenge page to the default client UA; a browser UA gets JSON.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36"}

_METALS = {"XAU": "GC=F", "GOLD": "GC=F", "XAG": "SI=F", "SILVER": "SI=F",
           "XPT": "PL=F", "XPD": "PA=F"}
_ENERGY = {"WTI": "CL=F", "USOIL": "CL=F", "BRENT": "BZ=F", "UKOIL": "BZ=F",
           "NATGAS": "NG=F"}

# Commodity tickers Pyth's catalog does NOT carry (so `pyth.resolve_asset`
# returns None for them) but Yahoo does. Used as a classification *fallback* so
# these still route to Yahoo instead of the crypto cascade. Kept tight and
# commodity-canonical to avoid hijacking a same-named crypto token; HL-universe
# and Pyth-crypto checks run BEFORE this fallback, so crypto majors are safe.
_FALLBACK_CLASS = {t: "energy" for t in _ENERGY}


def fallback_asset_class(ticker: str) -> str | None:
    """Non-crypto class for a commodity ticker Pyth lacks (e.g. WTI), else None."""
    return _FALLBACK_CLASS.get((ticker or "").strip().upper())


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _range_for(days: int) -> str:
    """Smallest Yahoo range string that covers `days` daily bars."""
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 365:
        return "1y"
    return "2y"


def map_symbol(ticker: str, asset_class: str | None = None) -> str | None:
    """Translate a plain ticker (+ optional Pyth-style asset class) to a Yahoo
    symbol. Returns None when the ticker is unusable."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    # Caller already gave a Yahoo-native symbol (GC=F, ^GSPC, EURUSD=X, BRK-B).
    if any(ch in t for ch in ("=", "^")) or "-" in ticker:
        return ticker.strip()
    cls = (asset_class or "").strip().lower()
    if t in _METALS or cls == "metal":
        return _METALS.get(t, f"{t}=F")
    if t in _ENERGY or cls in ("energy", "commodities", "commodity"):
        return _ENERGY.get(t, f"{t}=F")
    if cls == "fx" or (len(t) == 6 and t.isalpha() and asset_class is None and
                       t[3:] in ("USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD")):
        return f"{t}=X"
    # equity / etf / rates / unknown → ticker as-is
    return t


def _parse_chart(body: dict, days: int) -> list[dict] | None:
    """Parse a Yahoo v8 chart payload into `[{t,o,h,l,c,v}]` oldest-first."""
    try:
        result = (((body or {}).get("chart") or {}).get("result") or [])
        if not result:
            return None
        res = result[0]
        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs, lows = q.get("open") or [], q.get("high") or [], q.get("close") or []
        closes, vols = q.get("close") or [], q.get("volume") or []
    except (AttributeError, IndexError, TypeError):
        return None
    bars: list[dict] = []
    for i, t in enumerate(ts):
        c = _f(closes[i]) if i < len(closes) else None
        if c is None:  # holiday / gap row — skip
            continue
        try:
            iso = datetime.fromtimestamp(int(t), tz=UTC).date().isoformat()
        except (TypeError, ValueError, OSError):
            iso = None
        bars.append({
            "t": iso,
            "o": _f(q.get("open", [])[i]) if i < len(q.get("open") or []) else None,
            "h": _f(q.get("high", [])[i]) if i < len(q.get("high") or []) else None,
            "l": _f(q.get("low", [])[i]) if i < len(q.get("low") or []) else None,
            "c": c,
            "v": _f(vols[i]) if i < len(vols) else 0.0,
        })
    if not bars:
        return None
    return bars[-days:]


async def daily(yahoo_symbol: str, *, days: int = 90) -> list[dict] | None:
    """Daily OHLC bars for a Yahoo symbol, oldest-first. None on miss."""
    sym = (yahoo_symbol or "").strip()
    if not sym or not _http.safe_path_segment(sym):
        return None
    days = max(1, int(days))
    rng = _range_for(days)
    ck = ("daily", sym.upper(), rng)

    async def _fetch() -> list[dict] | None:
        body = await _http.get_json(
            f"{_BASE}/{sym}", params={"interval": "1d", "range": rng},
            headers=_HEADERS, label=f"yahoo chart {sym}")
        if not isinstance(body, dict):
            return None
        return _parse_chart(body, days=365 * 5)  # cache full range; slice on read

    bars = await _cache.cached(ck, settings.YAHOO_TTL_S, _fetch)
    if not bars:
        return None
    return bars[-days:]


async def spot(ticker: str, asset_class: str | None = None) -> dict | None:
    """Latest spot price for a non-crypto ticker via the chart `meta`
    (regularMarketPrice, falling back to the last close). Returns
    `{symbol, price, source:'yahoo', asset_class, yahoo_symbol}` or None.

    Used for assets Pyth's oracle doesn't carry (e.g. WTI crude) so `quote`
    can still answer keyless instead of dropping to the crypto cascade."""
    sym = map_symbol(ticker, asset_class)
    if not sym or not _http.safe_path_segment(sym):
        return None
    ck = ("spot", sym.upper())

    async def _fetch() -> float | None:
        body = await _http.get_json(
            f"{_BASE}/{sym}", params={"interval": "1d", "range": "5d"},
            headers=_HEADERS, label=f"yahoo spot {sym}")
        if not isinstance(body, dict):
            return None
        try:
            res = body["chart"]["result"][0]
            px = (res.get("meta") or {}).get("regularMarketPrice")
        except (KeyError, IndexError, TypeError):
            px = None
        if px is None:
            bars = _parse_chart(body, days=5)
            px = bars[-1]["c"] if bars else None
        return _f(px)

    px = await _cache.cached(ck, settings.YAHOO_SPOT_TTL_S, _fetch)
    if px is None:
        return None
    return {"symbol": ticker.strip().upper(), "price": px, "source": "yahoo",
            "asset_class": asset_class, "yahoo_symbol": sym}


async def history(ticker: str, *, asset_class: str | None = None,
                  days: int = 90) -> dict | None:
    """Daily OHLC for a plain ticker, resolved via `map_symbol`. Returns
    `{symbol, interval, candles:[{t,o,h,l,c,v}], source:'yahoo', asset_class,
    yahoo_symbol}` or None if the symbol can't be mapped / has no data."""
    sym = map_symbol(ticker, asset_class)
    if not sym:
        return None
    bars = await daily(sym, days=days)
    if not bars:
        return None
    return {
        "symbol": ticker.strip().upper(),
        "interval": "1d",
        "candles": bars,
        "source": "yahoo",
        "asset_class": asset_class,
        "yahoo_symbol": sym,
    }
