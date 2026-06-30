"""Unified, asset-class-aware quote cascade — best source per symbol.

For a simple "give me a USD price for X" the right source depends on what X is:
a crypto major, a long-tail token, a US equity, gold, or a raw contract address.
This module makes that routing decision once so every consumer inherits it —
and, crucially, stops the crypto cascade from silently pricing a *same-ticker
crypto* when you asked for a stock or a metal.

Routing (per symbol):

  1. Contract address (0x… EVM / base58 Solana) → DexScreener deepest pool.
  2. Hyperliquid mark — if `symbol` is in HL's ~230-perp universe. Live, no key,
     no rate limit; freshest source for the majors it covers.
  3. Pyth oracle — if Pyth has a **non-crypto** feed for the symbol (equity, ETF,
     metal, FX, rates). This is the authoritative keyless price for tradfi assets
     and is preferred over CoinGecko, which would otherwise resolve e.g. "XAU" to
     a microcap "gold" token or "SPY" to an unrelated "SmartyPay" token.
  4. CoinGecko — broadest crypto coverage; the long-tail/backstop. When CG is the
     only source AND the asset class can't be verified, the result carries a
     `warning` so the agent doesn't trust a possible same-ticker token blindly.

Returned shape per resolved symbol:

    {
      "symbol":      "NEAR",
      "price":       2.7421,
      "source":      "hyperliquid" | "pyth" | "coingecko" | "dexscreener",
      "asset_class": "crypto" | "equity" | "metal" | "fx" | "rates" | "unknown",
      "ts":          "2026-05-26T04:55:33+00:00",
      "warning":     "…",   # present only when the class/source is unverified
    }

Symbols that no source can resolve are omitted (the "absent rather than zero"
contract every tckr fetcher follows).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

from tckr import coingecko as cg
from tckr import dexscreener as ds
from tckr import hyperliquid as hl
from tckr import pyth
from tckr import yahoo

log = logging.getLogger("tckr.quotes")

# EVM address: 0x + 40 hex. Solana mint: base58, ~32-44 chars (no 0OIl).
_EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _looks_like_address(s: str) -> bool:
    """True when `s` is a contract address rather than a ticker symbol."""
    s = (s or "").strip()
    return bool(_EVM_ADDR.match(s) or _SOL_ADDR.match(s))


async def _address_quote(addr: str) -> dict | None:
    """Price a raw token contract address via DexScreener's deepest pool."""
    pairs = await ds.token_pairs(addr)
    pairs = [p for p in (pairs or []) if p.get("price_usd") is not None]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: p.get("liquidity_usd") or 0.0)
    base = best.get("base_token") or {}
    return {
        "symbol": (base.get("symbol") or addr),
        "price": float(best["price_usd"]),
        "source": "dexscreener",
        "asset_class": "crypto",
        "token_address": base.get("address") or addr,
        "chain": best.get("chain"),
        "name": base.get("name"),
        "ts": _now_iso(),
    }


async def _pyth_noncrypto_quote(symbol: str) -> dict | None:
    """Pyth price for `symbol` iff Pyth classifies it as a non-crypto asset."""
    res = await pyth.resolve_asset(symbol)
    if not res or not res.get("noncrypto") or not res.get("feed_id"):
        return None
    rows = await pyth.latest_price([res["feed_id"]])
    if not rows or rows[0].get("price") is None:
        return None
    return {
        "symbol": symbol,
        "price": float(rows[0]["price"]),
        "source": "pyth",
        "asset_class": res["asset_type"],
        "ts": _now_iso(),
    }


async def _cg_price(symbol: str) -> float | None:
    cid = await cg.coin_id_from_symbol(symbol)
    if not cid:
        return None
    body = await cg.simple_price(cid, "usd")
    if not isinstance(body, dict):
        return None
    row = body.get(cid)
    if not isinstance(row, dict):
        return None
    px = row.get("usd")
    try:
        return float(px) if px is not None else None
    except (TypeError, ValueError):
        return None


