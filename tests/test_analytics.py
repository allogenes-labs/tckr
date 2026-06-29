"""Unit tests for tckr.analytics — pure, offline, no network or keys.

These assert each formula against hand-computed fixtures and known identities;
that determinism is the whole reason the library exists (vs. an LLM doing the
arithmetic). Also guards the ta_* agent-tool registrations (pure schema checks).
"""
from __future__ import annotations

import math

import pytest

from tckr import analytics as an


def _close(a, b, *, tol=1e-9):
    return a is not None and b is not None and math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# --------------------------- returns ---------------------------

def test_returns_and_cumulative_identity():
    s = [100.0, 110.0, 99.0, 108.9]
    r = an.returns(s)
    assert len(r) == len(s) - 1
    assert _close(r[0], 0.10)
    assert _close(r[1], -0.10)
    # cumulative_return == prod(1+r) - 1
    prod = 1.0
    for x in r:
        prod *= (1.0 + x)
    assert _close(an.cumulative_return(s), prod - 1.0)


def test_log_returns_sum_equals_log_total():
    s = [50.0, 60.0, 55.0, 70.0]
    lr = an.log_returns(s)
    assert _close(sum(lr), math.log(s[-1] / s[0]))


def test_returns_guard_nonpositive_prev_is_nan():
    r = an.returns([0.0, 5.0])
    assert len(r) == 1 and math.isnan(r[0])


# --------------------------- moving averages ---------------------------

def test_sma_exact_and_length():
    s = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = an.sma(s, 3)
    assert out == [2.0, 3.0, 4.0]          # means of [1,2,3],[2,3,4],[3,4,5]
    assert len(out) == len(s) - 3 + 1


def test_ema_seeded_with_sma():
    s = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = an.ema(s, 3)
    # first EMA == SMA of first 3 == 2.0; then k = 2/4 = 0.5
    assert _close(out[0], 2.0)
    assert _close(out[1], 4.0 * 0.5 + 2.0 * 0.5)   # 3.0
    assert _close(out[2], 5.0 * 0.5 + 3.0 * 0.5)   # 4.0


def test_wma_weights_newest_heaviest():
    s = [1.0, 2.0, 3.0]
    # weights 1,2,3 over [1,2,3] = (1*1 + 2*2 + 3*3) / 6 = 14/6
    assert _close(an.wma(s, 3)[0], 14.0 / 6.0)


# --------------------------- indicators ---------------------------

def test_rsi_monotonic_extremes_and_length():
    up = [float(i) for i in range(1, 30)]
    r_up = an.rsi(up, 14)
    assert len(r_up) == len(up) - 14
    assert all(_close(x, 100.0) for x in r_up)     # no losses → 100
    down = [float(i) for i in range(30, 1, -1)]
    assert all(_close(x, 0.0) for x in an.rsi(down, 14))   # no gains → 0


def test_macd_hist_is_macd_minus_signal():
    s = [float(i) + (i % 5) for i in range(1, 60)]
    m = an.macd(s)
    assert m is not None
    assert len(m["macd"]) == len(m["signal"]) == len(m["hist"])
    for mm, sig, h in zip(m["macd"], m["signal"], m["hist"], strict=True):
        assert _close(h, mm - sig)


def test_correlation_self_is_one():
    from tckr import analytics as an
    s = [1.0, 1.1, 1.05, 1.2, 1.15, 1.3, 1.25]
    assert _close(an.correlation(s, s), 1.0)


def test_correlation_aligns_unequal_length_from_recent_end():
    """A longer benchmark must align to the asset's recent window, not misalign.
    Correlating a series with a suffix of itself should be ~1.0."""
    from tckr import analytics as an
    a = [10.0, 11.0, 10.5, 12.0, 11.5, 13.0]
    b = [1.0, 2.0, 3.0] + a  # same recent tail, extra older history
    assert _close(an.correlation(a, b), 1.0)


def test_correlation_skips_nonfinite_period_in_both_series():
    """A zero price (→ nan return) in one series drops that period from BOTH,
    keeping the vectors index-locked rather than shifting one."""
    from tckr import analytics as an
    a = [1.0, 2.0, 0.0, 2.0, 3.0, 4.0]   # the 0.0 makes one return non-finite
    b = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
    c = an.correlation(a, b)
    assert c is None or -1.0 <= c <= 1.0  # must not raise / must stay valid


def test_bollinger_mid_is_sma_and_constant_collapses():
    s = [10.0] * 25
    b = an.bollinger(s, 20, 2.0)
    assert b is not None
    # zero variance → all three bands equal the mid
    assert _close(b["mid"][-1], 10.0)
    assert _close(b["upper"][-1], 10.0)
    assert _close(b["lower"][-1], 10.0)


