"""Coinalyze API — cross-exchange perps funding, open interest, liquidations.

Coinalyze aggregates perps data across Binance, Bybit, OKX, Bitget, dYdX,
Hyperliquid, and others into a single uniform feed. The killer use case is
**cross-exchange funding spread**: if BTC funding is +30% APR on Binance but
-10% on Hyperliquid, that's a structural sentiment dispersion worth knowing
about — and no single-exchange feed will tell you.

Coinalyze symbols are full instrument codes like `BTCUSD_PERP.A` where the
suffix after the dot is the exchange code (A=Binance, 6=Bybit, etc). This
module's `funding_aggregate(base)` and `open_interest_aggregate(base)` helpers
discover all perp markets across exchanges for a given base coin and roll up
the cross-venue picture for you, so callers can stay in the "BTC" namespace.

Auth: free API key (no card, free signup at coinalyze.net). Passed via
`?api_key=...` query param. If `COINALYZE_API_KEY` is not set, every public
function returns [] / None and logs a warning once per call.

Docs: https://api.coinalyze.net/v1/doc/
"""
from __future__ import annotations

import logging
import statistics
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.coinalyze")

_BASE = "https://api.coinalyze.net/v1"
_cache = TTLCache()

# Exchange code → display name, mirroring the authoritative /exchanges
# endpoint (fetched 2026-06-10). Unknown codes fall back to the raw code.
_KNOWN_EXCHANGES: dict[str, str] = {
    "A": "Binance",
    "6": "Bybit",
    "3": "OKX",
    "0": "BitMEX",
    "2": "Deribit",
    "4": "Huobi",
    "7": "Phemex",
    "8": "dYdX",
    "B": "Bitstamp",
    "C": "Coinbase",
    "D": "Bitforex",
    "E": "MercadoBitcoin",
    "F": "Bitfinex",
    "G": "Gemini",
    "H": "Hyperliquid",
    "I": "Bit2c",
    "J": "Luno",
    "K": "Kraken",
    "L": "BitFlyer",
    "M": "BtcMarkets",
    "N": "Independent Reserve",
    "P": "Poloniex",
    "S": "Aster",
    "T": "Lighter",
    "U": "Bithumb",
    "V": "Vertex",
    "W": "WOO X",
    "Y": "Gate.io",
}

# Coinalyze reports funding `value` in PERCENT per the exchange's native
# funding interval (verified live against OKX: value == exchange fraction
# * 100). Most venues fund every 8h; the perp DEXes fund hourly.
_FUNDING_INTERVAL_H: dict[str, int] = {
    "H": 1,   # Hyperliquid
    "8": 1,   # dYdX
    "K": 1,   # Kraken (continuous, quoted per hour)
    "V": 1,   # Vertex
    "T": 1,   # Lighter
}
_DEFAULT_FUNDING_INTERVAL_H = 8


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now_unix() -> int:
    return int(datetime.now(UTC).timestamp())


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ts_to_iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _exchange_name(code: str) -> str:
    return _KNOWN_EXCHANGES.get(code, code)


def _symbol_parts(symbol: str) -> tuple[str, str]:
    """`BTCUSD_PERP.A` → ('BTCUSD_PERP', 'A'). Returns ('', '') on malformed."""
    if not symbol or "." not in symbol:
        return "", ""
    base, _, ex = symbol.rpartition(".")
    return base, ex


async def _get(path: str, *, params: dict | None = None, label: str = "",
               ttl_s: int | None = None) -> object | None:
    """Shared GET helper that adds the API key and caches by (path, params)."""
    if not settings.COINALYZE_API_KEY:
        log.warning("COINALYZE_API_KEY not set — coinalyze.%s skipped", label or path)
        return None
    params = dict(params or {})
    params.setdefault("api_key", settings.COINALYZE_API_KEY)
    # Cache key omits the api_key so a future key rotation doesn't blow the cache.
    ck = (path, tuple(sorted((k, v) for k, v in params.items() if k != "api_key")))
    if ttl_s is not None:
        cached = _cache.get(ck, ttl_s)
        if cached is not None:
            return cached
    body = await _http.get_json(f"{_BASE}/{path}", params=params,
                                label=label or f"coinalyze {path}")
    if body is not None and ttl_s is not None:
        _cache.put(ck, body)
    return body


