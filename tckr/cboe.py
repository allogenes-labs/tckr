"""CBOE delayed options — keyless US equity/ETF/index option chains with greeks.

The zero-signup counterpart to `tckr.options` (Alpaca). CBOE publishes a public
delayed-quote JSON feed that powers cboe.com's quote-table pages; it carries
per-contract bid/ask, IV, the full greek set (delta/gamma/theta/vega/rho), a
theoretical value, AND open interest + volume — the last two are a bonus Alpaca's
data snapshot doesn't provide.

No API key. Data is delayed ~15 minutes. This is an *unofficial* endpoint (the
backing for cboe.com quote pages) — no SLA, and the shape could change without
notice — so treat it as a best-effort keyless fallback, not a system of record.
For anything load-bearing, prefer `tckr.options` (Alpaca) or a paid feed.

Endpoint (one call returns the ENTIRE chain — no server-side filtering):
    https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json
Equities/ETFs use the bare ticker; CBOE indices take a leading underscore
(`_SPX`, `_VIX`, `_NDX`, `_RUT`, ...). Because the payload is large (AAPL ~1.6MB,
SPX ~13MB) the full parsed chain is cached per underlying and all filtering
(expiration / type / strike) happens client-side.

Contract symbols are OCC-format; this module reuses the OCC parser from
`tckr.options` and emits the same flattened row shape so a caller can swap
between the two sources (or cascade them) without reshaping.

Index coverage: SPX/VIX/NDX/RUT etc. are available here (unlike Alpaca, which is
equity/ETF only) — a second reason to keep CBOE around as a complement.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from tckr import _http, settings
from tckr.cache import TTLCache
from tckr.options import parse_occ  # shared OCC parsing — single source of truth

log = logging.getLogger("tckr.cboe")

_BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options"
_cache = TTLCache()

# CBOE serves an S3 AccessDenied without a browser-ish UA on some edges.
_HEADERS = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}

# CBOE index roots are requested with a leading underscore; equities are bare.
_INDEX_ROOTS = {
    "SPX", "SPXW", "VIX", "VIXW", "NDX", "RUT", "MRUT", "DJX",
    "OEX", "XEO", "XSP", "NANOS",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def _us_market_date() -> date:
    """Today in US/Eastern — mirrors `tckr.options._us_market_date`."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 — no tz database; EST approximation
        return (datetime.now(UTC) - timedelta(hours=5)).date()


def _dte(expiration: str | None) -> int | None:
    if not expiration:
        return None
    try:
        exp = date.fromisoformat(expiration)
    except ValueError:
        return None
    return (exp - _us_market_date()).days


def _path_symbol(underlying: str) -> str:
    """Map a user ticker to CBOE's request symbol (index roots get a `_`)."""
    s = (underlying or "").strip().upper().lstrip("_")
    return f"_{s}" if s in _INDEX_ROOTS else s


def _parse_contract(raw: dict) -> dict | None:
    """Flatten one CBOE option entry into the shared tckr contract row.

    Mirrors `tckr.options._parse_contract` keys, plus CBOE-only extras
    (open_interest, volume, theo).
    """
    sym = raw.get("option")
    occ = parse_occ(sym) if sym else None
    if occ is None:
        return None
    bid = _f(raw.get("bid"))
    ask = _f(raw.get("ask"))
    mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
    return {
        "symbol": occ["symbol"],
        "underlying": occ["underlying"],
        "expiration": occ["expiration"],
        "dte": _dte(occ["expiration"]),
        "type": occ["type"],
        "strike": occ["strike"],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "bid_size": _i(raw.get("bid_size")),
        "ask_size": _i(raw.get("ask_size")),
        "quote_ts": None,  # CBOE gives a chain-level timestamp, not per-quote
        "last": _f(raw.get("last_trade_price")),
        "last_size": None,
        "trade_ts": raw.get("last_trade_time"),
        "iv": _f(raw.get("iv")),
        "delta": _f(raw.get("delta")),
        "gamma": _f(raw.get("gamma")),
        "theta": _f(raw.get("theta")),
        "vega":  _f(raw.get("vega")),
        "rho":   _f(raw.get("rho")),
        # CBOE extras:
        "open_interest": _i(raw.get("open_interest")),
        "volume": _i(raw.get("volume")),
        "theo": _f(raw.get("theo")),
    }


def _sort_key(c: dict) -> tuple:
    return (c.get("expiration") or "", c.get("strike") or 0.0, c.get("type") or "")


