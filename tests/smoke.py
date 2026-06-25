"""Live smoke test for tckr source modules.

Hits the real upstream APIs — run it to confirm a module still works end to end.
No pytest dependency; just: python tests/smoke.py

Grows as modules are added.
"""
from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.WARNING)


async def smoke_geckoterminal() -> None:
    from tckr import geckoterminal as gt

    tp = await gt.trending_pools("base", limit=3)
    print(f"[geckoterminal] base trending_pools: {len(tp)}")
    for p in tp:
        bt = (p["base_token"] or {}).get("symbol")
        print(f"  {p['name']:<30} sym={bt} price={p['price_usd']} "
              f"vol24h={p['volume_24h_usd']} dex={p['dex']}")
    assert tp, "expected at least one Base trending pool"

    sp = await gt.trending_pools("sol", limit=3)
    print(f"[geckoterminal] sol trending_pools: {len(sp)} -> {[p['name'] for p in sp]}")
    assert sp, "expected at least one Solana trending pool"

    np = await gt.new_pools("base", limit=2)
    print(f"[geckoterminal] base new_pools: {len(np)} -> {[p['name'] for p in np]}")

    weth = "0x4200000000000000000000000000000000000006"
    tok = await gt.token_info("base", weth)
    print(f"[geckoterminal] token_info WETH: sym={tok['symbol']} "
          f"price={tok['price_usd']} vol24h={tok['volume_24h_usd']}")
    assert tok and tok["symbol"] == "WETH", "WETH token_info failed"

    oh = await gt.pool_ohlcv("base", tp[0]["pool_address"], timeframe="day", limit=3)
    base_sym = oh["base"].get("symbol") if oh and oh["base"] else None
    print(f"[geckoterminal] pool_ohlcv: {len(oh['candles'])} candles, base={base_sym}")
    for c in oh["candles"]:
        print(f"  {c['t']}  o={c['o']} h={c['h']} l={c['l']} c={c['c']} v={c['v']}")
    assert oh and oh["candles"], "expected OHLCV candles"
    # chronological order
    ts = [c["t"] for c in oh["candles"]]
    assert ts == sorted(ts), "candles not chronological"

    bad = await gt.token_info("base", "0xdeadbeef")
    print(f"[geckoterminal] graceful unknown-token -> {bad}")
    assert bad is None, "unknown token should return None, not raise"


async def smoke_dexscreener() -> None:
    from tckr import dexscreener as ds

    weth = "0x4200000000000000000000000000000000000006"
    tp = await ds.token_pairs(weth, chain="base")
    print(f"\n[dexscreener] token_pairs WETH on base: {len(tp)}")
    for p in tp[:3]:
        bt = (p["base_token"] or {}).get("symbol")
        print(f"  {bt}/{(p['quote_token'] or {}).get('symbol')} "
              f"dex={p['dex']} price={p['price_usd']} liq={p['liquidity_usd']}")
    assert tp and all(p["chain"] == "base" for p in tp), "base chain filter failed"

    sr = await ds.search("SOL USDC", chain="solana")
    print(f"[dexscreener] search 'SOL USDC' on solana: {len(sr)} "
          f"-> {[(p['base_token'] or {}).get('symbol') for p in sr[:3]]}")
    assert all(p["chain"] == "solana" for p in sr), "solana chain filter failed"

    prof = await ds.latest_token_profiles()
    base_prof = await ds.latest_token_profiles(chain="base")
    sol_prof = await ds.latest_token_profiles(chain="solana")
    print(f"[dexscreener] token profiles: {len(prof)} total, "
          f"{len(base_prof)} base, {len(sol_prof)} solana")
    assert prof, "expected some token profiles"

    if tp:
        one = await ds.pair("base", tp[0]["pair_address"])
        print(f"[dexscreener] pair lookup -> {one['base_token']['symbol'] if one else None}")