# --------------------------- exchanges + markets ---------------------------

async def exchanges() -> list[dict]:
    """All exchanges Coinalyze tracks. Mostly internal, but exposed for callers
    that want to surface the raw venue list."""
    body = await _get("exchanges", label="coinalyze exchanges",
                      ttl_s=settings.TOKEN_METADATA_TTL_S)
    if not isinstance(body, list):
        return []
    return [
        {"code": r.get("code"), "name": r.get("name")}
        for r in body if isinstance(r, dict)
    ]


async def markets(base: str | None = None) -> list[dict]:
    """All perp/futures markets. Filter by `base` to narrow to one coin.

    Returns rows with: {symbol, exchange_code, exchange_name, base_asset,
    quote_asset, market_type, is_perpetual, expire_at?, oi_lot_size, contract_size}.
    """
    body = await _get("future-markets", label="coinalyze markets",
                      ttl_s=settings.TOKEN_METADATA_TTL_S)
    if not isinstance(body, list):
        return []
    rows: list[dict] = []
    target = (base or "").strip().upper() or None
    for r in body:
        if not isinstance(r, dict):
            continue
        sym = r.get("symbol")
        base_asset = (r.get("base_asset") or "").upper()
        if target and base_asset != target:
            continue
        code = r.get("exchange") or _symbol_parts(sym)[1]
        rows.append({
            "symbol": sym,
            "exchange_code": code,
            "exchange_name": _exchange_name(code),
            "base_asset": base_asset,
            "quote_asset": (r.get("quote_asset") or "").upper(),
            "market_type": r.get("type"),
            "is_perpetual": bool(r.get("is_perpetual")),
            "expire_at": _ts_to_iso(r.get("expire_at")) if r.get("expire_at") else None,
            "oi_lot_size": _f(r.get("oi_lot_size")),
            "contract_size": _f(r.get("contract_size")),
        })
    return rows


# --------------------------- funding rate ---------------------------

def _parse_funding_row(r: dict) -> dict:
    sym = r.get("symbol")
    _, code = _symbol_parts(sym or "")
    pct = _f(r.get("value"))  # percent per funding interval
    interval_h = _FUNDING_INTERVAL_H.get(code, _DEFAULT_FUNDING_INTERVAL_H)
    apr_pct = (pct * (24 / interval_h) * 365) if pct is not None else None
    return {
        "symbol": sym,
        "exchange_code": code,
        "exchange_name": _exchange_name(code),
        "funding_rate_pct": pct,
        "funding_interval_hours": interval_h,
        "funding_apr_pct": apr_pct,
        "update_iso": _ts_to_iso(r.get("update")),
    }


async def funding_rate(symbols: str | list[str]) -> list[dict]:
    """Current funding for one or more full instrument symbols (e.g. `BTCUSD_PERP.A`)."""
    syms = [symbols] if isinstance(symbols, str) else list(symbols)
    syms = [s.strip() for s in syms if s and s.strip()]
    if not syms:
        return []
    body = await _get("funding-rate", params={"symbols": ",".join(syms)},
                      label="coinalyze funding-rate",
                      ttl_s=settings.PERPS_TTL_S)
    if not isinstance(body, list):
        return []
    return [_parse_funding_row(r) for r in body if isinstance(r, dict)]


async def predicted_funding(symbols: str | list[str]) -> list[dict]:
    """Predicted next funding for one or more symbols."""
    syms = [symbols] if isinstance(symbols, str) else list(symbols)
    syms = [s.strip() for s in syms if s and s.strip()]
    if not syms:
        return []
    body = await _get("predicted-funding-rate", params={"symbols": ",".join(syms)},
                      label="coinalyze predicted-funding",
                      ttl_s=settings.PERPS_TTL_S)
    if not isinstance(body, list):
        return []
    return [_parse_funding_row(r) for r in body if isinstance(r, dict)]


