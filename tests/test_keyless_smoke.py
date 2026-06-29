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


async def test_polymarket_outcome_touches():
    """Live CLOB touch summary — sources slug via top_volume so we never hardcode.

    Exercises the gamma + 2-book parallel-fetch path. We accept a wide range
    of returned values (real Polymarket books can be very thin) — just assert
    shape + that at least one side responded with a non-null best_bid or
    best_ask. The interesting wedge in this code path is the asyncio.gather
    with the `_none()` placeholder, which the shape-check covers.
    """
    from tckr import polymarket as pm
    rows = await pm.top_volume(limit=5)
    if not rows:
        pytest.skip("upstream returned no top-volume rows")
    slug = next((m.get("slug") for m in rows if m.get("slug")), None)
    if not slug:
        pytest.skip("no slug on any top-volume row")
    t = await pm.outcome_touches(slug)
    if t is None:
        pytest.skip(f"outcome_touches returned None for {slug!r}")
    for k in ("slug", "yes_bid", "yes_ask", "no_bid", "no_ask",
              "tick_size", "min_order_size", "liquidity"):
        assert k in t, f"missing key {k!r} in outcome_touches shape"
    # At least one side should have produced a touch — if both books were
    # empty, our gamma + CLOB cascade isn't talking to the right market.
    sides = [t.get("yes_bid"), t.get("yes_ask"),
             t.get("no_bid"), t.get("no_ask")]
    assert any(v is not None for v in sides), (
        f"both YES and NO books were empty for top-volume slug {slug!r}"
    )


async def test_polymarket_market_status_alive():
    """Pick a hot active market and confirm market_status classifies it as alive.

    Uses top_volume to source a slug live (so we never hardcode a slug that
    polymarket may rename). An active top-volume market should be "alive";
    anything else (resolved/ghost/ambiguous) for a *just-discovered* active
    market would indicate the cascade is mis-routing.
    """
    from tckr import polymarket as pm
    rows = await pm.top_volume(limit=5)
    if not rows:
        pytest.skip("upstream returned no top-volume rows")
    slug = next((m.get("slug") for m in rows if m.get("slug")), None)
    if not slug:
        pytest.skip("no slug on any top-volume row")
    status = await pm.market_status(slug)
    assert status in {"alive", "resolved_yes", "resolved_no",
                      "ambiguous", "ghost"}
    # A market picked from active top_volume should be alive; if it isn't,
    # the discovery and detail endpoints disagree (worth surfacing).
    assert status == "alive", (
        f"top_volume returned slug {slug!r} but market_status said {status!r}"
    )


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


async def test_bankr_new_launches():
    """Bankr public launchpad feed — keyless, fixed 50-row response."""
    from tckr import bankr
    rows = await bankr.new_launches(limit=5)
    if rows is None:
        pytest.skip("upstream returned None")
    assert isinstance(rows, list)
    if rows:
        r = rows[0]
        # Core fields that should always parse out of a deployed launch.
        for k in ("activity_id", "token_address", "chain", "launch_type",
                  "deployer_address", "timestamp_ms"):
            assert k in r, f"missing key {k!r} in parsed bankr row"
        # `chain` should be one of the known launchpad chains.
        assert r["chain"] in {"base", "solana"}, f"unexpected chain {r['chain']!r}"


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


async def test_cryptonews_latest_keyless():
    """Crypto outlet RSS aggregator — keyless, merged across feeds."""
    from tckr import cryptonews
    rows = await cryptonews.latest(limit=5)
    assert isinstance(rows, list)  # always a list (empty if all feeds down)
    if not rows:
        pytest.skip("all RSS feeds returned nothing — likely transient")
    r = rows[0]
    for k in ("title", "url", "source", "published_at", "published_ts",
              "summary", "categories", "image"):
        assert k in r, f"missing key {k!r} in cryptonews item"
    assert r["url"].startswith("http")
    assert r["source"] in cryptonews.FEEDS
    # merged result must be de-duplicated by URL
    urls = [x["url"] for x in rows]
    assert len(urls) == len(set(urls)), "cryptonews returned duplicate URLs"


async def test_cryptonews_single_feed():
    from tckr import cryptonews
    rows = await cryptonews.feed("decrypt")
    if not rows:
        pytest.skip("decrypt feed returned nothing — transient")
    assert isinstance(rows, list)
    assert all(it["source"] == "decrypt" for it in rows)


async def test_gdelt_articles_keyless():
    """GDELT DOC 2.0 — keyless. Tolerant of the ~1 req/5s soft-throttle, which
    returns a text body that surfaces here as None."""
    from tckr import gdelt
    rows = await gdelt.articles("bitcoin", timespan="2d", max_records=5)
    if rows is None:
        pytest.skip("GDELT returned None — likely the 1-req/5s soft throttle")
    assert isinstance(rows, list)
    if rows:
        r = rows[0]
        for k in ("title", "url", "source", "published_at", "published_ts",
                  "language", "source_country"):
            assert k in r, f"missing key {k!r} in gdelt article"
        assert r["url"].startswith("http")


@pytest.mark.needs_keys
async def test_finnhub_market_news():
    """Finnhub market news — requires FINNHUB_API_KEY (free tier)."""
    from tckr import finnhub, registry
    if not registry.configured("finnhub"):
        pytest.skip("FINNHUB_API_KEY not set")
    rows = await finnhub.market_news("general")
    if rows is None:
        pytest.skip("upstream returned None — rate-limited or transient")
    assert isinstance(rows, list)
    if rows:
        r = rows[0]
        for k in ("title", "url", "source", "published_at", "published_ts",
                  "summary", "category"):
            assert k in r, f"missing key {k!r} in finnhub item"
        assert r["url"].startswith("http")


async def test_news_cascade_keyless():
    """Unified cascade returns a merged, de-duped, provider-tagged list using at
    least the keyless providers."""
    from tckr import news
    rows = await news.latest(limit=8)
    assert isinstance(rows, list)
    if not rows:
        pytest.skip("no provider returned anything — transient")
    providers = {r.get("provider") for r in rows}
    assert providers <= {"cryptonews", "gdelt", "finnhub"}
    for r in rows:
        assert "provider" in r and r["url"].startswith("http")
    urls = [r["url"] for r in rows]
    assert len(urls) == len(set(urls)), "news cascade returned duplicate URLs"
