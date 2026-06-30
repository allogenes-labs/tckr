#!/usr/bin/env python3
"""Keyless coverage harness — exercise the tckr agent toolkit across asset
archetypes with NO API keys, to map where the default-install (keyless) cascade
delivers data vs. goes dark.

This is a *measurement* tool, not a test that asserts. It drives the real
`tckr.agent_toolkit.core` tools (the exact callables the MCP / SDK adapters
expose) against a spread of assets — major/mid/long-tail crypto, Solana + Base
memecoins, US equities, an ETF, commodities, and a prediction market — and
records, per (asset x capability):

    status  ∈ {data | empty | error | n/a}
    source  (which provider answered, where the tool reports one)
    note    (a salient fact: price, n_bars, row count, error message)

Outputs (under the dir passed as argv[1], default ./coverage_out):
    raw/<ASSET>.json   full interpreted probe results per asset
    matrix.md          asset x capability grid (✓ / · / ✗ / blank)
    matrix.csv         same, machine-readable
    meta.json          capabilities() summary + final health() snapshot

Why scrub keys here: `tckr.settings` reads os.environ at import, and
`registry.configured()` keys off settings. To faithfully reproduce the
zero-config experience regardless of the operator's shell, we pop every known
tckr key env var BEFORE importing tckr. (Originals are stashed but never
re-injected — the whole run is keyless by construction.)

Run:
    python tests/coverage/keyless_coverage.py <out_dir>
Tune politeness (CoinGecko/GDELT free tiers rate-limit):
    COV_SLEEP=2.0  (seconds between probes; default 1.5)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# --- scrub keys BEFORE importing tckr (settings reads env at import time) ----
_KEY_ENV = [
    "ALCHEMY_API_KEY", "HELIUS_API_KEY", "COINALYZE_API_KEY", "BIRDEYE_API_KEY",
    "BANKR_API_KEY", "MORALIS_API_KEY", "BITQUERY_API_KEY", "NEYNAR_API_KEY",
    "COINGECKO_API_KEY", "COINGECKO_DEMO_API_KEY", "ETHERSCAN_API_KEY",
    "BASESCAN_API_KEY", "SOLSCAN_API_KEY", "LUNARCRUSH_API_KEY", "MESSARI_API_KEY",
    "TOKENTERMINAL_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY",
    "ALPACA_API_SECRET", "THEGRAPH_API_KEY",
]
_SCRUBBED = sorted(k for k in _KEY_ENV if os.environ.pop(k, None) is not None)

from tckr import registry  # noqa: E402
from tckr.agent_toolkit import core  # noqa: E402

SLEEP = float(os.environ.get("COV_SLEEP", "1.5"))
GDELT_EXTRA = 4.0  # GDELT soft limit ~1 req/5s — extra pause after each gdelt call

# Fixed capability columns (matrix order). Each maps to one probe label.
COLS = [
    "quote", "candles", "ta_risk", "ta_indicators", "ta_corr_btc",
    "hl_perp", "hl_candles", "ds_search", "dex_ohlcv",
    "oracle", "options", "news", "gdelt", "cg_search", "prediction",
]

# --- asset set (broadened across archetypes) --------------------------------
# kind drives which probes run. symbol = crypto-style ticker for the cascade/HL
# tools; pyth = Pyth "BASE/USD" feed symbol; name = free-text for news; chain =
# dexscreener hint for memecoins.
ASSETS = [
    {"id": "BTC",  "kind": "crypto-major",   "symbol": "BTC",  "pyth": "BTC/USD",  "name": "bitcoin"},
    {"id": "ETH",  "kind": "crypto-major",   "symbol": "ETH",  "pyth": "ETH/USD",  "name": "ethereum"},
    {"id": "SOL",  "kind": "crypto-major",   "symbol": "SOL",  "pyth": "SOL/USD",  "name": "solana"},
    {"id": "HYPE", "kind": "crypto-major",   "symbol": "HYPE", "pyth": "HYPE/USD", "name": "hyperliquid"},
    {"id": "RUNE", "kind": "crypto-mid",     "symbol": "RUNE", "pyth": "RUNE/USD", "name": "thorchain"},
    {"id": "MOG",  "kind": "crypto-longtail","symbol": "MOG",  "pyth": "MOG/USD",  "name": "mog coin"},
    {"id": "ANSEM","kind": "sol-memecoin",   "symbol": "ANSEM","chain": "solana",  "name": "ansem"},
    {"id": "MICHI","kind": "sol-memecoin",   "symbol": "MICHI","chain": "solana",  "name": "michi"},
    {"id": "DEGEN","kind": "base-memecoin",  "symbol": "DEGEN","chain": "base",    "name": "degen base"},
    {"id": "MU",   "kind": "equity",         "symbol": "MU",   "pyth": "MU/USD",   "name": "micron technology"},
    {"id": "AAPL", "kind": "equity",         "symbol": "AAPL", "pyth": "AAPL/USD", "name": "apple inc"},
    {"id": "NVDA", "kind": "equity",         "symbol": "NVDA", "pyth": "NVDA/USD", "name": "nvidia"},
    {"id": "SPY",  "kind": "etf",            "symbol": "SPY",  "pyth": "SPY/USD",  "name": "S&P 500 ETF"},
    {"id": "XAU",  "kind": "commodity",      "symbol": "XAU",  "pyth": "XAU/USD",  "name": "gold price"},
    {"id": "XAG",  "kind": "commodity",      "symbol": "XAG",  "pyth": "XAG/USD",  "name": "silver price"},
    {"id": "WTI",  "kind": "commodity",      "symbol": "WTI",  "pyth": "WTI/USD",  "name": "crude oil price"},
    {"id": "PREDICTION", "kind": "prediction", "name": "polymarket"},
]

CRYPTO_KINDS = {"crypto-major", "crypto-mid", "crypto-longtail"}
MEME_KINDS = {"sol-memecoin", "base-memecoin"}
TRADFI_KINDS = {"equity", "etf", "commodity"}


def _call(name: str, args: dict):
    tool = core.get_tool(name)
    if tool is None:
        raise RuntimeError(f"tool {name!r} not registered")
    return tool.callable(args)


def _gt_network(ds_chain: str | None) -> str | None:
    """Map a dexscreener chain id to a geckoterminal network id."""
    if not ds_chain:
        return None
    return {"ethereum": "eth"}.get(ds_chain, ds_chain)


# --- result interpretation --------------------------------------------------

def interpret(label: str, asset: dict, res) -> tuple[str, str, str | None]:
    """Return (status, note, source) for a probe result."""
    sym = (asset.get("symbol") or "").upper()
    try:
        if label == "quote":
            row = (res or {}).get(sym) if isinstance(res, dict) else None
            if row:
                return "data", f"${row.get('price')}", row.get("source")
            return "empty", "unresolved", None
        if label == "candles":
            row = (res or {}).get(sym) if isinstance(res, dict) else None
            closes = (row or {}).get("closes") or []
            if closes:
                return "data", f"{len(closes)} closes", row.get("source")
            return "empty", "unresolved", None
        if label in ("ta_risk", "ta_indicators"):
            if not isinstance(res, dict):
                return "empty", "None", None
            if res.get("error"):
                return "empty", res["error"], res.get("source")
            if (res.get("n_bars") or 0) >= 2:
                return "data", f"n_bars={res.get('n_bars')}", res.get("source")
            return "empty", "no bars", res.get("source")
        if label == "ta_corr_btc":
            if not isinstance(res, dict):
                return "empty", "None", None
            if res.get("error"):
                return "empty", res["error"], None
            return "data", f"corr={res.get('correlation')}, beta={res.get('beta')}", None
        if label == "hl_perp":
            if isinstance(res, dict) and res.get("mark_px") is not None:
                return "data", f"mark={res.get('mark_px')}", "hyperliquid"
            return "empty", "not on HL", None
        if label == "hl_candles":
            candles = (res or {}).get("candles") if isinstance(res, dict) else None
            if candles:
                return "data", f"{len(candles)} candles", "hyperliquid"
            return "empty", "not on HL", None
        if label == "ds_search":
            if isinstance(res, list) and res:
                matches = [r for r in res
                           if (r.get("base_token") or {}).get("symbol", "").upper() == sym]
                addrs = {(r.get("base_token") or {}).get("address") for r in matches}
                chains = {r.get("chain") for r in matches}
                note = (f"{len(res)} pairs; {len(matches)} match {sym} "
                        f"across {len(addrs)} token(s)/{len(chains)} chain(s)")
                return "data", note, "dexscreener"
            return "empty", "no pairs", None
        if label == "dex_ohlcv":
            candles = (res or {}).get("candles") if isinstance(res, dict) else None
            if candles:
                return "data", f"{len(candles)} candles", "geckoterminal"
            return "empty", "no pool ohlcv", None
        if label == "oracle":
            if isinstance(res, list) and res and res[0].get("price") is not None:
                return "data", f"{res[0].get('symbol')}={res[0].get('price')}", "pyth"
            return "empty", "no feed", None
        if label == "options":
            exps = (res or {}).get("expirations") if isinstance(res, dict) else None
            if exps:
                return "data", f"{len(exps)} expiries", (res or {}).get("source")
            return "empty", "no chain", None
        if label in ("news", "gdelt"):
            if isinstance(res, list) and res:
                return "data", f"{len(res)} articles", None
            return "empty", "no articles", None
        if label == "cg_search":
            coins = (res or {}).get("coins") if isinstance(res, dict) else None
            if coins:
                return "data", f"{len(coins)} coins", "coingecko"
            return "empty", "no match", None
        if label == "prediction":
            if isinstance(res, dict) and res.get("question"):
                return "data", res.get("question")[:60], "polymarket"
            return "empty", "no market", None
    except Exception as e:  # interpretation must never crash the run
        return "error", f"interpret: {type(e).__name__}: {e}", None
    # generic fallback
    if res is None:
        return "empty", "None", None
    if isinstance(res, list):
        return ("data", f"{len(res)} rows", None) if res else ("empty", "[]", None)
    if isinstance(res, dict):
        return ("data", "dict", None) if res else ("empty", "{}", None)
    return "data", str(res)[:40], None


async def probe(results: dict, label: str, asset: dict, name: str, args: dict):
    t0 = time.monotonic()
    try:
        res = await _call(name, args)
        status, note, source = interpret(label, asset, res)
    except Exception as e:
        res, status, note, source = None, "error", f"{type(e).__name__}: {e}", None
    results[label] = {
        "status": status, "note": note, "source": source,
        "tool": name, "ms": int((time.monotonic() - t0) * 1000),
    }
    await asyncio.sleep(SLEEP + (GDELT_EXTRA if name == "gdelt_articles" else 0.0))
    return res


async def resolve_meme_pair(asset: dict, results: dict):
    """ds_search the memecoin symbol, record ambiguity, return the deepest pair."""
    sym = asset["symbol"]
    rows = await probe(results, "ds_search", asset, "ds_search",
                       {"query": sym, "chain": asset.get("chain"), "limit": 25})
    if not isinstance(rows, list) or not rows:
        return None
    matches = [r for r in rows
               if (r.get("base_token") or {}).get("symbol", "").upper() == sym.upper()]
    pool = matches or rows
    pool = [r for r in pool if r.get("liquidity_usd")]
    pool.sort(key=lambda r: r.get("liquidity_usd") or 0, reverse=True)
    return pool[0] if pool else None


async def run_asset(asset: dict) -> dict:
    kind = asset["kind"]
    results: dict = {}
    sym = asset.get("symbol")

    # --- crypto cascade + TA (run for crypto + memecoins to expose what the
    #     symbol-based cascade does; run a reduced set for tradfi to capture the
    #     blind-spot failure mode rather than skipping it silently) ---
    if kind in CRYPTO_KINDS or kind in MEME_KINDS:
        await probe(results, "quote", asset, "quote", {"symbols": [sym]})
        await probe(results, "candles", asset, "candles", {"symbols": [sym], "days": 90})
        await probe(results, "ta_risk", asset, "ta_risk", {"symbol": sym, "days": 90})
        await probe(results, "ta_indicators", asset, "ta_indicators", {"symbol": sym, "days": 90})
        if sym != "BTC":
            await probe(results, "ta_corr_btc", asset, "ta_correlation",
                        {"symbol": sym, "benchmark": "BTC", "days": 90})
        await probe(results, "hl_perp", asset, "hl_perp", {"symbol": sym})
        await probe(results, "hl_candles", asset, "hl_candles",
                    {"symbol": sym, "interval": "1d", "limit": 30})
        await probe(results, "cg_search", asset, "cg_search", {"query": sym})
    elif kind in TRADFI_KINDS:
        # demonstrate the crypto-only cascade failing on a non-crypto ticker
        await probe(results, "quote", asset, "quote", {"symbols": [sym]})
        await probe(results, "ta_risk", asset, "ta_risk", {"symbol": sym, "days": 90})
        await probe(results, "ta_indicators", asset, "ta_indicators", {"symbol": sym, "days": 90})

    # --- memecoin DEX path: resolve pair, then pool OHLCV ---
    if kind in MEME_KINDS:
        top = await resolve_meme_pair(asset, results)
        if top and top.get("pair_address"):
            net = _gt_network(top.get("chain"))
            await probe(results, "dex_ohlcv", asset, "gt_pool_ohlcv",
                        {"network": net, "pool_address": top["pair_address"],
                         "timeframe": "day", "limit": 30})
        else:
            results["dex_ohlcv"] = {"status": "empty", "note": "no resolvable pair",
                                    "source": None, "tool": "gt_pool_ohlcv", "ms": 0}

    # --- oracle price (crypto, equity, commodity — anything with a Pyth feed) ---
    if asset.get("pyth"):
        await probe(results, "oracle", asset, "py_latest_price", {"symbols": asset["pyth"]})

    # --- options (equities + ETF, keyless via CBOE) ---
    if kind in ("equity", "etf"):
        await probe(results, "options", asset, "opt_expirations", {"underlying": sym})

    # --- news: unified for everyone; GDELT macro for tradfi ---
    if kind != "prediction":
        await probe(results, "news", asset, "news", {"query": asset.get("name"), "limit": 15})
    if kind in TRADFI_KINDS:
        await probe(results, "gdelt", asset, "gdelt_articles",
                    {"query": asset.get("name"), "timespan": "1w", "limit": 15})

    # --- prediction market: discover top market, then fetch it ---
    if kind == "prediction":
        try:
            top = await _call("pm_top_volume", {"limit": 5})
            slug = (top[0].get("slug") if isinstance(top, list) and top else None)
        except Exception:
            slug = None
        if slug:
            await probe(results, "prediction", asset, "pm_market", {"slug_or_id": slug})
        else:
            results["prediction"] = {"status": "empty", "note": "no top market",
                                     "source": None, "tool": "pm_market", "ms": 0}

    return results


_SYMBOL = {"data": "✓", "empty": "·", "error": "✗"}


def build_matrix(all_results: dict) -> tuple[str, str]:
    header = ["asset", "kind", *COLS]
    md = ["| " + " | ".join(header) + " |",
          "|" + "|".join(["---"] * len(header)) + "|"]
    csv = [",".join(header)]
    for asset in ASSETS:
        r = all_results[asset["id"]]
        md_cells, csv_cells = [asset["id"], asset["kind"]], [asset["id"], asset["kind"]]
        for col in COLS:
            cell = r.get(col)
            if cell is None:
                md_cells.append("")
                csv_cells.append("n/a")
            else:
                md_cells.append(_SYMBOL.get(cell["status"], "?"))
                csv_cells.append(cell["status"])
        md.append("| " + " | ".join(md_cells) + " |")
        csv.append(",".join(csv_cells))
    legend = "\nLegend: ✓ data · empty/miss ✗ error  (blank = not applicable)\n"
    return "\n".join(md) + "\n" + legend, "\n".join(csv) + "\n"


async def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./coverage_out")
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    print(f"keyless run — scrubbed {len(_SCRUBBED)} key env vars: {_SCRUBBED}")
    print(f"SLEEP={SLEEP}s  assets={len(ASSETS)}  out={out}")

    all_results: dict = {}
    for i, asset in enumerate(ASSETS, 1):
        print(f"[{i}/{len(ASSETS)}] {asset['id']} ({asset['kind']}) ...", flush=True)
        res = await run_asset(asset)
        all_results[asset["id"]] = res
        (raw / f"{asset['id']}.json").write_text(
            json.dumps({"asset": asset, "results": res}, indent=2), encoding="utf-8")
        for label, cell in res.items():
            print(f"      {label:<14} {cell['status']:<6} {cell.get('source') or '':<12} {cell['note']}")

    md, csv = build_matrix(all_results)
    (out / "matrix.md").write_text(md, encoding="utf-8")
    (out / "matrix.csv").write_text(csv, encoding="utf-8")

    # capabilities + health snapshot (proves keyless state + transient vs structural)
    import tckr
    meta = {
        "scrubbed_keys": _SCRUBBED,
        "capabilities_summary": registry.capabilities()["summary"],
        "configured_modules": sorted(
            n for n in registry.REGISTRY if registry.configured(n)),
        "health": tckr.health(),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # Console may be cp1252 (Windows) — the matrix uses ✓/·; write went to file
    # already, so guard the stdout echo against UnicodeEncodeError.
    enc = (sys.stdout.encoding or "utf-8")
    print("\n=== MATRIX (also written to matrix.md) ===")
    print(md.encode(enc, errors="replace").decode(enc))
    print(f"configured (keyless) modules: {len(meta['configured_modules'])}/"
          f"{len(registry.REGISTRY)} -> {meta['configured_modules']}")


if __name__ == "__main__":
    asyncio.run(main())