async def funding_history(symbol: str, *, interval: str = "1hour",
                          hours: int = 24) -> list[dict]:
    """Historical funding for one full symbol. Returns chronological list of
    {t, open, high, low, close} funding rate per bin, in PERCENT per the
    exchange's native funding interval (Coinalyze's raw unit)."""
    sym = (symbol or "").strip()
    if not sym:
        return []
    hours = max(1, min(int(hours), 24 * 30))
    to_ts = _now_unix()
    from_ts = to_ts - hours * 3600
    body = await _get(
        "funding-rate-history",
        params={"symbols": sym, "interval": interval, "from": from_ts, "to": to_ts},
        label=f"coinalyze funding-history {sym}",
        ttl_s=settings.PERPS_TTL_S,
    )
    if not isinstance(body, list) or not body:
        return []
    history = body[0].get("history") if isinstance(body[0], dict) else None
    rows: list[dict] = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        rows.append({
            "t": _ts_to_iso(h.get("t")),
            "open": _f(h.get("o")),
            "high": _f(h.get("h")),
            "low": _f(h.get("l")),
            "close": _f(h.get("c")),
        })
    rows.sort(key=lambda x: x["t"] or "")
    return rows


# --------------------------- open interest ---------------------------

def _parse_oi_row(r: dict) -> dict:
    sym = r.get("symbol")
    _, code = _symbol_parts(sym or "")
    return {
        "symbol": sym,
        "exchange_code": code,
        "exchange_name": _exchange_name(code),
        "open_interest_usd": _f(r.get("value")),
        "update_iso": _ts_to_iso(r.get("update")),
    }


async def open_interest(symbols: str | list[str], *, convert_to_usd: bool = True) -> list[dict]:
    """Current open interest. `convert_to_usd=True` normalizes everything to USD."""
    syms = [symbols] if isinstance(symbols, str) else list(symbols)
    syms = [s.strip() for s in syms if s and s.strip()]
    if not syms:
        return []
    body = await _get(
        "open-interest",
        params={"symbols": ",".join(syms),
                "convert_to_usd": "true" if convert_to_usd else "false"},
        label="coinalyze open-interest",
        ttl_s=settings.PERPS_TTL_S,
    )
    if not isinstance(body, list):
        return []
    return [_parse_oi_row(r) for r in body if isinstance(r, dict)]


# --------------------------- liquidations ---------------------------

async def liquidations(symbols: str | list[str], *, interval: str = "1hour",
                       hours: int = 24) -> list[dict]:
    """Liquidation aggregates per symbol per interval. Chronological per symbol.

    Each bin row: {symbol, t, long_liquidations_usd, short_liquidations_usd}.
    Big spikes in long_liquidations_usd often mark capitulation lows; the reverse
    for short_liquidations_usd marks short squeezes.
    """
    syms = [symbols] if isinstance(symbols, str) else list(symbols)
    syms = [s.strip() for s in syms if s and s.strip()]
    if not syms:
        return []
    hours = max(1, min(int(hours), 24 * 30))
    to_ts = _now_unix()
    from_ts = to_ts - hours * 3600
    body = await _get(
        "liquidation-history",
        params={"symbols": ",".join(syms), "interval": interval,
                "from": from_ts, "to": to_ts, "convert_to_usd": "true"},
        label="coinalyze liquidation-history",
        ttl_s=settings.LIQUIDATION_TTL_S,
    )
    if not isinstance(body, list):
        return []
    rows: list[dict] = []
    for r in body:
        if not isinstance(r, dict):
            continue
        sym = r.get("symbol")
        for h in r.get("history") or []:
            if not isinstance(h, dict):
                continue
            rows.append({
                "symbol": sym,
                "t": _ts_to_iso(h.get("t")),
                "long_liquidations_usd": _f(h.get("l")),
                "short_liquidations_usd": _f(h.get("s")),
            })
    rows.sort(key=lambda x: (x["symbol"] or "", x["t"] or ""))
    return rows


# --------------------------- cross-exchange aggregates ---------------------------

