"""Alpaca options market data — US equity/ETF option chains, quotes, and greeks.

This is the equities-options analogue of the crypto perps modules: where
`hyperliquid` gives you perp marks/funding, this gives you listed option
contracts with live(-ish) quotes and model greeks (delta/gamma/theta/vega/rho)
plus implied volatility — in a single chain call.

Why this exists: it replaces the unofficial yfinance options scrape, which has
no SLA, no greeks, and rate-limits aggressively. Alpaca's options data is an
*official*, documented REST API with a free tier.

Auth: two keys via headers (`APCA-API-KEY-ID` + `APCA-API-SECRET-KEY`). Free
signup at alpaca.markets, no funding required. Set `ALPACA_API_KEY` and
`ALPACA_API_SECRET` in env.

Feed tiers:
- `indicative` (default, free) — trades are delayed ~15m and quotes are a
  modified/indicative feed. Greeks + IV are still returned.
- `opra` — real-time OPRA, requires the paid Algo Trader Plus data sub. Pass
  `feed="opra"` (or set ALPACA_OPTIONS_FEED=opra) once subscribed.

Contract symbols are OCC-format, e.g. `AAPL260619C00150000`
= AAPL, 2026-06-19 expiry, Call, strike 150.000. The parser/formatter here
turns those into structured {underlying, expiration, type, strike} and back.

Coverage: US-listed equity + ETF options only. Crypto options are not here —
route those through a crypto venue. Index options (SPX/NDX) are not part of
Alpaca's options data.

Docs: https://docs.alpaca.markets/reference/optionchain
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.options")

_BASE = "https://data.alpaca.markets/v1beta1/options"
_cache = TTLCache()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _headers() -> dict | None:
    if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
        log.warning("ALPACA_API_KEY / ALPACA_API_SECRET not set — options skipped")
        return None
    return {
        "APCA-API-KEY-ID": settings.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": settings.ALPACA_API_SECRET,
        "accept": "application/json",
    }


def _feed(feed: str | None) -> str:
    return (feed or settings.ALPACA_OPTIONS_FEED or "indicative").strip().lower()


# --------------------------- OCC symbol helpers ---------------------------

def parse_occ(symbol: str) -> dict | None:
    """Parse an OCC option symbol into its components.

    `AAPL260619C00150000` -> {symbol, underlying: "AAPL",
        expiration: "2026-06-19", type: "call", strike: 150.0}

    The OCC format packs the last 15 chars as YYMMDD(6) + C/P(1) + strike*1000
    zero-padded to 8 digits; everything before that is the underlying root.
    Returns None if the tail doesn't parse.
    """
    s = (symbol or "").strip().upper()
    if len(s) < 16:
        return None
    tail = s[-15:]
    root = s[:-15]
    yy, mm, dd = tail[0:2], tail[2:4], tail[4:6]
    cp = tail[6]
    strike_raw = tail[7:]
    if cp not in ("C", "P") or not (yy + mm + dd + strike_raw).isdigit():
        return None
    try:
        exp = date(2000 + int(yy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None
    return {
        "symbol": s,
        "underlying": root,
        "expiration": exp,
        "type": "call" if cp == "C" else "put",
        "strike": int(strike_raw) / 1000.0,
    }


def build_occ(underlying: str, expiration: str, type_: str, strike: float) -> str:
    """Inverse of `parse_occ`: build an OCC symbol from components.

    `("AAPL", "2026-06-19", "call", 150)` -> `AAPL260619C00150000`.
    """
    root = (underlying or "").strip().upper()
    exp = datetime.strptime(expiration, "%Y-%m-%d").date()
    cp = "C" if (type_ or "").strip().lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{root}{exp:%y%m%d}{cp}{strike_int:08d}"


def _us_market_date() -> date:
    """Today in US/Eastern. DTE is a US-market calendar concept — the UTC date
    runs a day ahead between midnight UTC and ET market hours."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 — no tz database; EST approximation
        return (datetime.now(UTC) - timedelta(hours=5)).date()


def _dte(expiration: str | None) -> int | None:
    """Calendar days to expiration from today (US/Eastern). None if unparseable."""
    if not expiration:
        return None
    try:
        exp = date.fromisoformat(expiration)
    except ValueError:
        return None
    return (exp - _us_market_date()).days


def _parse_contract(symbol: str, snap: dict) -> dict:
    """Flatten one Alpaca snapshot entry into a tckr contract row."""
    occ = parse_occ(symbol) or {"symbol": symbol, "underlying": None,
                                "expiration": None, "type": None, "strike": None}
    q = snap.get("latestQuote") or {}
    t = snap.get("latestTrade") or {}
    g = snap.get("greeks") or {}
    bid = _f(q.get("bp"))
    ask = _f(q.get("ap"))
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
        "bid_size": _i(q.get("bs")),
        "ask_size": _i(q.get("as")),
        "quote_ts": q.get("t"),
        "last": _f(t.get("p")),
        "last_size": _i(t.get("s")),
        "trade_ts": t.get("t"),
        "iv": _f(snap.get("impliedVolatility")),
        "delta": _f(g.get("delta")),
        "gamma": _f(g.get("gamma")),
        "theta": _f(g.get("theta")),
        "vega":  _f(g.get("vega")),
        "rho":   _f(g.get("rho")),
    }