async def _full_chain(underlying: str) -> dict | None:
    """Fetch + parse the entire CBOE chain for `underlying`, cached per symbol.

    Returns {underlying, current_price, chain_ts, contracts: [...]} or None.
    """
    sym = (underlying or "").strip().upper().lstrip("_")
    if not sym:
        return None
    ck = ("full", sym)
    cached = _cache.get(ck, settings.OPTIONS_TTL_S)
    if cached is not None:
        return cached

    # Double-checked lock: CBOE chains are large (AAPL ~1.6MB, SPX ~13MB), so a
    # thundering herd of concurrent cold-cache callers is especially costly —
    # let the first fetch run while the rest await the lock and reuse its result.
    async with _cache.lock(ck):
        cached = _cache.get(ck, settings.OPTIONS_TTL_S)
        if cached is not None:
            return cached

        body = await _http.get_json(
            f"{_BASE}/{_path_symbol(sym)}.json",
            headers=_HEADERS,
            label=f"cboe options {sym}",
        )
        if not isinstance(body, dict):
            return None
        data = body.get("data") or {}
        raw_opts = data.get("options") or []
        contracts: list[dict] = []
        for r in raw_opts:
            if isinstance(r, dict):
                row = _parse_contract(r)
                if row is not None:
                    contracts.append(row)
        contracts.sort(key=_sort_key)
        out = {
            "underlying": sym,
            "current_price": _f(data.get("current_price")),
            "chain_ts": body.get("timestamp"),
            "contracts": contracts,
        }
        _cache.put(ck, out)
        return out


# --------------------------- public surface (mirrors tckr.options) ---------------------------

async def option_chain(
    underlying: str,
    *,
    expiration: str | None = None,
    exp_gte: str | None = None,
    exp_lte: str | None = None,
    type: str | None = None,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
) -> dict | None:
    """Option chain for `underlying` with quotes + greeks + IV + OI/volume.

    Same return shape as `tckr.options.option_chain` (feed reported as
    `cboe-delayed`), with the CBOE-only `open_interest`, `volume`, and `theo`
    fields on each contract. All narrowing is applied client-side to the cached
    full chain. Pass at least `expiration` for a wide name to keep results
    readable. Returns None on upstream failure.
    """
    full = await _full_chain(underlying)
    if full is None:
        return None
    typ = (type or "").strip().lower() or None
    rows = []
    for c in full["contracts"]:
        if expiration and c["expiration"] != expiration:
            continue
        if exp_gte and (c["expiration"] or "") < exp_gte:
            continue
        if exp_lte and (c["expiration"] or "") > exp_lte:
            continue
        if typ in ("call", "put") and c["type"] != typ:
            continue
        if strike_gte is not None and (c["strike"] is None or c["strike"] < strike_gte):
            continue
        if strike_lte is not None and (c["strike"] is None or c["strike"] > strike_lte):
            continue
        rows.append(c)
    return {
        "underlying": full["underlying"],
        "feed": "cboe-delayed",
        "current_price": full.get("current_price"),
        "count": len(rows),
        "ts": _now_iso(),
        "chain_ts": full.get("chain_ts"),
        "contracts": rows,
    }


async def option_snapshot(symbols: str | list[str]) -> list[dict]:
    """Rows for one or more explicit OCC contract symbols.

    Groups the requested symbols by underlying, pulls each underlying's cached
    full chain once, and returns the matching contracts (sorted). Symbols whose
    underlying or contract isn't found are simply absent.
    """
    if isinstance(symbols, str):
        wanted = [symbols]
    else:
        wanted = list(symbols or [])
    wanted = {s.strip().upper() for s in wanted if s and s.strip()}
    if not wanted:
        return []
    # Group by underlying so we fetch each chain at most once.
    by_underlying: dict[str, set[str]] = {}
    for s in wanted:
        occ = parse_occ(s)
        if occ and occ["underlying"]:
            by_underlying.setdefault(occ["underlying"], set()).add(s)
    rows: list[dict] = []
    for und, syms in by_underlying.items():
        full = await _full_chain(und)
        if full is None:
            continue
        rows.extend(c for c in full["contracts"] if c["symbol"] in syms)
    rows.sort(key=_sort_key)
    return rows


async def expirations(underlying: str) -> dict | None:
    """Distinct expiration dates + strike range for `underlying`.

    Same shape as `tckr.options.expirations`. Derived from the cached full
    chain, so it's essentially free after a chain/expirations call.
    """
    full = await _full_chain(underlying)
    if full is None:
        return None
    exps = sorted({c["expiration"] for c in full["contracts"] if c.get("expiration")})
    strikes = [c["strike"] for c in full["contracts"] if c.get("strike") is not None]
    return {
        "underlying": full["underlying"],
        "expirations": exps,
        "strikes": {"min": min(strikes), "max": max(strikes)} if strikes else None,
        "ts": _now_iso(),
    }
