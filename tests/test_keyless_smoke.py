"""Smoke tests for keyless modules.

These hit live upstreams. They're tolerant of upstream flakiness — a None
return is acceptable; a successful call should produce a well-shaped result.

CI runs these by default; mark new keyed-tier tests with @pytest.mark.needs_keys
so they're skipped in CI but run locally with `pytest -m needs_keys`.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_geckoterminal_trending_pools_base():
    from tckr import geckoterminal as gt
    rows = await gt.trending_pools("base", limit=3)
    if rows is None:
        pytest.skip("upstream returned None — likely transient rate limit")
    assert isinstance(rows, list)
    if rows:
        r = rows[0]
        assert "pool_address" in r
        assert "base_token" in r or "name" in r


async def test_defillama_chain_base():
    from tckr import defillama as dl
    chain = await dl.chain("base")
    if chain is None:
        pytest.skip("upstream returned None")
    assert isinstance(chain, dict)
    assert "tvl_usd" in chain
    assert isinstance(chain["tvl_usd"], (int, float))


async def test_hyperliquid_perps_universe_includes_btc():
    from tckr import hyperliquid as hl
    rows = await hl.perps_universe()
    if rows is None:
        pytest.skip("upstream returned None")
    assert isinstance(rows, list)
    syms = {r.get("symbol") for r in rows if isinstance(r, dict)}
    assert "BTC" in syms, f"BTC missing from perps universe; got {sorted(syms)[:10]}…"


async def test_dexscreener_search_btc():
    from tckr import dexscreener as ds
    rows = await ds.search("BTC")
    if rows is None:
        pytest.skip("upstream returned None")
    assert isinstance(rows, list)


async def test_coingecko_simple_price_btc():
    from tckr import coingecko as cg
    data = await cg.simple_price("bitcoin", "usd", include_24h_change=True)
    if data is None:
        pytest.skip("upstream returned None — likely rate-limited (public no-key)")
    assert isinstance(data, dict)
    assert "bitcoin" in data
    assert "usd" in data["bitcoin"]
    assert isinstance(data["bitcoin"]["usd"], (int, float))


async def test_coingecko_trending():
    from tckr import coingecko as cg
    data = await cg.trending()
    if data is None:
        pytest.skip("upstream returned None")
    assert isinstance(data, dict)
    assert "coins" in data


async def test_polymarket_top_volume():
    from tckr import polymarket as pm
    rows = await pm.top_volume(limit=5)
    if rows is None:
        pytest.skip("upstream returned None")
    assert isinstance(rows, list)
    if rows:
        m = rows[0]
        # `yes_price` may be None for malformed markets — just check the key exists.
        assert "slug" in m
        assert "question" in m


async def test_pyth_btc_usd():
    from tckr import pyth
    rows = await pyth.latest_price_for_symbols(["BTC/USD"])
    if rows is None:
        pytest.skip("upstream returned None")
    assert isinstance(rows, list)
    if rows:
        r = rows[0]
        assert r.get("id") is not None
        # price may be None on transient publish gap; just check the field exists
        assert "price" in r
        assert "publish_time" in r


async def test_pyth_feeds_crypto_filter():
    from tckr import pyth
    rows = await pyth.feeds(query="BTC", asset_type="crypto")
    if rows is None:
        pytest.skip("upstream returned None")
    assert isinstance(rows, list)
    if rows:
        assert any("BTC" in (f.get("symbol") or "") for f in rows)


async def test_solscan_public_token_meta_usdc():
    """USDC on Solana — well-known mint, public endpoint."""
    from tckr import solscan as sc
    # EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v = USDC SPL mint
    data = await sc.token_meta("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", pro=False)
    if data is None:
        pytest.skip("upstream returned None — solscan public API throttled")
    assert isinstance(data, dict)


async def test_thegraph_public_uniswap_v3():
    """Public gateway is heavily throttled but should work for a small query."""
    from tckr import thegraph as tg
    rows = await tg.uniswap_v3_top_pools(first=3)
    if rows is None:
        pytest.skip("upstream returned None — public gateway throttled")
    assert isinstance(rows, list)
    if rows:
        p = rows[0]
        assert "totalValueLockedUSD" in p
        assert "token0" in p