async def funding_aggregate(base: str) -> dict | None:
    """Cross-exchange funding spread for one coin. THE killer use case.

    Discovers every perp market for `base` across exchanges, queries current
    funding, and rolls up: {base, per_exchange: [...], aggregate: {min, max,
    median, mean, spread_pct, n_exchanges, ts}}. `spread_pct` is the gap between
    the most-positive and most-negative funding APR — a wide spread implies
    structural sentiment dispersion across venues.
    """
    target = (base or "").strip().upper()
    if not target:
        return None
    ck = ("funding_aggregate", target)
    cached = _cache.get(ck, settings.FUNDING_AGG_TTL_S)
    if cached is not None:
        return cached

    mkts = await markets(base=target)
    perp_syms = [m["symbol"] for m in mkts if m.get("is_perpetual") and m.get("symbol")]
    if not perp_syms:
        return None
    rates = await funding_rate(perp_syms)
    if not rates:
        return None
    aprs = [r["funding_apr_pct"] for r in rates if r.get("funding_apr_pct") is not None]
    if not aprs:
        return None
    agg = {
        "min_apr_pct": min(aprs),
        "max_apr_pct": max(aprs),
        "median_apr_pct": statistics.median(aprs),
        "mean_apr_pct": sum(aprs) / len(aprs),
        "spread_apr_pct": max(aprs) - min(aprs),
        "n_exchanges": len(aprs),
        "ts": _now_iso(),
    }
    # Sort per-exchange most-positive-first for human readability.
    rates_sorted = sorted(
        rates,
        key=lambda r: (r.get("funding_apr_pct") if r.get("funding_apr_pct") is not None else 0),
        reverse=True,
    )
    out = {"base": target, "per_exchange": rates_sorted, "aggregate": agg}
    _cache.put(ck, out)
    return out


async def open_interest_aggregate(base: str) -> dict | None:
    """Cross-exchange OI summary for one coin. Same shape as funding_aggregate
    but for open interest. Useful for "is OI being added on Binance while
    being unwound on Bybit" reads."""
    target = (base or "").strip().upper()
    if not target:
        return None
    ck = ("oi_aggregate", target)
    cached = _cache.get(ck, settings.FUNDING_AGG_TTL_S)
    if cached is not None:
        return cached

    mkts = await markets(base=target)
    perp_syms = [m["symbol"] for m in mkts if m.get("is_perpetual") and m.get("symbol")]
    if not perp_syms:
        return None
    ois = await open_interest(perp_syms)
    if not ois:
        return None
    vals = [r["open_interest_usd"] for r in ois if r.get("open_interest_usd") is not None]
    if not vals:
        return None
    total = sum(vals)
    agg = {
        "total_open_interest_usd": total,
        "n_exchanges": len(vals),
        "top_exchange_share_pct": (max(vals) / total * 100.0) if total > 0 else None,
        "ts": _now_iso(),
    }
    ois_sorted = sorted(ois, key=lambda r: r.get("open_interest_usd") or 0, reverse=True)
    out = {"base": target, "per_exchange": ois_sorted, "aggregate": agg}
    _cache.put(ck, out)
    return out


async def funding_extremes(bases: list[str] | None = None, *, top_n: int = 10) -> dict:
    """Discovery tool: scan a list of bases (default majors) and return the
    biggest funding outliers across exchanges. Returns:
        {most_positive: [...], most_negative: [...], biggest_spread: [...]}
    each as a list of `funding_aggregate` results sorted by the relevant metric.

    Default `bases` covers the highly-traded universe to keep the scan fast
    (~30 contracts × 1 API call each). Pass your own list to target a watchlist.
    """
    if not bases:
        bases = ["BTC", "ETH", "SOL", "BNB", "AVAX", "MATIC", "ARB", "OP",
                 "SUI", "TON", "TRX", "DOT", "NEAR", "APT", "ADA", "LINK",
                 "ATOM", "INJ", "DOGE", "PEPE"]
    out_aggs: list[dict] = []
    for base in bases:
        agg = await funding_aggregate(base)
        if agg:
            out_aggs.append(agg)
    if not out_aggs:
        return {"most_positive": [], "most_negative": [], "biggest_spread": []}
    by_max = sorted(out_aggs, key=lambda a: a["aggregate"]["max_apr_pct"], reverse=True)[:top_n]
    by_min = sorted(out_aggs, key=lambda a: a["aggregate"]["min_apr_pct"])[:top_n]
    by_spread = sorted(out_aggs, key=lambda a: a["aggregate"]["spread_apr_pct"], reverse=True)[:top_n]
    return {
        "most_positive": by_max,
        "most_negative": by_min,
        "biggest_spread": by_spread,
        "ts": _now_iso(),
    }
