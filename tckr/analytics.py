"""tckr.analytics — deterministic financial primitives + technical indicators.

Pure, synchronous, **stdlib-only** (``math`` + ``statistics``). No I/O, no numpy,
no pandas — these functions run on the plain ``list[float]`` / OHLC-dict shapes
the data modules already return (e.g. ``history.candles_one(...)["closes"]`` or
``hyperliquid.candles(...)`` bars), so an agent gets a *provably* correct number
instead of doing arithmetic in-context.

Why this lives in tckr: the same repetition tckr kills for data access (every
project re-wrapping the same APIs) applies to math — every consumer otherwise
re-derives returns / volatility / RSI from candles. There is precedent for
compute-on-the-data here: ``coinalyze.funding_aggregate`` (median/mean/spread via
``statistics``), ``wallet_pnl.apply_fifo``, options greeks.

Conventions
-----------
- **Inputs**: close-based functions take ``list[float]`` (a price/close series,
  oldest-first). Range-based functions (``atr``) take ``list[dict]`` OHLC bars in
  the canonical ``{t, o, h, l, c, v}`` shape.
- **Units**: rates are returned as **fractions**, not percents (``0.02`` == 2%).
  The agent-toolkit layer multiplies by 100 for its ``*_pct`` fields; library
  callers convert if they want.
- **Annualization**: the default ``periods_per_year`` is **365**, not 252 — crypto
  trades 24/7, so a daily-bar series annualizes on 365 calendar days. Pass
  ``periods_per_year=252`` for an equities daily series, ``8760`` for hourly, etc.
- **Output alignment**: rolling functions return the most-recent value **last**;
  a window of ``period`` consumes the first ``period-1`` points, so the result has
  ``len(series) - period + 1`` elements (``rsi`` consumes one extra for the delta).

Failure modes
-------------
Following the package's graceful-degradation style, functions **return ``None``
(scalars / dicts) or ``[]`` (sequences) on insufficient or malformed input** —
empty series, non-numeric/non-finite values, ``period`` longer than the data, or a
zero-variance denominator — rather than raising. Callers check for ``None``.
"""
from __future__ import annotations

import math
import statistics

__all__ = [
    # returns
    "returns", "log_returns", "cumulative_return",
    # risk / performance
    "volatility", "downside_deviation", "sharpe", "sortino", "calmar",
    "max_drawdown",
    # moving averages / indicators
    "sma", "ema", "wma", "rsi", "macd", "bollinger", "atr",
    # cross-sectional / statistical
    "zscore", "percentile_rank", "correlation", "beta",
]


# --------------------------- helpers ---------------------------

