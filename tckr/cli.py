"""tckr CLI — ad-hoc explorer for the data layer.

Run via `python -m tckr <command>` or, after `pip install -e .`, the
`tckr` script entry point.

Commands (`tckr <cmd> --help` for details):

    dex     DEX pools on a network (trending / new / top)
    token   token snapshot by contract address
    perps   Hyperliquid perps (top by OI, or named symbols)
    options US equity/ETF option chain + greeks (Alpaca; --expirations to list expiries)
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

from tckr import _ansi, settings

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


async def cmd_options(args) -> None:
    from tckr import cboe
    from tckr import options as opt

    # --source: auto cascades (Alpaca if keyed → keyless CBOE); explicit forces one.
    src = args.source

    if args.expirations:
        if src == "alpaca":
            exps = await opt.expirations(args.underlying)
        elif src == "cboe":
            exps = await cboe.expirations(args.underlying)
        else:
            exps = await opt.expirations_cascade(args.underlying)
        if not exps:
            print(f"# no options data for {args.underlying} "
                  f"(Alpaca needs ALPACA_API_KEY + ALPACA_API_SECRET; "
                  f"CBOE keyless fallback also returned nothing)")
            return
        sk = exps.get("strikes") or {}
        via = f"  via {exps['source']}" if exps.get("source") else ""
        print(f"# {exps['underlying']} expirations (n={len(exps['expirations'])})  "
              f"strikes {_fmt_num(sk.get('min'))}-{_fmt_num(sk.get('max'))}{via}\n")
        for e in exps["expirations"]:
            print(f"  {e}")
        return

    if src == "alpaca":
        chain = await opt.option_chain(args.underlying, expiration=args.exp,
                                       type=args.type, limit=args.limit)
    elif src == "cboe":
        chain = await cboe.option_chain(args.underlying, expiration=args.exp,
                                        type=args.type)
    else:
        chain = await opt.chain_cascade(args.underlying, expiration=args.exp,
                                        type=args.type, limit=args.limit)
    if chain is None:
        print(f"# no chain for {args.underlying} "
              f"(Alpaca needs ALPACA_API_KEY + ALPACA_API_SECRET; "
              f"CBOE keyless fallback also returned nothing)")
        return
    rows = chain["contracts"]
    label = f" exp={args.exp}" if args.exp else ""
    via = f"  source={chain['source']}" if chain.get("source") else ""
    print(f"# {chain['underlying']} options{label}  feed={chain['feed']}{via}  "
          f"n={chain['count']}\n")

    def _d(v, prec=2):  # fixed-decimal price/greek (not sig-figs like _fmt_num)
        return f"{float(v):.{prec}f}" if v is not None else "?"

    print(f"{'expiry':<12} {'type':<5} {'strike':<9} {'bid':<8} {'ask':<8} "
          f"{'last':<8} {'IV':<7} {'delta':<7}")
    for c in rows[:args.top]:
        print(f"{(c['expiration'] or '?'):<12} "
              f"{(c['type'] or '?'):<5} "
              f"{_d(c['strike']):<9} "
              f"{_d(c['bid']):<8} "
              f"{_d(c['ask']):<8} "
              f"{_d(c['last']):<8} "
              f"{_d(c['iv'], 3):<7} "
              f"{_d(c['delta'], 3):<7}")
    if len(rows) > args.top:
        print(f"\n  ... {len(rows) - args.top} more (raise --top)")


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


# --------------------------- status dashboard ---------------------------

# "ANSI Shadow" block logo, one line per _ansi.GRADIENT entry.
_TCKR_LOGO = [
    " ████████╗ ██████╗██╗  ██╗██████╗ ",
    " ╚══██╔══╝██╔════╝██║ ██╔╝██╔══██╗",
    "    ██║   ██║     █████╔╝ ██████╔╝",
    "    ██║   ██║     ██╔═██╗ ██╔══██╗",
    "    ██║   ╚██████╗██║  ██╗██║  ██║",
    "    ╚═╝    ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝",
]

# Plain-ASCII wordmark for consoles that can't encode the block glyphs.
_TCKR_LOGO_ASCII = [
    "  _   _    _           _  __  _ ",
    " | |_| |_ | |__  _ _  | |/ / | |",
    " |  _|  _|| / / | '_| |   <  |_|",
    "  \\__|\\__||_\\_\\ |_|   |_|\\_\\ (_)",
]

_TIER_COLOR = {
    "keyless-free": _ansi.CYAN,
    "keyed-free": _ansi.BLUE,
    "keyed-paid": _ansi.ORANGE,
}


def _can_encode_unicode() -> bool:
    """True if stdout can render the block-glyph logo / box rules."""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█╗─·↑✓✗".encode(enc)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def _render_logo(color: bool, unicode_ok: bool) -> list[str]:
    art = _TCKR_LOGO if unicode_ok else _TCKR_LOGO_ASCII
    if not color:
        return list(art)
    grad = _ansi.GRADIENT
    return [_ansi.paint(line, grad[i % len(grad)], True) for i, line in enumerate(art)]


_NOTE_W = 48


def _short_note(note: str, uni: bool, width: int = _NOTE_W) -> str:
    note = note.replace("\n", " ").strip()
    ell = "…" if uni else "..."
    return note[: width - 1] + ell if len(note) > width else note


def _render_status(caps: dict, version: str, *, color: bool) -> str:
    uni = _can_encode_unicode()
    ok = "✓" if uni else "+"
    no = "✗" if uni else "x"
    arrow = "↑" if uni else ">"
    bullet = "·" if uni else "-"
    rule = ("─" if uni else "-") * 60

    mods = caps["modules"]
    summary = caps["summary"]
    name_w = max((len(n) for n in mods), default=8)

    out: list[str] = [""]
    out.extend("  " + ln for ln in _render_logo(color, uni))
    out.append("")
    tagline = (f"  tckr v{version}  {bullet}  async, cached market data across "
               f"{summary['total']} free APIs")
    out.append(_ansi.paint(tagline, _ansi.GREY, color))
    out.append("")

    active = [(n, m) for n, m in mods.items() if m["configured"]]
    locked = [(n, m) for n, m in mods.items() if not m["configured"]]

    def _row(mark: str, mark_code: str, name: str, tier: str, detail: str) -> str:
        tier_c = _ansi.paint(f"{tier:<12}", _TIER_COLOR.get(tier, ""), color)
        return (f"  {_ansi.paint(mark, mark_code, color)} "
                f"{name:<{name_w}}  {tier_c}  {detail}")

    # ---- ACTIVE ----
    hdr = f"ACTIVE {bullet} usable now ({len(active)})"
    out.append(_ansi.paint(hdr, _ansi.GREEN + _ansi.BOLD, color))
    for name, m in active:
        note = _short_note(m["notes"], uni)
        if m["expansion_keys"]:
            keys = " / ".join(m["expansion_keys"])
            hint = f"  {arrow} add {keys} for more"
            detail = f"{note:<{_NOTE_W + 1}}{_ansi.paint(hint, _ansi.YELLOW, color)}"
        else:
            detail = note
        out.append(_row(ok, _ansi.GREEN, name, m["tier"], detail))
    out.append("")

    # ---- LOCKED ----
    hdr = f"LOCKED {bullet} add a key to unlock ({len(locked)})"
    out.append(_ansi.paint(hdr, _ansi.RED + _ansi.BOLD, color))
    for name, m in locked:
        spec_required = bool(m["required_env"])
        joiner = " + " if spec_required else " or "
        keys = joiner.join(m["missing_keys"]) or "(unknown)"
        detail = _ansi.paint(f"needs {keys}", _ansi.YELLOW, color)
        out.append(_row(no, _ansi.RED, name, m["tier"], detail))
    out.append("")

    # ---- footer ----
    out.append(_ansi.paint("  " + rule, _ansi.GREY, color))
    by = summary["by_tier"]
    counts = (f"  {summary['configured']}/{summary['total']} ready"
              f"   keyless {by.get('keyless-free', 0)} {bullet}"
              f" keyed-free {by.get('keyed-free', 0)} {bullet}"
              f" paid {by.get('keyed-paid', 0)}"
              f"   ({summary['expandable']} expandable)")
    out.append(_ansi.paint(counts, _ansi.BOLD, color))
    cta = ("  Add keys to a .env file or your shell env to unlock more — "
           "see the README key table.")
    out.append(_ansi.paint(cta, _ansi.GREY, color))
    out.append("")
    return "\n".join(out)


def _emit(text: str) -> None:
    """Print without ever crashing on a console that can't encode a glyph."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


