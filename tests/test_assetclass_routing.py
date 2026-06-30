"""Offline tests for the asset-class routing / Stooq→Yahoo / guardrail fixes.

These are deterministic (no live network): pure helpers are tested directly, and
the few async paths monkeypatch the upstream fetchers with stubs. Live behavior
is covered by tests/test_keyless_smoke.py.
"""
from __future__ import annotations

import pytest

from tckr import yahoo
from tckr import quotes
from tckr.agent_toolkit import core


# --------------------------- yahoo.map_symbol ---------------------------

@pytest.mark.parametrize("ticker,cls,expected", [
    ("MU", "equity", "MU"),
    ("AAPL", None, "AAPL"),
    ("SPY", "etf", "SPY"),
    ("XAU", "metal", "GC=F"),
    ("XAG", "metal", "SI=F"),
    ("WTI", "energy", "CL=F"),
    ("EURUSD", "fx", "EURUSD=X"),
    ("EURUSD", None, "EURUSD=X"),   # inferred FX from 6-letter shape
    ("GC=F", None, "GC=F"),          # native passthrough
    ("BRK-B", "equity", "BRK-B"),    # hyphen passthrough
])
def test_yahoo_map_symbol(ticker, cls, expected):
    assert yahoo.map_symbol(ticker, cls) == expected


@pytest.mark.parametrize("ticker,expected", [
    ("WTI", "energy"), ("BRENT", "energy"), ("NATGAS", "energy"),
    ("MU", None), ("BTC", None), ("XAU", None),   # Pyth covers these → no fallback
])
def test_yahoo_fallback_asset_class(ticker, expected):
    # Commodities Pyth lacks (WTI etc.) get a fallback class so they still route
    # to Yahoo instead of the crypto cascade; Pyth-covered symbols return None.
    assert yahoo.fallback_asset_class(ticker) == expected


def test_yahoo_map_symbol_empty():
    assert yahoo.map_symbol("") is None
    assert yahoo.map_symbol("   ") is None


def test_yahoo_parse_chart():
    payload = {"chart": {"result": [{
        "timestamp": [1700000000, 1700086400, 1700172800],
        "indicators": {"quote": [{
            "open": [10.0, 11.0, None],
            "high": [10.5, 11.5, 12.0],
            "low": [9.5, 10.5, 11.0],
            "close": [10.2, None, 11.8],   # middle bar has no close → dropped
            "volume": [1000, 2000, 3000],
        }]},
    }]}}
    bars = yahoo._parse_chart(payload, days=10)
    assert [b["c"] for b in bars] == [10.2, 11.8]   # null-close row skipped
    assert bars[0]["t"] and bars[-1]["v"] == 3000.0


def test_yahoo_parse_chart_garbage():
    assert yahoo._parse_chart({}, days=10) is None
    assert yahoo._parse_chart({"chart": {"result": []}}, days=10) is None


# --------------------------- address detection ---------------------------

@pytest.mark.parametrize("s,is_addr", [
    ("0x" + "a" * 40, True),
    ("BWVLAfrGMbu3oUsza5ZWHcysDZPfB3hPyNfUCPv1p7Y7", True),  # solana base58
    ("BTC", False),
    ("HYPE", False),
    ("MU", False),
    ("0x123", False),            # too short for EVM
    ("EURUSD", False),
])
def test_looks_like_address(s, is_addr):
    assert quotes._looks_like_address(s) is is_addr


# --------------------------- TA guardrails ---------------------------

def test_periods_per_year():
    assert core._periods_per_year("crypto") == 365
    assert core._periods_per_year(None) == 365
    assert core._periods_per_year("equity") == 252
    assert core._periods_per_year("metal") == 252


def test_assess_quality_good_series():
    closes = [100 + i * 0.1 for i in range(60)]   # 60 smooth bars
    q = core._assess_series_quality(closes, "equity")
    assert q["reliable"] is True
    assert q["n_bars"] == 60
    assert q["periods_per_year"] == 252
    assert q["warnings"] == []


def test_assess_quality_too_short():
    q = core._assess_series_quality([100, 101, 102], "crypto")
    assert q["reliable"] is False
    assert any("bars" in w for w in q["warnings"])


def test_assess_quality_extreme_bar():
    # 40 bars (long enough) but a launch-ramp 10x bar ⇒ unreliable.
    closes = [0.001] + [1.0] * 40
    q = core._assess_series_quality(closes, "crypto")
    assert q["reliable"] is False
    assert any("extreme" in w for w in q["warnings"])


# --------------------------- token_resolve (stubbed) ---------------------------