def _sort_key(c: dict) -> tuple:
    return (c.get("expiration") or "", c.get("strike") or 0.0, c.get("type") or "")


# --------------------------- chain ---------------------------

async def option_chain(
    underlying: str,
    *,
    expiration: str | None = None,
    exp_gte: str | None = None,
    exp_lte: str | None = None,
    type: str | None = None,
    strike_gte: float | None = None,
    strike_lte: float | None = None,
    feed: str | None = None,
    limit: int = 100,
    max_pages: int = 3,
) -> dict | None:
    """Option chain for `underlying` with quotes + greeks + IV per contract.

    Returns:
        {underlying, feed, count, ts, contracts: [
            {symbol, underlying, expiration, dte, type, strike,
             bid, ask, mid, bid_size, ask_size, quote_ts,
             last, last_size, trade_ts,
             iv, delta, gamma, theta, vega, rho}, ...]}
    sorted by (expiration, strike, type). Returns None if keys are unset or the
    upstream fails; an empty `contracts` list when the underlying has no
    matching contracts.

    Narrowing args map straight to Alpaca query params — always pass at least
    `expiration` (or `exp_gte`/`exp_lte`) for a liquid name like SPY/AAPL, since
    the full chain across all expiries can be thousands of contracts.

    `limit` is per page (max 1000); `max_pages` bounds pagination so a wide
    query can't run away. If pagination is truncated, a `truncated: True` flag
    is added so callers know more contracts exist.
    """
    sym = (underlying or "").strip().upper()
    if not sym:
        return None
    headers = _headers()
    if not headers:
        return None
    fd = _feed(feed)
    limit = max(1, min(int(limit), 1000))
    max_pages = max(1, int(max_pages))
    typ = (type or "").strip().lower() or None

    ck = ("chain", sym, expiration, exp_gte, exp_lte, typ,
          strike_gte, strike_lte, fd, limit, max_pages)
    cached = _cache.get(ck, settings.OPTIONS_TTL_S)
    if cached is not None:
        return cached

    params: dict = {"feed": fd, "limit": limit}
    if expiration:
        params["expiration_date"] = expiration
    if exp_gte:
        params["expiration_date_gte"] = exp_gte
    if exp_lte:
        params["expiration_date_lte"] = exp_lte
    if typ in ("call", "put"):
        params["type"] = typ
    if strike_gte is not None:
        params["strike_price_gte"] = strike_gte
    if strike_lte is not None:
        params["strike_price_lte"] = strike_lte

    contracts: list[dict] = []
    page_token: str | None = None
    truncated = False
    for page in range(max_pages):
        if page_token:
            params["page_token"] = page_token
        body = await _http.get_json(
            f"{_BASE}/snapshots/{sym}",
            params=params,
            headers=headers,
            label=f"alpaca options snapshots {sym}",
        )
        if not isinstance(body, dict):
            # First-page failure → None; later-page failure → return what we have.
            if page == 0:
                return None
            break
        snaps = body.get("snapshots") or {}
        for occ_sym, snap in snaps.items():
            if isinstance(snap, dict):
                contracts.append(_parse_contract(occ_sym, snap))
        page_token = body.get("next_page_token")
        if not page_token:
            break
        if page == max_pages - 1:
            truncated = True

    contracts.sort(key=_sort_key)
    out = {
        "underlying": sym,
        "feed": fd,
        "count": len(contracts),
        "ts": _now_iso(),
        "contracts": contracts,
    }
    if truncated:
        out["truncated"] = True
    _cache.put(ck, out)
    return out


# --------------------------- single / explicit contracts ---------------------------

async def option_snapshot(symbols: str | list[str], *,
                          feed: str | None = None) -> list[dict]:
    """Snapshots for one or more explicit OCC contract symbols.

    `symbols` is a single OCC symbol or a list of them. Returns the same
    flattened contract rows as `option_chain`, sorted by (expiration, strike,
    type). Unknown/expired symbols are simply absent from the result.
    """
    if isinstance(symbols, str):
        wanted = [symbols]
    else:
        wanted = list(symbols or [])
    wanted = [s.strip().upper() for s in wanted if s and s.strip()]
    if not wanted:
        return []
    headers = _headers()
    if not headers:
        return []
    fd = _feed(feed)
    joined = ",".join(sorted(wanted))
    ck = ("snapshot", joined, fd)
    cached = _cache.get(ck, settings.OPTIONS_TTL_S)
    if cached is not None:
        return cached

    body = await _http.get_json(
        f"{_BASE}/snapshots",
        params={"symbols": joined, "feed": fd},
        headers=headers,
        label=f"alpaca options snapshot {wanted[0]}{'+' if len(wanted) > 1 else ''}",
    )
    if not isinstance(body, dict):
        return []
    snaps = body.get("snapshots") or {}
    rows = [_parse_contract(s, snap) for s, snap in snaps.items()
            if isinstance(snap, dict)]
    rows.sort(key=_sort_key)
    _cache.put(ck, rows)
    return rows