def _f(v) -> float | None:
    """Safely cast to float (mirrors the per-module helper used across tckr)."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _floats(series) -> list[float] | None:
    """Coerce a series to a list of finite floats. Returns None if the series is
    empty or contains any value that isn't a finite number — a price series with
    a hole is treated as unusable rather than silently re-indexed."""
    if not series:
        return None
    out: list[float] = []
    for v in series:
        f = _f(v)
        if f is None or not math.isfinite(f):
            return None
        out.append(f)
    return out


def _finite(xs) -> list[float]:
    """Drop None / non-finite entries (e.g. an undefined return from a zero price)."""
    return [x for x in xs if x is not None and math.isfinite(x)]


def _last(xs):
    """Most-recent element of a sequence, or None if empty."""
    return xs[-1] if xs else None


def _ema_seq(values: list[float], period: int) -> list[float]:
    """EMA over a clean float list, seeded with the SMA of the first ``period``
    values. Returns ``len(values) - period + 1`` points (first aligns to index
    ``period-1``). Empty list if too short."""
    n = len(values)
    if period < 1 or n < period:
        return []
    k = 2.0 / (period + 1.0)
    prev = statistics.fmean(values[:period])
    out = [prev]
    for v in values[period:]:
        prev = v * k + prev * (1.0 - k)
        out.append(prev)
    return out


def _ohlc(bars) -> tuple[list[float], list[float], list[float]] | None:
    """Extract (highs, lows, closes) from a list of OHLC dicts. None if any bar
    is missing/!finite h/l/c."""
    if not bars:
        return None
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for b in bars:
        if not isinstance(b, dict):
            return None
        h, lo, c = _f(b.get("h")), _f(b.get("l")), _f(b.get("c"))
        if h is None or lo is None or c is None or not all(map(math.isfinite, (h, lo, c))):
            return None
        highs.append(h)
        lows.append(lo)
        closes.append(c)
    return highs, lows, closes


# --------------------------- returns ---------------------------

def returns(series) -> list[float]:
    """Simple periodic returns ``s[i]/s[i-1] - 1``. Length ``len-1``; an entry
    whose prior price is <= 0 is ``nan`` (filter with downstream stats)."""
    s = _floats(series)
    if s is None or len(s) < 2:
        return []
    out = []
    for prev, cur in zip(s, s[1:], strict=False):
        out.append(cur / prev - 1.0 if prev > 0 else float("nan"))
    return out


def log_returns(series) -> list[float]:
    """Natural-log returns ``ln(s[i]/s[i-1])``. Length ``len-1``; non-positive
    prices yield ``nan``."""
    s = _floats(series)
    if s is None or len(s) < 2:
        return []
    out = []
    for prev, cur in zip(s, s[1:], strict=False):
        out.append(math.log(cur / prev) if prev > 0 and cur > 0 else float("nan"))
    return out


def cumulative_return(series) -> float | None:
    """Total return over the series: ``s[-1]/s[0] - 1`` (fraction)."""
    s = _floats(series)
    if s is None or len(s) < 2 or s[0] <= 0:
        return None
    return s[-1] / s[0] - 1.0


# --------------------------- risk / performance ---------------------------

def volatility(series, *, periods_per_year: int = 365) -> float | None:
    """Annualized realized volatility = sample stdev of periodic returns
    ``* sqrt(periods_per_year)`` (fraction). None if < 2 usable returns."""
    rs = _finite(returns(series))
    if len(rs) < 2:
        return None
    return statistics.stdev(rs) * math.sqrt(periods_per_year)


def downside_deviation(series, *, target: float = 0.0,
                       periods_per_year: int = 365) -> float | None:
    """Annualized downside deviation of periodic returns below ``target``
    (a per-period return threshold, default 0). None if < 2 usable returns."""
    rs = _finite(returns(series))
    if len(rs) < 2:
        return None
    sq = [min(0.0, r - target) ** 2 for r in rs]
    return math.sqrt(statistics.fmean(sq)) * math.sqrt(periods_per_year)


def sharpe(series, *, rf: float = 0.0, periods_per_year: int = 365) -> float | None:
    """Annualized Sharpe ratio. ``rf`` is the **annual** risk-free rate (fraction);
    it is converted to a per-period drag. None if < 2 returns or zero variance."""
    rs = _finite(returns(series))
    if len(rs) < 2:
        return None
    sd = statistics.stdev(rs)
    if sd == 0:
        return None
    excess = statistics.fmean(rs) - rf / periods_per_year
    return (excess / sd) * math.sqrt(periods_per_year)


def sortino(series, *, rf: float = 0.0, periods_per_year: int = 365) -> float | None:
    """Annualized Sortino ratio — like Sharpe but the denominator is downside
    deviation (per-period, target = per-period rf). None if no downside or < 2
    returns."""
    rs = _finite(returns(series))
    if len(rs) < 2:
        return None
    target = rf / periods_per_year
    dd = math.sqrt(statistics.fmean([min(0.0, r - target) ** 2 for r in rs]))
    if dd == 0:
        return None
    excess = statistics.fmean(rs) - target
    return (excess / dd) * math.sqrt(periods_per_year)


def max_drawdown(series) -> dict | None:
    """Largest peak-to-trough decline over the series.

    Returns ``{max_drawdown, peak_idx, trough_idx}`` where ``max_drawdown`` is a
    fraction <= 0 (e.g. -0.42 == a 42% drawdown). None if < 2 points."""
    s = _floats(series)
    if s is None or len(s) < 2:
        return None
    peak = s[0]
    peak_idx = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0
    for i, p in enumerate(s):
        if p > peak:
            peak = p
            peak_idx = i
        dd = (p / peak - 1.0) if peak > 0 else 0.0
        if dd < worst:
            worst = dd
            worst_peak = peak_idx
            worst_trough = i
    return {"max_drawdown": worst, "peak_idx": worst_peak, "trough_idx": worst_trough}


def calmar(series, *, periods_per_year: int = 365) -> float | None:
    """Calmar ratio = annualized (CAGR-style) return / absolute max drawdown.
    None if < 2 points or zero drawdown."""
    s = _floats(series)
    if s is None or len(s) < 2 or s[0] <= 0:
        return None
    mdd = max_drawdown(s)
    if not mdd or mdd["max_drawdown"] == 0:
        return None
    n_periods = len(s) - 1
    growth = s[-1] / s[0]
    cagr = growth ** (periods_per_year / n_periods) - 1.0
    return cagr / abs(mdd["max_drawdown"])


# --------------------------- moving averages / indicators ---------------------------

def sma(series, period: int) -> list[float]:
    """Simple moving average. Length ``len - period + 1``, most-recent last."""
    s = _floats(series)
    if s is None or period < 1 or len(s) < period:
        return []
    return [statistics.fmean(s[i - period:i]) for i in range(period, len(s) + 1)]


def ema(series, period: int) -> list[float]:
    """Exponential moving average (SMA-seeded). Length ``len - period + 1``."""
    s = _floats(series)
    if s is None:
        return []
    return _ema_seq(s, period)


def wma(series, period: int) -> list[float]:
    """Linearly-weighted moving average (weights 1..period, newest heaviest).
    Length ``len - period + 1``."""
    s = _floats(series)
    if s is None or period < 1 or len(s) < period:
        return []
    weights = list(range(1, period + 1))
    denom = float(sum(weights))
    out = []
    for i in range(period, len(s) + 1):
        window = s[i - period:i]
        out.append(sum(w * x for w, x in zip(weights, window, strict=True)) / denom)
    return out


def rsi(series, period: int = 14) -> list[float]:
    """Wilder's RSI (0..100). Length ``len - period`` (one bar consumed for the
    first delta, ``period`` deltas for the seed). Most-recent last."""
    s = _floats(series)
    if s is None or period < 1 or len(s) < period + 1:
        return []
    deltas = [b - a for a, b in zip(s, s[1:], strict=False)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = statistics.fmean(gains[:period])
    avg_loss = statistics.fmean(losses[:period])

    def _rsi(g: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        rs = g / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out = [_rsi(avg_gain, avg_loss)]
    for g, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(_rsi(avg_gain, avg_loss))
    return out


def macd(series, *, fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD. Returns ``{macd, signal, hist}`` — three equal-length lists aligned to
    the signal-defined region, most-recent last. None if too short."""
    s = _floats(series)
    if s is None or fast >= slow or len(s) < slow + signal - 1:
        return None
    ema_fast = _ema_seq(s, fast)   # aligns to index fast-1
    ema_slow = _ema_seq(s, slow)   # aligns to index slow-1
    # MACD line is defined where both EMAs exist: original indices slow-1 .. len-1.
    macd_line = [
        ema_fast[j - (fast - 1)] - ema_slow[j - (slow - 1)]
        for j in range(slow - 1, len(s))
    ]
    if len(macd_line) < signal:
        return None
    signal_line = _ema_seq(macd_line, signal)         # aligns to macd_line[signal-1:]
    macd_tail = macd_line[signal - 1:]
    hist = [m - sig for m, sig in zip(macd_tail, signal_line, strict=True)]
    return {"macd": macd_tail, "signal": signal_line, "hist": hist}