async def cmd_status(args) -> None:
    from tckr import __version__, registry
    if args.json:
        print(json.dumps(registry.capabilities(), indent=2))
        return
    # Best effort: UTF-8 stdout lets the block logo + ✓/✗ render on modern
    # consoles; the renderer falls back to ASCII glyphs if this can't stick.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    color = not args.no_color and _ansi.supports_color(sys.stdout)
    if color:
        _ansi.enable_windows_vt()
    latest = _check_for_update()
    if latest:
        banner = (f"→ tckr {latest} is available (you have {__version__}) — "
                  f"`pip install -U tckr`")
        _emit(_ansi.paint(banner, _ansi.YELLOW, color))
    _emit(_render_status(registry.capabilities(), __version__, color=color))


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

    sp = sub.add_parser("options",
                        help="equity/ETF/index option chain + greeks "
                             "(Alpaca if keyed, else keyless CBOE)")
    sp.add_argument("underlying", help="stock/ETF/index ticker, e.g. AAPL, SPY, SPX")
    sp.add_argument("--exp", help="expiration date YYYY-MM-DD")
    sp.add_argument("--type", choices=("call", "put"), help="filter to calls or puts")
    sp.add_argument("--source", choices=("auto", "alpaca", "cboe"), default="auto",
                     help="data source: auto cascades Alpaca→CBOE (default)")
    sp.add_argument("--expirations", action="store_true",
                     help="list available expirations + strike range instead of the chain")
    sp.add_argument("--limit", type=int, default=200,
                     help="contracts fetched per page (default 200, max 1000)")
    sp.add_argument("--top", type=int, default=25,
                     help="rows to print (default 25)")

    sp = sub.add_parser("tvl", help="DefiLlama chain TVL")
    sp.add_argument("chain", nargs="?",
                     help="optional chain name; default: top N by TVL")
    sp.add_argument("--top", type=int, default=15)

    sp = sub.add_parser("wallet", help="wallet holdings (on-chain)")
    sp.add_argument("chain", help="base | solana | eth")
    sp.add_argument("address")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("status", help="capability dashboard — what's usable now + what a key unlocks")
    sp.add_argument("--json", action="store_true",
                     help="emit JSON instead of the human-readable dashboard")
    sp.add_argument("--no-color", action="store_true",
                     help="disable ANSI color (also honors NO_COLOR / non-TTY)")

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
        "options": cmd_options,
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
