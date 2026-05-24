"""Honeypot.is API — sell-simulation backstop for EVM tokens.

Goplus tells you what the contract LOOKS LIKE; honeypot.is actually attempts
a simulated swap to verify you can buy AND sell, returns the realized buy/sell
tax, and flags if a router blocks the exit. Use it as a second source —
contracts that pass Goplus can still fail in practice if a router has special
handling, and contracts that look scary in Goplus may simulate fine.

Free, no API key. Rate limit is generous (~50 req/min) but cache aggressively
since the result for a given contract barely changes intra-day.

Supports a useful subset of EVM chains via numeric chainID:
  1 Ethereum  56 BSC  8453 Base
  (other chains return "unsupported" or empty results)

Docs: https://honeypot.is/api
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.honeypot")

_BASE = "https://api.honeypot.is/v2"
_cache = TTLCache()

# Chain alias → honeypot.is chainID (subset of EVM chains the service supports).
_CHAIN_IDS: dict[str, int] = {
    "ethereum": 1, "eth": 1, "mainnet": 1,
    "bsc": 56, "bnb": 56, "binance": 56,
    "base": 8453,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _chain_id(chain: str) -> int | None:
    key = (chain or "").strip().lower()
    if key in _CHAIN_IDS:
        return _CHAIN_IDS[key]
    if key.isdigit():
        try:
            return int(key)
        except ValueError:
            return None
    return None


async def is_honeypot(chain: str, address: str) -> dict | None:
    """Simulate a buy + sell on `address` on `chain`. Returns a flattened
    report:
        {
          chain, address,
          is_honeypot, honeypot_reason, buy_tax, sell_tax, transfer_tax,
          can_buy, can_sell, max_buy_usd, max_sell_usd,
          simulation_success, simulation_error,
          ts,
        }
    `is_honeypot=True` is the hard signal — do not trade. `sell_tax > 0.10`
    is the soft signal — tradeable but the venue is extracting >10% on exit.
    """
    cid = _chain_id(chain)
    addr = (address or "").strip()
    if cid is None or not addr:
        return None
    ck = ("honeypot", cid, addr.lower())
    cached = _cache.get(ck, settings.HONEYPOT_TTL_S)
    if cached is not None:
        return cached

    body = await _http.get_json(
        f"{_BASE}/IsHoneypot",
        params={"address": addr, "chainID": cid},
        label=f"honeypot {cid}/{addr[:10]}",
    )
    if not isinstance(body, dict):
        return None

    honey = body.get("honeypotResult") or {}
    sim = body.get("simulationResult") or {}
    sim_meta = body.get("simulationSuccess")
    flags = body.get("flags") or []
    summary = body.get("summary") or {}

    out = {
        "chain": chain,
        "address": addr,
        "is_honeypot": bool(honey.get("isHoneypot")),
        "honeypot_reason": honey.get("honeypotReason"),
        "buy_tax": _f(sim.get("buyTax")),
        "sell_tax": _f(sim.get("sellTax")),
        "transfer_tax": _f(sim.get("transferTax")),
        "can_buy": bool(sim.get("buyGas")) and not honey.get("isHoneypot"),
        "can_sell": bool(sim.get("sellGas")) and not honey.get("isHoneypot"),
        "buy_gas": sim.get("buyGas"),
        "sell_gas": sim.get("sellGas"),
        "max_buy_usd": _f((summary.get("maxBuy") or {}).get("withTokenUsd")),
        "max_sell_usd": _f((summary.get("maxSell") or {}).get("withTokenUsd")),
        "simulation_success": bool(sim_meta),
        "simulation_error": body.get("simulationError"),
        "flags": flags,
        "risk_label": (summary.get("risk") or "").lower() or None,  # "low"/"medium"/"high"/"honeypot"
        "ts": _now_iso(),
    }
    _cache.put(ck, out)
    return out
