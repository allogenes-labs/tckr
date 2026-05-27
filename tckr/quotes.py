"""Unified crypto quote cascade — try the best available source per symbol.

tckr has many provider modules; for a simple "give me a USD price for symbol X"
question the canonical answer depends on which source has coverage and whether
the upstream is currently rate-limited. This module captures that decision
once, so every consumer (a CLI, a trading harness, an analytics notebook)
inherits the same fallback behavior.

Cascade order (per symbol):

  1. Hyperliquid mark — if `symbol` is in HL's ~230-perp universe
     (BTC/ETH/SOL/NEAR/HYPE/RUNE/...). Live perp mark, no auth, no observed
     rate limit. For majors at low basis, ±bp of CG spot; far fresher.
  2. CoinGecko — broadest coverage (~14k coins) but free-tier rate-limits
     aggressively. Used for any symbol HL doesn't cover, and as a backstop
     when HL transiently returns no mark for a covered symbol.

The order was previously CG-first, but in production we saw CG 429-ing on
nearly every turn for the majors HL already covers — wasting requests and
serving older data than HL's live mark. The current order takes the fresher
source where it exists and only falls through to CG when HL truly can't
answer.

Returned shape per resolved symbol:

    {
      "symbol":  "NEAR",
      "price":   2.7421,
      "source":  "coingecko" | "hyperliquid" | "geckoterminal",
      "ts":      "2026-05-26T04:55:33+00:00",
    }

Symbols that no source can resolve are omitted from the result (the same
"absent rather than zero" contract every tckr fetcher follows).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from tckr import coingecko as cg
from tckr import hyperliquid as hl

log = logging.getLogger("tckr.quotes")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    """Resolve USD prices for `symbols`, cascading Hyperliquid → CoinGecko.

    Returns `{symbol: {symbol, price, source, ts}}`. Symbols no source could
    resolve are absent. HL is tried first for any symbol in its perp universe
    (fresher mark, no rate limit); CG handles the long-tail and acts as a
    backstop when HL transiently returns nothing for a covered symbol.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}

    hl_syms = await _hl_universe_syms()
    out: dict[str, dict] = {}

    async def _one(sym: str) -> None:
        # Prefer HL where it has coverage — live mark beats CG's cached spot
        # and isn't subject to the free-tier rate limit.
        if sym in hl_syms:
            px = await _hl_price(sym)
            if px is not None:
                out[sym] = {"symbol": sym, "price": px,
                            "source": "hyperliquid", "ts": _now_iso()}
                return
            # HL says it should cover this but didn't (auction/transient) —
            # fall through to CG rather than returning nothing.
        px = await _cg_price(sym)
        if px is not None:
            out[sym] = {"symbol": sym, "price": px,
                        "source": "coingecko", "ts": _now_iso()}
            return
        log.debug("quotes: unresolved %s", sym)

    await asyncio.gather(*(_one(s) for s in syms))
    return out


async def get_one(symbol: str) -> dict | None:
    """Single-symbol convenience over `get([symbol])`."""
    d = await get([symbol])
    return d.get(symbol.strip().upper()) if symbol else None