# --------------------------- expirations ---------------------------

async def expirations(underlying: str, *, feed: str | None = None,
                      max_pages: int = 5) -> dict | None:
    """Distinct expiration dates (and the strike range) available for `underlying`.

    Returns {underlying, expirations: ["2026-06-19", ...], strikes:
    {min, max}, ts}. Derived by paging the chain filtered to calls only (calls
    and puts share the same expiry/strike ladder, so this halves the payload).

    Cached longer than quote data — the listed ladder changes slowly (new
    weeklies roll on, old ones expire), not tick-by-tick.
    """
    sym = (underlying or "").strip().upper()
    if not sym:
        return None
    headers = _headers()
    if not headers:
        return None
    fd = _feed(feed)
    ck = ("expirations", sym, fd)
    cached = _cache.get(ck, settings.OPTIONS_EXPIRATIONS_TTL_S)
    if cached is not None:
        return cached

    chain = await option_chain(sym, type="call", feed=fd,
                               limit=1000, max_pages=max(1, int(max_pages)))
    if chain is None:
        return None
    exps = sorted({c["expiration"] for c in chain["contracts"] if c.get("expiration")})
    strikes = [c["strike"] for c in chain["contracts"] if c.get("strike") is not None]
    out = {
        "underlying": sym,
        "expirations": exps,
        "strikes": {"min": min(strikes), "max": max(strikes)} if strikes else None,
        "ts": _now_iso(),
    }
    if chain.get("truncated"):
        out["truncated"] = True
    _cache.put(ck, out)
    return out


# --------------------------- Alpaca → CBOE cascade ---------------------------
#
# "Best available" options data without the caller choosing a provider: use
# Alpaca when its keys are configured (official, opra-capable), otherwise — or
# when Alpaca returns nothing — fall back to the keyless CBOE delayed feed. Each
# result carries a `source` field so the caller knows which upstream answered.
# Mirrors the tckr.quotes / tckr.history cascade convention.


def _alpaca_ready() -> bool:
    return bool(settings.ALPACA_API_KEY and settings.ALPACA_API_SECRET)


async def chain_cascade(underlying: str, **kwargs) -> dict | None:
    """Option chain via Alpaca if keyed, else keyless CBOE. Adds `source`.

    Accepts the same narrowing kwargs as `option_chain` (expiration, exp_gte,
    exp_lte, type, strike_gte, strike_lte). CBOE ignores Alpaca-only kwargs
    (`feed`, `limit`, `max_pages`).

    An *empty* Alpaca chain also falls through to CBOE — Alpaca has no index
    options, so empty usually means "no coverage" rather than "no contracts
    match". If CBOE can't answer either, the empty Alpaca result is returned
    (it's still an authoritative answer for the filter the caller asked for).
    """
    from tckr import cboe

    alpaca_res = None
    if _alpaca_ready():
        alpaca_res = await option_chain(underlying, **kwargs)
        if alpaca_res is not None and alpaca_res.get("count"):
            alpaca_res["source"] = "alpaca"
            return alpaca_res
    cboe_kwargs = {k: v for k, v in kwargs.items()
                   if k in ("expiration", "exp_gte", "exp_lte",
                            "type", "strike_gte", "strike_lte")}
    res = await cboe.option_chain(underlying, **cboe_kwargs)
    if res is not None:
        res["source"] = "cboe"
        return res
    if alpaca_res is not None:
        alpaca_res["source"] = "alpaca"
    return alpaca_res


async def snapshot_cascade(symbols: str | list[str]) -> list[dict]:
    """Explicit-contract snapshots via Alpaca if keyed, else keyless CBOE."""
    from tckr import cboe

    if _alpaca_ready():
        rows = await option_snapshot(symbols)
        if rows:
            return rows
    return await cboe.option_snapshot(symbols)


async def expirations_cascade(underlying: str) -> dict | None:
    """Expiration ladder via Alpaca if keyed, else keyless CBOE. Adds `source`."""
    from tckr import cboe

    alpaca_res = None
    if _alpaca_ready():
        alpaca_res = await expirations(underlying)
        if alpaca_res is not None and alpaca_res.get("expirations"):
            alpaca_res["source"] = "alpaca"
            return alpaca_res
    res = await cboe.expirations(underlying)
    if res is not None:
        res["source"] = "cboe"
        return res
    if alpaca_res is not None:
        alpaca_res["source"] = "alpaca"
    return alpaca_res