async def _hl_price(symbol: str) -> float | None:
    """Hyperliquid perp mark for `symbol`, if it exists in the universe."""
    p = await hl.perp(symbol)
    if not p:
        return None
    # Explicit None check — `or` would silently swap a legitimate 0.0 mark
    # (auction/delisted edge state) for the mid.
    px = p.get("mark_px") if p.get("mark_px") is not None else p.get("mid_px")
    try:
        return float(px) if px is not None else None
    except (TypeError, ValueError):
        return None


async def _hl_universe_syms() -> set[str]:
    """Set of uppercase symbols HL has perp marks for. TTL-cached at hl layer."""
    perps = await hl.perps_universe()
    return {(p.get("symbol") or "").upper()
            for p in (perps or [])
            if p.get("symbol")}


async def get(symbols: list[str] | str) -> dict[str, dict]:
    """Resolve USD prices for `symbols`, routing by asset class.

    Per symbol: contract address → DexScreener; HL-universe ticker → Hyperliquid;
    Pyth non-crypto feed → Pyth (equities/metals/FX); otherwise CoinGecko (crypto
    long-tail / backstop). Returns `{key: {symbol, price, source, asset_class,
    ts, ...}}` keyed by the original input string (so an address key maps to its
    own result). Unresolvable inputs are absent. A CoinGecko-only result whose
    asset class can't be verified carries a `warning`.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    raw = [s.strip() for s in symbols if s and s.strip()]
    raw = list(dict.fromkeys(raw))
    if not raw:
        return {}

    hl_syms = await _hl_universe_syms()
    out: dict[str, dict] = {}

    async def _one(key: str) -> None:
        # 1. Raw contract address — price the deepest DEX pool, skip ticker logic.
        if _looks_like_address(key):
            r = await _address_quote(key)
            if r is not None:
                out[key] = r
            else:
                log.debug("quotes: unresolved address %s", key)
            return

        sym = key.upper()
        # 2. Hyperliquid — freshest for the majors it covers.
        if sym in hl_syms:
            px = await _hl_price(sym)
            if px is not None:
                out[key] = {"symbol": sym, "price": px, "source": "hyperliquid",
                            "asset_class": "crypto", "ts": _now_iso()}
                return
            # HL should cover it but didn't (auction/transient) — fall through.

        # 3. Pyth for non-crypto (equity/ETF/metal/FX/rates) — authoritative and
        #    keeps us from mis-pricing a tradfi ticker as a same-name token.
        r = await _pyth_noncrypto_quote(sym)
        if r is not None:
            out[key] = r
            return

        # 3b. Commodity Pyth doesn't carry (e.g. WTI crude) — Yahoo spot, so it
        #     still resolves keyless instead of falling to the crypto cascade.
        nc_class = yahoo.fallback_asset_class(sym)
        if nc_class:
            r = await yahoo.spot(sym, asset_class=nc_class)
            if r is not None:
                r["ts"] = _now_iso()
                out[key] = r
                return
            return  # confirmed non-crypto — don't mis-resolve via CoinGecko

        # 4. CoinGecko — crypto long-tail / backstop. Verify class via Pyth so an
        #    unverifiable resolution is flagged, not trusted silently.
        px = await _cg_price(sym)
        if px is not None:
            res = await pyth.resolve_asset(sym)
            asset_class = res["asset_type"] if res else "unknown"
            row = {"symbol": sym, "price": px, "source": "coingecko",
                   "asset_class": asset_class, "ts": _now_iso()}
            if asset_class in (None, "unknown"):
                row["asset_class"] = "unknown"
                row["warning"] = (
                    "resolved via the crypto cascade (CoinGecko) and the asset "
                    "class could not be verified — if you expected a non-crypto "
                    "asset, this may be a same-ticker token. Cross-check with "
                    "py_latest_price.")
            out[key] = row
            return
        log.debug("quotes: unresolved %s", key)

    await asyncio.gather(*(_one(s) for s in raw))
    return out


async def get_one(symbol: str) -> dict | None:
    """Single-symbol convenience over `get([symbol])`."""
    d = await get([symbol])
    return d.get(symbol.strip().upper()) if symbol else None
