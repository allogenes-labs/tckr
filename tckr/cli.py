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
    update  upgrade tckr to the latest PyPI release (--check to dry-run)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tckr import settings

# --------------------------- update check ---------------------------
#
# A soft, opt-out PyPI version check shown at the top of `tckr status`.
# Uses stdlib urllib (so it doesn't import httpx for CLI-only users) and
# caches the result on disk for 24h so we don't hit PyPI on every invocation.
# Set TCKR_NO_UPDATE_CHECK=1 to disable.

_UPDATE_CACHE_FILE = Path.home() / ".cache" / "tckr" / "version_check.json"
_UPDATE_CACHE_TTL = timedelta(hours=24)
_UPDATE_FETCH_TIMEOUT_S = 2.0


def _parse_version(v: str) -> tuple[int, ...]:
    """Loose semver parse — enough to compare numeric MAJOR.MINOR.PATCH.
    Non-numeric suffixes (rc1, dev, post1) are stripped per-segment."""
    parts: list[int] = []
    for seg in (v or "").split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _fetch_latest_pypi_version() -> str | None:
    import urllib.request

    from tckr import __version__
    req = urllib.request.Request(
        "https://pypi.org/pypi/tckr/json",
        headers={"User-Agent": f"tckr/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=_UPDATE_FETCH_TIMEOUT_S) as resp:  # noqa: S310
        data = json.loads(resp.read())
    return (data.get("info") or {}).get("version")


def _check_for_update() -> str | None:
    """Return the latest PyPI version of `tckr` if newer than installed,
    else None. Soft-fails on any error (offline, PyPI down, parse error)."""
    if os.environ.get("TCKR_NO_UPDATE_CHECK"):
        return None
    from tckr import __version__

    # Try the 24h disk cache first.
    try:
        if _UPDATE_CACHE_FILE.exists():
            cached = json.loads(_UPDATE_CACHE_FILE.read_text())
            ts = datetime.fromisoformat(cached.get("ts", ""))
            if datetime.now(UTC) - ts < _UPDATE_CACHE_TTL:
                latest = cached.get("latest")
                if latest and _parse_version(latest) > _parse_version(__version__):
                    return latest
                return None
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass  # cache unreadable — refetch

    try:
        latest = _fetch_latest_pypi_version()
    except Exception:  # noqa: BLE001 — never let an update check crash the CLI
        return None
    if not latest:
        return None

    try:
        _UPDATE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_CACHE_FILE.write_text(json.dumps({
            "ts": datetime.now(UTC).isoformat(),
            "latest": latest,
        }))
    except OSError:
        pass  # cache write best-effort

    return latest if _parse_version(latest) > _parse_version(__version__) else None


def _detect_install_method() -> tuple[str, str | None]:
    """Best-effort guess at how tckr was installed.

    Returns `(method, suggested_command)`. `method` ∈ {"pip", "pipx", "uv",
    "conda", "system"}. `suggested_command` is set when the user should use
    a non-pip tool, or None to mean "pip is fine".
    """
    exe = sys.executable.replace("\\", "/").lower()
    env = os.environ

    # pipx installs each app in ~/.local/pipx/venvs/<pkg>/  (or %LOCALAPPDATA%\pipx\venvs\<pkg>\)
    if "/pipx/venvs/tckr/" in exe:
        return "pipx", "pipx upgrade tckr"

    # uv tool installs land under uv-owned dirs
    if "/uv/tools/tckr/" in exe or "uv-tool" in exe:
        return "uv", "uv tool upgrade tckr"

    # Conda env — pip-in-conda still works, just warn rather than block
    cp = env.get("CONDA_PREFIX")
    if cp and sys.executable.startswith(cp):
        return "conda", None

    # PEP 668 externally-managed marker — usually means system Python on
    # Debian/Ubuntu/Fedora/etc. pip will refuse without --user or --break-system-packages.
    try:
        import sysconfig
        marker = Path(sysconfig.get_paths()["stdlib"]).parent / "EXTERNALLY-MANAGED"
        if marker.exists():
            return "system", f"{sys.executable} -m pip install --user --upgrade tckr"
    except (KeyError, OSError):
        pass

    return "pip", None


def _do_pip_upgrade() -> tuple[int, str]:
    """Run `pip install -U tckr` in the current interpreter. Streams pip's
    output to the user's terminal so they see progress. Returns (returncode,
    captured_stderr). Captures stderr in addition to streaming so we can
    suggest a fix for known failure modes like PEP 668."""
    import subprocess
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "tckr"]
    # Stream stdout for progress; capture stderr both ways (tee-like) so the
    # user sees errors AND we can pattern-match them. subprocess can't natively
    # tee, so we capture and replay.
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode, (proc.stderr or "")


async def cmd_update(args) -> None:
    from tckr import __version__

    # Always do a fresh fetch on explicit `tckr update` — the user asked.
    # We deliberately do NOT honor TCKR_NO_UPDATE_CHECK here; that env var
    # silences the implicit banner in `tckr status`, not explicit commands.
    try:
        latest = _fetch_latest_pypi_version()
    except Exception:  # noqa: BLE001
        print("error: could not reach PyPI to check for updates "
              "(offline, or pypi.org is unreachable)", file=sys.stderr)
        sys.exit(1)

    if not latest:
        print("error: PyPI returned no version for tckr", file=sys.stderr)
        sys.exit(1)

    is_newer = _parse_version(latest) > _parse_version(__version__)
    if not is_newer:
        print(f"tckr {__version__} is already up to date "
              f"(latest on PyPI: {latest}).")
        return

    if args.check:
        print(f"tckr {latest} is available (you have {__version__}). "
              f"Run `tckr update` to install.")
        return

    method, suggested = _detect_install_method()
    if method == "pipx":
        print("tckr was installed via pipx. Run:")
        print(f"  {suggested}")
        return
    if method == "uv":
        print("tckr was installed via `uv tool`. Run:")
        print(f"  {suggested}")
        return
    if method == "system":
        print("tckr is installed in a system-managed Python (PEP 668). "
              "pip will refuse a global upgrade.")
        print("Recommended (one of):")
        print("  pipx install --force tckr      # if you have pipx")
        print(f"  {sys.executable} -m pip install --user --upgrade tckr")
        return
    if method == "conda":
        print("note: detected a conda env. Trying pip — if it conflicts, "
              "use `conda update tckr` instead.")

    print(f"upgrading tckr {__version__} -> {latest}...\n")
    rc, stderr = _do_pip_upgrade()
    print()

    if rc == 0:
        # Invalidate the banner cache so a fresh `tckr status` doesn't keep
        # nagging about an update the user just installed.
        try:
            _UPDATE_CACHE_FILE.unlink()
        except OSError:
            pass
        print(f"upgraded to tckr {latest}.")
        return

    # pip failed — translate the most common errors into actionable hints.
    stderr_lower = stderr.lower()
    print(f"error: pip exited with code {rc}.", file=sys.stderr)
    if "externally-managed-environment" in stderr_lower or "pep 668" in stderr_lower:
        print("This Python is system-managed. Try:", file=sys.stderr)
        print("  pipx install --force tckr", file=sys.stderr)
        print(f"  {sys.executable} -m pip install --user --upgrade tckr",
              file=sys.stderr)
    elif "permission denied" in stderr_lower or "errno 13" in stderr_lower:
        print("Permission denied. Try a user-site install:", file=sys.stderr)
        print(f"  {sys.executable} -m pip install --user --upgrade tckr",
              file=sys.stderr)
    elif "no matching distribution" in stderr_lower:
        print(f"PyPI did not have a matching distribution for your Python "
              f"({sys.version_info.major}.{sys.version_info.minor}). "
              f"tckr requires Python 3.11+.", file=sys.stderr)
    sys.exit(1)


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
    from tckr import __version__, registry
    if args.json:
        print(json.dumps(registry.capabilities(), indent=2))
        return
    latest = _check_for_update()
    if latest:
        print(f"→ tckr {latest} is available (you have {__version__}) — "
              f"`pip install -U tckr`\n")
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

    sp = sub.add_parser("update", help="upgrade tckr to the latest PyPI release")
    sp.add_argument("--check", action="store_true",
                     help="only check whether a newer version exists; don't install")

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
        "update": cmd_update,
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