async def smoke_hyperliquid() -> None:
    from tckr import hyperliquid as hl

    universe = await hl.perps_universe()
    print(f"\n[hyperliquid] perps_universe: {len(universe)} perps")
    assert universe, "expected at least one perp"

    btc = await hl.perp("BTC")
    eth = await hl.perp("ETH")
    sol = await hl.perp("SOL")
    for p in (btc, eth, sol):
        assert p is not None, "BTC/ETH/SOL must exist"
        print(f"  {p['symbol']:<6} mark={p['mark_px']} mid={p['mid_px']} "
              f"funding_hr={p['funding_rate_hourly']} apr={p['funding_apr_pct']:.2f}% "
              f"OI=${p['open_interest_usd']:,.0f} dayVol=${p['day_notional_volume_usd']:,.0f} "
              f"24hChg={p['day_change_pct']:.2f}%")

    mids = await hl.all_mids()
    print(f"[hyperliquid] all_mids: {len(mids)} tickers, "
          f"BTC mid={mids.get('BTC')}, ETH mid={mids.get('ETH')}")
    assert "BTC" in mids and "ETH" in mids

    fh = await hl.funding_history("BTC", hours=12)
    print(f"[hyperliquid] BTC funding_history (12h): {len(fh)} rows, latest={fh[-1] if fh else None}")
    assert fh, "expected funding history rows"

    book = await hl.l2_book("ETH", depth=3)
    print(f"[hyperliquid] ETH l2_book depth=3: bids={book['bids']} asks={book['asks']}")
    assert book and book["bids"] and book["asks"], "expected order book levels"


async def smoke_defillama() -> None:
    from tckr import defillama as dl

    cs = await dl.chains()
    print(f"\n[defillama] chains: {len(cs)} total")
    base = await dl.chain("base")
    sol = await dl.chain("solana")
    print(f"  Base TVL = ${base['tvl_usd']:,.0f}")
    print(f"  Solana TVL = ${sol['tvl_usd']:,.0f}")
    assert base and sol, "Base and Solana must be in DefiLlama"

    hist = await dl.chain_tvl_history("base")
    print(f"[defillama] Base TVL history: {len(hist)} days, "
          f"first={hist[0]['t']} last={hist[-1]['t']} "
          f"latest_tvl=${hist[-1]['tvl_usd']:,.0f}")

    top_base = await dl.protocols("base", min_tvl_usd=10_000_000, limit=5)
    print("[defillama] top 5 Base protocols (>=$10M TVL):")
    for p in top_base:
        print(f"  {p['name']:<28} {p['category']:<15} tvl=${p['tvl_usd']:,.0f} 7dChg={p['change_7d']}")
    assert top_base, "expected Base protocols"

    dexs = await dl.dex_overview("base")
    print(f"[defillama] Base DEX overview: 24h=${dexs['total_24h']:,.0f}, "
          f"top DEX: {dexs['protocols'][0]['name']} (${dexs['protocols'][0]['total_24h']:,.0f})")

    sc = await dl.stablecoins("base")
    print(f"[defillama] Base stablecoins: {len(sc)} -> top 3: "
          f"{[(s['symbol'], int(s['chain_circulating_usd'] or 0)) for s in sc[:3]]}")

    yp = await dl.yields("base", min_tvl_usd=5_000_000, limit=3)
    print("[defillama] top 3 Base yields (>=$5M TVL):")
    for y in yp:
        print(f"  {y['project']:<20} {y['symbol']:<15} apy={y['apy']}% tvl=${y['tvl_usd']:,.0f}")


async def smoke_alchemy() -> None:
    import os
    if not os.environ.get("ALCHEMY_API_KEY", "").strip():
        print("\n[alchemy] SKIPPED — ALCHEMY_API_KEY not set in env")
        return
    from tckr import alchemy as al

    vitalik = "0xd8dA6BF26964aF9D7eeD9e03E53415D37aA96045"
    eth = await al.native_balance(vitalik, network="base")
    print(f"\n[alchemy] vitalik native ETH on Base: {eth}")
    assert eth is not None, "expected an ETH balance value"

    holdings = await al.token_balances(vitalik, network="base",
                                       hide_zero=True, max_tokens=10)
    print("[alchemy] vitalik top-10 Base ERC-20 holdings (by raw):")
    for t in holdings:
        sym = t.get("symbol") or "?"
        bal = t.get("balance")
        bal_str = f"{bal:.6g}" if isinstance(bal, (int, float)) else "?"
        print(f"  {sym:<10} bal={bal_str:<15} contract={t['contract']}")
    assert holdings, "expected at least one ERC-20 holding for Vitalik on Base"

    xfers = await al.transfers(vitalik, network="base",
                               direction="out", limit=3)
    print(f"[alchemy] vitalik recent outbound transfers on Base ({len(xfers)}):")
    for x in xfers:
        print(f"  {x['block_ts']} {x['asset']:<8} value={x['value']} -> {x['to']}")


