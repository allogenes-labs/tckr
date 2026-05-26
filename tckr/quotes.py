"""Unified crypto quote cascade — try the best available source per symbol.

tckr has many provider modules; for a simple "give me a USD price for symbol X"
question the canonical answer depends on which source has coverage and whether
the upstream is currently rate-limited. This module captures that decision
once, so every consumer (a CLI, a trading harness, an analytics notebook)
inherits the same fallback behavior.

Cascade order (per symbol):

  1. CoinGecko — broadest coverage (~14k coins), free tier rate-limits
     aggressively. Resolved via `coin_id_from_symbol → simple_price`.
  2. Hyperliquid — ~230 perp marks (BTC/ETH/SOL/NEAR/HYPE/RUNE/...).
     Cheap, no auth, no observed rate-limit at typical volume.
  3. (extensible) GeckoTerminal pool prices — for long-tail tokens addressable
     only on-chain. Not enabled by default because it needs an address lookup
     step; opt in via `get(symbols, allow_dex=True)`.

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


async def get(symbols: list[str] | str) -> dict[str, dict]:
    """Resolve USD prices for `symbols`, cascading CoinGecko → Hyperliquid.

    Returns `{symbol: {symbol, price, source, ts}}`. Symbols no source could
    resolve are absent. Each provider is allowed to fail independently; a
    CoinGecko 429 just means that symbol falls through to HL.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    syms = list(dict.fromkeys(syms))
    if not syms:
        return {}

    out: dict[str, dict] = {}

    async def _one(sym: str) -> None:
        # Primary: CoinGecko spot.
        px = await _cg_price(sym)
        if px is not None:
            out[sym] = {"symbol": sym, "price": px,
                        "source": "coingecko", "ts": _now_iso()}
            return
        # Fallback: Hyperliquid mark price.
        px = await _hl_price(sym)
        if px is not None:
            out[sym] = {"symbol": sym, "price": px,
                        "source": "hyperliquid", "ts": _now_iso()}
            return
        log.debug("quotes: unresolved %s", sym)

    await asyncio.gather(*(_one(s) for s in syms))
    return out


async def get_one(symbol: str) -> dict | None:
    """Single-symbol convenience over `get([symbol])`."""
    d = await get([symbol])
    return d.get(symbol.strip().upper()) if symbol else None
