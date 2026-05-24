"""tckr CLI — ad-hoc explorer for the data layer.

Run via `python -m tckr <command>` or, after `pip install -e .`, the
`tckr` script entry point.

Commands (`tckr <cmd> --help` for details):

    dex     DEX pools on a network (trending / new / top)
    token   token snapshot by contract address
    perps   Hyperliquid perps (top by OI, or named symbols)
    tvl     DefiLlama chain TVL (one chain + protocols, or top by TVL)
    wallet  on-chain wallet holdings (Base, Ethereum, or Solana)
    status  show which modules are configured + their tier
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from tckr import settings

# --------------------------- formatters ---------------------------

def _fmt_usd(v) -> str:
    if v is None:
        return "?"
    av = abs(v)
    if av >= 1e9:  return f"${v/1e9:.2f}B"
    if av >= 1e6:  return f"${v/1e6:.2f}M"
    if av >= 1e3:  return f"${v/1e3:.1f}K"
    return f"${v:.2f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "?"
    return f"{v:+.2f}%"


def _fmt_num(v, prec: int = 4) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.{prec}g}"
    except (TypeError, ValueError):
        return "?"


def _to_f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------- commands ---------------------------

async def cmd_dex(args) -> None:
    from tckr import geckoterminal as gt

    network = settings.normalize_network(args.network)
    if args.kind == "trending":
        pools = await gt.trending_pools(network, limit=args.limit)
        label = "trending"
    elif args.kind == "new":
        pools = await gt.new_pools(network, limit=args.limit)
        label = "new"
    else:
        pools = await gt.top_pools(network, limit=args.limit)
        label = "top by liquidity"
    print(f"# {network} pools ({label}, n={len(pools)})\n")
    print(f"{'pool':<28} {'dex':<22} {'price USD':<14} {'vol 24h':<12} {'1h':<8} {'24h':<8}")
    for p in pools:
        pc = p.get("price_change_pct") or {}
        print(f"{(p['name'] or '?')[:28]:<28} "
              f"{(p['dex'] or '')[:22]:<22} "
              f"{_fmt_num(p['price_usd']):<14} "
              f"{_fmt_usd(p['volume_24h_usd']):<12} "
              f"{_fmt_pct(_to_f(pc.get('h1'))):<8} "
              f"{_fmt_pct(_to_f(pc.get('h24'))):<8}")


async def cmd_token(args) -> None:
    from tckr import geckoterminal as gt

    network = settings.normalize_network(args.network)
    tok = await gt.token_info(network, args.address)
    if not tok:
        print(f"# token not found on {network}: {args.address}")
        return
    print(f"# {tok['symbol']} ({tok['name']}) on {network}")
    print(f"  address:    {tok['address']}")
    print(f"  price:      ${tok['price_usd']}")
    print(f"  FDV:        {_fmt_usd(tok['fdv_usd'])}")
    print(f"  market cap: {_fmt_usd(tok['market_cap_usd'])}")
    print(f"  24h volume: {_fmt_usd(tok['volume_24h_usd'])}")
    print(f"  reserves:   {_fmt_usd(tok['total_reserve_usd'])}")


async def cmd_perps(args) -> None:
    from tckr import hyperliquid as hl

    universe = await hl.perps_universe()
    if args.symbols:
        wanted = {s.upper() for s in args.symbols}
        rows = [p for p in universe if (p.get("symbol") or "").upper() in wanted]
    else:
        rows = sorted(universe,
                       key=lambda p: p.get("open_interest_usd") or 0,
                       reverse=True)[:args.top]
    print(f"# hyperliquid perps (n={len(rows)})\n")
    print(f"{'sym':<8} {'mark':<12} {'24h chg':<10} {'funding APR':<14} "
          f"{'OI USD':<10} {'24h vol':<10}")
    for p in rows:
        print(f"{(p['symbol'] or '?'):<8} "
              f"{_fmt_num(p['mark_px']):<12} "
              f"{_fmt_pct(p['day_change_pct']):<10} "
              f"{_fmt_pct(p['funding_apr_pct']):<14} "
              f"{_fmt_usd(p['open_interest_usd']):<10} "
              f"{_fmt_usd(p['day_notional_volume_usd']):<10}")


async def cmd_tvl(args) -> None:
    from tckr import defillama as dl

    if args.chain:
        c = await dl.chain(args.chain)
        if not c:
            print(f"# chain not found: {args.chain}")
            return
        print(f"# {c['name']} TVL = {_fmt_usd(c['tvl_usd'])}\n")
        prots = await dl.protocols(args.chain, min_tvl_usd=1_000_000, limit=10)
        print(f"# top protocols on {c['name']} (>= $1M TVL):")
        print(f"{'protocol':<30} {'category':<15} {'TVL':<10} {'7d':<8}")
        for p in prots:
            print(f"{(p['name'] or '?')[:30]:<30} "
                  f"{(p['category'] or '')[:15]:<15} "
                  f"{_fmt_usd(p['tvl_usd']):<10} "
                  f"{_fmt_pct(p['change_7d']):<8}")
    else:
        chains = await dl.chains()
        rows = chains[:args.top]
        print(f"# top {len(rows)} chains by TVL\n")
        print(f"{'chain':<22} {'TVL':<12} {'symbol':<10}")
        for c in rows:
            print(f"{(c['name'] or '?')[:22]:<22} "
                  f"{_fmt_usd(c['tvl_usd']):<12} "
                  f"{(c['token_symbol'] or '')[:10]:<10}")


async def cmd_wallet(args) -> None:
    chain = settings.normalize_network(args.chain)
    if chain == "solana":
        from tckr import helius as he
        holdings = await he.token_holdings(args.address, limit=args.limit)
        print(f"# Solana wallet {args.address}")
        nat = holdings.get("native_balance_sol")
        print(f"  native:    {nat} SOL  ({_fmt_usd(holdings.get('native_value_usd'))})")
        print(f"  fungibles: {len(holdings.get('fungibles') or [])} "
              f"/ total assets {holdings.get('total')}\n")
        print(f"{'token':<14} {'balance':<18} {'price USD':<14} {'value':<12}")
        for t in (holdings.get("fungibles") or [])[:args.limit]:
            print(f"{(t.get('symbol') or '?')[:14]:<14} "
                  f"{_fmt_num(t.get('balance')):<18} "
                  f"{_fmt_num(t.get('price_usd')):<14} "
                  f"{_fmt_usd(t.get('value_usd')):<12}")
    elif chain in ("base", "eth"):
        from tckr import alchemy as al
        native = await al.native_balance(args.address, network=chain)
        holdings = await al.token_balances(args.address, network=chain,
                                            hide_zero=True, max_tokens=args.limit)
        print(f"# {chain} wallet {args.address}")
        print(f"  native:    {native} ETH\n")
        print(f"  tokens ({len(holdings)}):")
        print(f"{'symbol':<14} {'balance':<24} {'contract':<46}")
        for t in holdings:
            print(f"{(t.get('symbol') or '?')[:14]:<14} "
                  f"{_fmt_num(t.get('balance')):<24} "
                  f"{(t.get('contract') or '')[:46]:<46}")
    else:
        print(f"# unsupported chain for wallet: {args.chain} "
              f"(use base, eth, or solana)")


async def cmd_status(args) -> None:
    from tckr import registry
    if args.json:
        import json
        print(json.dumps(registry.capabilities(), indent=2))
    else:
        print(registry.format_status())


# --------------------------- parser ---------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tckr",
        description="Ad-hoc explorer for the tckr layer "
                    "(DEX, perps, TVL, on-chain wallets).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("dex", help="DEX pools on a network")
    sp.add_argument("network", help="base | solana | eth (aliases ok)")
    sp.add_argument("--kind", default="trending",
                     choices=("trending", "new", "top"))
    sp.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("token", help="token info by contract address")
    sp.add_argument("network")
    sp.add_argument("address")

    sp = sub.add_parser("perps", help="Hyperliquid perps snapshot")
    sp.add_argument("symbols", nargs="*",
                     help="optional symbols; default: top N by open interest")
    sp.add_argument("--top", type=int, default=10)

    sp = sub.add_parser("tvl", help="DefiLlama chain TVL")
    sp.add_argument("chain", nargs="?",
                     help="optional chain name; default: top N by TVL")
    sp.add_argument("--top", type=int, default=15)

    sp = sub.add_parser("wallet", help="wallet holdings (on-chain)")
    sp.add_argument("chain", help="base | solana | eth")
    sp.add_argument("address")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("status", help="show registered modules + which are configured")
    sp.add_argument("--json", action="store_true",
                     help="emit JSON instead of the human-readable table")

    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "dex":    cmd_dex,
        "token":  cmd_token,
        "perps":  cmd_perps,
        "tvl":    cmd_tvl,
        "wallet": cmd_wallet,
        "status": cmd_status,
    }
    try:
        asyncio.run(handlers[args.cmd](args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