async def smoke_helius() -> None:
    import os
    if not os.environ.get("HELIUS_API_KEY", "").strip():
        print("\n[helius] SKIPPED — HELIUS_API_KEY not set in env")
        return
    from tckr import helius as he

    sol_wallet = "86xCnPeV69n6t3DnyGvkKobf9FdN2H9oiVDdaMpo2MMY"
    sol = await he.native_balance(sol_wallet)
    print(f"\n[helius] SOL native balance: {sol}")
    assert sol is not None, "expected a SOL balance value"

    holdings = await he.token_holdings(sol_wallet, limit=5)
    nat_sol = holdings["native_balance_sol"]
    print(f"[helius] token_holdings: native SOL={nat_sol} "
          f"(~${holdings['native_value_usd']:.2f}) total_assets={holdings['total']}")
    print(f"  top fungibles ({len(holdings['fungibles'])}):")
    for t in holdings["fungibles"][:5]:
        sym = t.get("symbol") or "?"
        bal = t.get("balance")
        bal_str = f"{bal:.6g}" if isinstance(bal, (int, float)) else "?"
        price = t.get("price_usd")
        value = t.get("value_usd")
        print(f"  {sym:<10} bal={bal_str:<14} price=${price} value=${value}")

    sigs = await he.transactions(sol_wallet, limit=3)
    print("[helius] last 3 signatures:")
    for s in sigs:
        err = "err" if s["err"] else "ok"
        print(f"  {s['block_ts']} {err:<4} slot={s['slot']} sig={s['signature'][:16]}…")


async def smoke_analytics() -> None:
    """Fetch-and-compute analytics tools end to end (keyless via HL cascade)."""
    from tckr.agent_toolkit.core import get_tool

    risk = await get_tool("ta_risk").callable({"symbol": "BTC", "days": 90})
    print(f"\n[analytics] ta_risk BTC: n={risk.get('n_bars')} src={risk.get('source')} "
          f"vol%={risk.get('annualized_volatility_pct')} sharpe={risk.get('sharpe')} "
          f"maxDD%={risk.get('max_drawdown_pct')}")
    assert risk.get("n_bars", 0) >= 2, "expected BTC daily candles for risk stats"

    ind = await get_tool("ta_indicators").callable({"symbol": "BTC", "days": 60})
    print(f"[analytics] ta_indicators BTC: last={ind.get('last_close')} "
          f"rsi14={ind.get('rsi_14')} sma20={ind.get('sma_20')} "
          f"atr14={ind.get('atr_14')} zscore={ind.get('zscore')}")
    assert "rsi_14" in ind, "expected RSI in indicators output"
    assert ind.get("atr_14") is not None, "BTC is HL-covered → ATR should populate from OHLC"

    corr = await get_tool("ta_correlation").callable(
        {"symbol": "ETH", "benchmark": "BTC", "days": 90})
    print(f"[analytics] ta_correlation ETH/BTC: n={corr.get('n_returns')} "
          f"corr={corr.get('correlation')} beta={corr.get('beta')}")
    assert corr.get("n_returns", 0) >= 2, "expected overlapping ETH/BTC history"


async def main() -> None:
    from tckr import _http

    try:
        await smoke_geckoterminal()
        await smoke_dexscreener()
        await smoke_hyperliquid()
        await smoke_defillama()
        await smoke_alchemy()
        await smoke_helius()
        await smoke_analytics()
        print("\nSMOKE OK")
    finally:
        await _http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