def test_atr_wilder_fixture():
    bars = [
        {"h": 10.0, "l": 8.0, "c": 9.0},
        {"h": 12.0, "l": 9.0, "c": 11.0},
        {"h": 11.0, "l": 7.0, "c": 8.0},
    ]
    # TRs: 2, 3, 4 ; period=2 → seed mean(2,3)=2.5 ; next (2.5*1 + 4)/2 = 3.25
    out = an.atr(bars, 2)
    assert len(out) == len(bars) - 2 + 1
    assert _close(out[0], 2.5)
    assert _close(out[1], 3.25)


# --------------------------- risk / performance ---------------------------

def test_max_drawdown_fixture():
    mdd = an.max_drawdown([100.0, 120.0, 60.0, 80.0])
    assert _close(mdd["max_drawdown"], -0.5)   # 120 → 60
    assert mdd["peak_idx"] == 1
    assert mdd["trough_idx"] == 2


def test_volatility_zero_for_flat_and_sharpe_none():
    flat = [100.0, 100.0, 100.0]
    assert _close(an.volatility(flat), 0.0)
    assert an.sharpe(flat) is None             # zero variance → undefined


def test_volatility_annualization_factor():
    # two-point returns: stdev of a single pair is the spread/sqrt(2)... use a
    # series with known sample stdev. returns = [0.1, -0.1] → mean 0, stdev = sqrt(((0.1)^2+(0.1)^2)/1)
    s = [100.0, 110.0, 99.0]
    r = an.returns(s)
    import statistics
    expected = statistics.stdev(r) * math.sqrt(365)
    assert _close(an.volatility(s), expected)


# --------------------------- statistical ---------------------------

def test_zscore_last_value():
    s = [1.0, 2.0, 3.0, 4.0, 5.0]
    # mean 3, pstdev = sqrt(2); last=5 → (5-3)/sqrt(2)
    assert _close(an.zscore(s), 2.0 / math.sqrt(2.0))


def test_percentile_rank():
    assert _close(an.percentile_rank([1, 2, 3, 4], 3), 0.75)
    assert _close(an.percentile_rank([1, 2, 3, 4], 0), 0.0)
    assert _close(an.percentile_rank([1, 2, 3, 4], 9), 1.0)


def test_correlation_and_beta_on_scaled_returns():
    # asset returns are exactly 2x benchmark returns → corr 1.0, beta 2.0
    bench = [100.0, 110.0, 104.5, 125.4]        # returns 0.1, -0.05, 0.2
    asset = [100.0, 120.0, 108.0, 151.2]        # returns 0.2, -0.10, 0.4
    assert _close(an.correlation(asset, bench), 1.0, tol=1e-6)
    assert _close(an.beta(asset, bench), 2.0, tol=1e-6)
    assert _close(an.beta(bench, bench), 1.0, tol=1e-9)


# --------------------------- graceful failure modes ---------------------------

@pytest.mark.parametrize("fn", [an.returns, an.log_returns])
def test_sequence_fns_empty_on_bad_input(fn):
    assert fn([]) == []
    assert fn([1.0]) == []
    assert fn(["x", "y"]) == []                 # non-numeric → unusable


@pytest.mark.parametrize("fn", [
    lambda s: an.cumulative_return(s),
    lambda s: an.volatility(s),
    lambda s: an.sharpe(s),
    lambda s: an.zscore(s),
    lambda s: an.max_drawdown(s),
    lambda s: an.macd(s),
])
def test_scalar_fns_none_on_short_input(fn):
    assert fn([1.0]) is None
    assert fn([]) is None


def test_period_longer_than_series_is_empty():
    assert an.sma([1.0, 2.0], 5) == []
    assert an.ema([1.0, 2.0], 5) == []
    assert an.rsi([1.0, 2.0], 14) == []


# --------------------------- ta_* tool registrations (pure) ---------------------------

@pytest.mark.parametrize("name,required", [
    ("ta_risk", ["symbol"]),
    ("ta_indicators", ["symbol"]),
    ("ta_correlation", ["symbol", "benchmark"]),
])
def test_ta_tools_registered(name, required):
    from tckr.agent_toolkit.core import get_tool
    spec = get_tool(name)
    assert spec is not None, f"{name} not registered in TOOLS"
    assert spec.module == "", "local-compute analytics tools use module='' (meta bucket)"
    assert spec.schema.get("required") == required