def bollinger(series, period: int = 20, k: float = 2.0) -> dict | None:
    """Bollinger Bands. Returns ``{mid, upper, lower}`` (each ``len - period + 1``,
    most-recent last). Bands use the population stdev of each window. None if
    too short."""
    s = _floats(series)
    if s is None or period < 1 or len(s) < period:
        return None
    mid: list[float] = []
    upper: list[float] = []
    lower: list[float] = []
    for i in range(period, len(s) + 1):
        window = s[i - period:i]
        m = statistics.fmean(window)
        sd = statistics.pstdev(window)
        mid.append(m)
        upper.append(m + k * sd)
        lower.append(m - k * sd)
    return {"mid": mid, "upper": upper, "lower": lower}


def atr(bars, period: int = 14) -> list[float]:
    """Average True Range (Wilder) over OHLC bars. Needs high/low/close, so it
    takes ``list[dict]`` ``{h,l,c}`` bars (e.g. ``hyperliquid.candles`` output) —
    the ``history`` cascade returns closes only. Length ``len(bars) - period + 1``."""
    ohlc = _ohlc(bars)
    if ohlc is None:
        return []
    highs, lows, closes = ohlc
    n = len(closes)
    if period < 1 or n < period + 1:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        prev_c = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev_c), abs(lows[i] - prev_c)))
    prev = statistics.fmean(trs[:period])
    out = [prev]
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


# --------------------------- cross-sectional / statistical ---------------------------

def zscore(series) -> float | None:
    """Z-score of the **most-recent** value vs the whole series' mean / population
    stdev — "how many sigma extended is the latest point". None if < 2 points or
    zero variance."""
    s = _floats(series)
    if s is None or len(s) < 2:
        return None
    sd = statistics.pstdev(s)
    if sd == 0:
        return None
    return (s[-1] - statistics.fmean(s)) / sd


def percentile_rank(series, value: float) -> float | None:
    """Fraction of the series <= ``value``, in [0, 1]. None on empty/bad input."""
    s = _floats(series)
    v = _f(value)
    if s is None or v is None:
        return None
    return sum(1 for x in s if x <= v) / len(s)


def _pair_returns(a, b) -> tuple[list[float], list[float]] | None:
    """Periodic returns of two price series, aligned from the most-recent end."""
    ra = _finite(returns(a))
    rb = _finite(returns(b))
    n = min(len(ra), len(rb))
    if n < 2:
        return None
    return ra[-n:], rb[-n:]


def correlation(series_a, series_b) -> float | None:
    """Pearson correlation of the periodic **returns** of two price series
    (aligned from the most-recent end). None if < 2 overlapping returns or zero
    variance in either."""
    pr = _pair_returns(series_a, series_b)
    if pr is None:
        return None
    ra, rb = pr
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return None


def beta(asset, benchmark) -> float | None:
    """Beta of ``asset`` vs ``benchmark`` from periodic returns:
    ``cov(asset, bench) / var(bench)``. None if < 2 overlapping returns or the
    benchmark has zero return variance."""
    pr = _pair_returns(asset, benchmark)
    if pr is None:
        return None
    ra, rb = pr
    var_b = statistics.variance(rb)
    if var_b == 0:
        return None
    return statistics.covariance(ra, rb) / var_b