async def test_token_resolve_ambiguous(monkeypatch):
    fake_pairs = [
        {"base_token": {"symbol": "ANSEM", "name": "Real", "address": "AAA"},
         "chain": "solana", "pair_address": "p1", "price_usd": 0.1,
         "liquidity_usd": 1_000_000, "fdv_usd": 1e7, "volume": {"h24": 5e5}},
        {"base_token": {"symbol": "ANSEM", "name": "Copy", "address": "BBB"},
         "chain": "solana", "pair_address": "p2", "price_usd": 0.01,
         "liquidity_usd": 5_000, "fdv_usd": 1e5, "volume": {"h24": 1e3}},
        {"base_token": {"symbol": "ANSEM", "name": "Real-2nd-pool", "address": "AAA"},
         "chain": "solana", "pair_address": "p3", "price_usd": 0.1,
         "liquidity_usd": 800_000, "fdv_usd": 1e7, "volume": {"h24": 4e5}},
    ]

    async def fake_search(query, *, chain=None):
        return fake_pairs

    monkeypatch.setattr("tckr.dexscreener.search", fake_search)
    out = await core.get_tool("token_resolve").callable({"query": "ANSEM"})
    assert out["n_distinct_tokens"] == 2          # AAA + BBB (p1/p3 dedup to AAA)
    assert out["ambiguous"] is True
    # deepest-liquidity token first, single pair per token
    assert out["candidates"][0]["token_address"] == "AAA"
    assert out["candidates"][0]["liquidity_usd"] == 1_000_000


async def test_token_resolve_single(monkeypatch):
    async def fake_search(query, *, chain=None):
        return [{"base_token": {"symbol": "ONLY", "name": "Only", "address": "X"},
                 "chain": "base", "pair_address": "p", "price_usd": 1.0,
                 "liquidity_usd": 10_000, "fdv_usd": 1e5, "volume": {"h24": 1e3}}]

    monkeypatch.setattr("tckr.dexscreener.search", fake_search)
    out = await core.get_tool("token_resolve").callable({"query": "ONLY"})
    assert out["n_distinct_tokens"] == 1
    assert out["ambiguous"] is False


# --------------------------- quote address path (stubbed) ---------------------------

async def test_quote_by_address(monkeypatch):
    async def fake_token_pairs(addr, *, chain=None):
        return [
            {"base_token": {"symbol": "TKN", "name": "Token", "address": addr},
             "chain": "solana", "price_usd": 0.5, "liquidity_usd": 100_000},
            {"base_token": {"symbol": "TKN", "name": "Token", "address": addr},
             "chain": "solana", "price_usd": 0.5, "liquidity_usd": 900_000},
        ]

    monkeypatch.setattr("tckr.dexscreener.token_pairs", fake_token_pairs)
    addr = "BWVLAfrGMbu3oUsza5ZWHcysDZPfB3hPyNfUCPv1p7Y7"
    out = await quotes.get([addr])
    assert addr in out
    assert out[addr]["source"] == "dexscreener"
    assert out[addr]["price"] == 0.5
    assert out[addr]["asset_class"] == "crypto"


# --------------------------- GDELT rate gate (mocked HTTP) ---------------------------

async def test_gdelt_rate_gate_spaces_requests(monkeypatch):
    """The GDELT gate must serialize cold fetches and space them end-to-end by
    GDELT_MIN_INTERVAL_S — proven deterministically by recording the timestamps
    of a mocked HTTP layer, so it doesn't depend on the live (throttle-prone)
    GDELT endpoint."""
    import asyncio
    import time
    from tckr import gdelt, settings

    interval = 0.25
    monkeypatch.setattr(settings, "GDELT_MIN_INTERVAL_S", interval)
    gdelt._last_fetch_mono = 0.0  # reset the process-wide gate clock

    hits: list[float] = []

    async def fake_get_json(url, *, params=None, headers=None, label=""):
        hits.append(time.monotonic())
        return {"articles": [{"url": "http://x", "title": "t", "domain": "d",
                              "seendate": "20260101T000000Z"}]}

    monkeypatch.setattr("tckr._http.get_json", fake_get_json)

    # Four DISTINCT queries → four cold cache keys → four real fetches.
    results = await asyncio.gather(*(
        gdelt.articles(f"query number {i}", timespan="1d", max_records=3)
        for i in range(4)
    ))
    assert all(r for r in results)        # every call delivered data
    assert len(hits) == 4                 # serialized, not deduped
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    # Each consecutive upstream fetch is spaced ~>= interval (allow scheduling slack).
    assert all(g >= interval * 0.9 for g in gaps), gaps
