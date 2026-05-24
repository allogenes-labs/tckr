"""Wallet PnL — FIFO position tracking across Solana + Base.

Caller supplies a list of wallet addresses; this module fetches the swap
history, applies first-in-first-out lot accounting per token, and returns
realized + unrealized PnL with per-token breakdowns.

Built on top of existing tckr modules — no new external dependencies:

    Solana data  → helius.swap_history()  (parsed swaps with token transfers)
    Base data    → Moralis wallet swaps endpoint (parsed swaps with USD values)
    SOL prices   → birdeye.token_overview()  for unrealized PnL leg
    Base prices  → Moralis token-price endpoint  for unrealized PnL leg

Public surface:

    apply_fifo(transfers, current_prices)   pure-function FIFO calculator
    wallet_transactions(addresses, ...)     raw transfer ledger (FIFO input)
    wallet_pnl(addresses, ...)              the headline function — per-token
                                            realized + unrealized PnL summary

Chain detection is automatic: addresses matching `^0x[a-f0-9]{40}$` route to
Base; everything else (base58, typically 32-44 chars) routes to Solana. Pass
`chain="solana"` or `chain="base"` to override.

## Cost basis methodology

For each token a wallet ever held, this module builds a chronological list of
in/out events:

    BUY  (token received in a swap): qty + USD cost basis (from counter asset)
    SELL (token sent out in a swap): qty + USD proceeds  (from counter asset)

It then walks the list in time order. Each BUY opens a new FIFO lot at its
cost basis. Each SELL consumes the oldest open lots first and records the
delta as realized PnL. After all events, any remaining open lots contribute
to unrealized PnL using the supplied (or fetched) current price.

## Known gotchas (v1)

- **Bridges look like sells with $0 proceeds.** If a wallet bridges X tokens
  out of Solana, the on-chain record is "X tokens sent to a bridge contract"
  with no counter asset received in-wallet. FIFO will close those lots at
  proceeds=0 and book a 100% realized loss. Filter the transfer ledger
  manually if you need bridge-aware accounting.

- **Self-transfers between owned wallets look like sell+buy at the same
  price.** Net PnL impact is zero but realized PnL gets inflated. Pass all
  addresses in one entity together so the module can detect and skip
  intra-entity transfers (not yet implemented; first cut treats each address
  in isolation).

- **Per-chain only.** Wallets are computed independently per chain; this
  module does not consolidate a USD position across chains. Sum at the
  caller layer if needed.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from tckr import _http, birdeye, helius, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.wallet_pnl")

_cache = TTLCache()

# Address-shape detection.
_RE_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Canonical mints/addresses we always know how to value at $1 (stablecoins +
# wrapped-SOL handled via SOL pricing). Lowercased for EVM. Solana addresses
# are case-sensitive base58 so compared as-is.
_STABLE_USD_MINTS = {
    # Solana
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    # Base (EVM, lowercase)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # USDC
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",   # USDT (limited on Base)
}
_WSOL_MINT = "So11111111111111111111111111111111111111112"
_WETH_BASE = "0x4200000000000000000000000000000000000006"  # canonical Base WETH


# ---------- helpers ----------

def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def detect_chain(address: str) -> str | None:
    """Return 'base' for EVM-shaped, 'solana' for base58-shaped, None for empty."""
    s = (address or "").strip()
    if not s:
        return None
    return "base" if _RE_EVM.match(s) else "solana"


def _is_stable(mint_or_addr: str, chain: str) -> bool:
    if chain == "base":
        return mint_or_addr.lower() in {m.lower() for m in _STABLE_USD_MINTS}
    return mint_or_addr in _STABLE_USD_MINTS


# ---------- FIFO calculator (pure function) ----------

def apply_fifo(transfers: list[dict],
                current_prices: dict[str, float] | None = None) -> dict[str, dict]:
    """Pure FIFO accounting. Returns {token: {pnl row}} keyed by token mint/address.

    `transfers` is a list of dicts sorted ascending by `ts`:
        {ts, token, side, qty, usd_value}
    where side is 'buy' (token received) or 'sell' (token sent).

    `current_prices` maps token → current USD price for unrealized PnL.
    Tokens absent from the dict get unrealized_pnl_usd = None.

    Per-token output:
        {qty_open, qty_total_bought, qty_total_sold, avg_cost_basis_usd,
         realized_pnl_usd, unrealized_pnl_usd, current_price_usd, n_buys,
         n_sells, first_buy_iso, last_activity_iso}
    """
    prices = current_prices or {}
    lots: dict[str, deque] = defaultdict(deque)  # token -> deque of {qty, cost_per_unit}
    stats: dict[str, dict] = defaultdict(lambda: {
        "qty_total_bought": 0.0, "qty_total_sold": 0.0,
        "realized_pnl_usd": 0.0, "n_buys": 0, "n_sells": 0,
        "first_buy_iso": None, "last_activity_iso": None,
        "total_cost_basis_usd": 0.0,
    })

    for ev in sorted(transfers, key=lambda r: r.get("ts") or ""):
        tok = ev.get("token")
        qty = _f(ev.get("qty"))
        usd = _f(ev.get("usd_value"))
        side = ev.get("side")
        ts = ev.get("ts")
        if not tok or qty is None or qty <= 0 or side not in ("buy", "sell"):
            continue
        s = stats[tok]
        s["last_activity_iso"] = ts

        if side == "buy":
            cost_per = (usd / qty) if (usd is not None and qty > 0) else 0.0
            lots[tok].append({"qty": qty, "cost_per_unit": cost_per})
            s["qty_total_bought"] += qty
            s["total_cost_basis_usd"] += (usd or 0.0)
            s["n_buys"] += 1
            if s["first_buy_iso"] is None:
                s["first_buy_iso"] = ts
        else:  # sell
            proceeds_per = (usd / qty) if (usd is not None and qty > 0) else 0.0
            remaining = qty
            while remaining > 0 and lots[tok]:
                lot = lots[tok][0]
                consume = min(lot["qty"], remaining)
                pnl_per = proceeds_per - lot["cost_per_unit"]
                s["realized_pnl_usd"] += pnl_per * consume
                lot["qty"] -= consume
                remaining -= consume
                if lot["qty"] <= 1e-12:
                    lots[tok].popleft()
            # Anything left after lots are exhausted: untracked short — record
            # proceeds as pure gain (basis-less). Real shorts on these chains
            # are rare; this usually means we missed an earlier buy.
            if remaining > 1e-12:
                s["realized_pnl_usd"] += proceeds_per * remaining
            s["qty_total_sold"] += qty
            s["n_sells"] += 1

    # Finalize per-token rows.
    out: dict[str, dict] = {}
    for tok, s in stats.items():
        qty_open = sum(lot["qty"] for lot in lots[tok])
        open_basis_usd = sum(lot["qty"] * lot["cost_per_unit"] for lot in lots[tok])
        avg_basis = (open_basis_usd / qty_open) if qty_open > 1e-12 else None
        price = prices.get(tok)
        unrealized = None
        if price is not None and qty_open > 1e-12 and avg_basis is not None:
            unrealized = (price - avg_basis) * qty_open
        out[tok] = {
            "qty_open": qty_open,
            "qty_total_bought": s["qty_total_bought"],
            "qty_total_sold":   s["qty_total_sold"],
            "avg_cost_basis_usd": avg_basis,
            "realized_pnl_usd":   s["realized_pnl_usd"],
            "unrealized_pnl_usd": unrealized,
            "current_price_usd":  price,
            "n_buys":  s["n_buys"],
            "n_sells": s["n_sells"],
            "first_buy_iso":      s["first_buy_iso"],
            "last_activity_iso":  s["last_activity_iso"],
            "total_pnl_usd": (s["realized_pnl_usd"]
                              + (unrealized if unrealized is not None else 0.0)),
        }
    return out


# ---------- Solana ----------

def _swap_to_transfers(sw: dict, *, sol_price_usd: float | None) -> list[dict]:
    """Convert one Helius parsed swap into two FIFO transfer rows.

    Treats wSOL legs as cost basis via SOL/USD price; treats stablecoin legs
    at $1. For non-stable / non-SOL counter assets we leave usd_value=None
    (FIFO downstream will count quantity but not value).
    """
    out: list[dict] = []
    ts = sw.get("ts")
    sold = sw.get("sold")
    bought = sw.get("bought")
    if not (sold and bought):
        return out

    def _usd_of(leg: dict) -> float | None:
        mint = leg.get("mint")
        amt = leg.get("amount") or 0.0
        if mint == _WSOL_MINT and sol_price_usd:
            return amt * sol_price_usd
        if _is_stable(mint, "solana"):
            return amt
        return None  # unknown counter asset

    counter_usd_in  = _usd_of(sold)    # USD value the wallet paid
    counter_usd_out = _usd_of(bought)  # USD value the wallet received

    # The TOKEN of interest is the non-stable / non-SOL leg. We emit a buy
    # for the bought leg with cost = USD paid (counter_usd_in), and a sell
    # for the sold leg with proceeds = USD received (counter_usd_out).
    out.append({"ts": ts, "token": bought["mint"], "side": "buy",
                "qty": bought["amount"], "usd_value": counter_usd_in})
    out.append({"ts": ts, "token": sold["mint"], "side": "sell",
                "qty": sold["amount"], "usd_value": counter_usd_out})
    return out


async def _solana_transfers(address: str, *, lookback_days: int) -> list[dict]:
    """Fetch Solana swap history → flatten into FIFO transfer rows.

    Normalizes ATA → owner wallet up-front so callers can pass either kind
    of address (Bitquery's trade endpoints frequently return ATAs).
    """
    owner, kind = await helius.resolve_owner(address)
    if kind == "ata":
        log.info("wallet_pnl: resolved ATA %s -> owner %s", address[:8], owner[:8])
    swaps = await helius.swap_history(owner, limit=100)
    if not swaps:
        return []
    # Filter by lookback window.
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat()
    swaps = [s for s in swaps if (s.get("ts") or "") >= cutoff]

    # SOL price for valuing wSOL legs. One lookup per call.
    sol_ov = await birdeye.token_overview(_WSOL_MINT, chain="solana")
    sol_price = _f((sol_ov or {}).get("price_usd"))

    transfers: list[dict] = []
    for sw in swaps:
        transfers.extend(_swap_to_transfers(sw, sol_price_usd=sol_price))
    return transfers


# ---------- Base (Moralis) ----------

async def _moralis_get(path: str, *, params: dict | None = None,
                       label: str = "") -> object | None:
    if not settings.MORALIS_API_KEY:
        log.warning("MORALIS_API_KEY not set — wallet_pnl base path skipped")
        return None
    url = f"https://deep-index.moralis.io/api/v2.2{path}"
    headers = {"X-API-Key": settings.MORALIS_API_KEY, "accept": "application/json"}
    return await _http.get_json(url, params=params, headers=headers,
                                label=label or f"moralis {path}")


async def _base_transfers(address: str, *, lookback_days: int) -> list[dict]:
    """Fetch Base wallet swap history → flatten into FIFO transfer rows.

    Uses Moralis `/wallets/{addr}/swaps` which returns already-parsed swap
    records: {bought: {amount, address, usdAmount}, sold: {...}, transactionHash, blockTimestamp}.
    """
    body = await _moralis_get(f"/wallets/{address}/swaps",
                              params={"chain": "base", "limit": 100,
                                      "order": "DESC"},
                              label=f"moralis base swaps {address[:10]}")
    if not isinstance(body, dict):
        return []
    rows = body.get("result") or []
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat()
    transfers: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = r.get("blockTimestamp") or r.get("block_timestamp")
        if not ts or ts < cutoff:
            continue
        bought = r.get("bought") or {}
        sold = r.get("sold") or {}
        b_addr = (bought.get("address") or "").lower() or None
        s_addr = (sold.get("address") or "").lower() or None
        # Moralis returns the SOLD leg's amount/usdAmount as a negative number
        # (signed from the wallet's perspective). FIFO expects positive qty
        # with side='sell' carrying the direction — take abs().
        b_amt = _f(bought.get("amount"))
        s_amt = _f(sold.get("amount"))
        b_usd = _f(bought.get("usdAmount") or bought.get("usd_amount"))
        s_usd = _f(sold.get("usdAmount") or sold.get("usd_amount"))
        if s_amt is not None: s_amt = abs(s_amt)
        if s_usd is not None: s_usd = abs(s_usd)
        if b_amt is not None: b_amt = abs(b_amt)
        if b_usd is not None: b_usd = abs(b_usd)
        if b_addr and b_amt is not None:
            # Buy: USD cost is what we paid (sold leg's USD value).
            transfers.append({"ts": ts, "token": b_addr, "side": "buy",
                              "qty": b_amt, "usd_value": s_usd})
        if s_addr and s_amt is not None:
            # Sell: USD proceeds is what we received (bought leg's USD value).
            transfers.append({"ts": ts, "token": s_addr, "side": "sell",
                              "qty": s_amt, "usd_value": b_usd})
    return transfers


# ---------- pricing for unrealized PnL ----------

async def _fetch_prices(tokens: Iterable[str], chain: str) -> dict[str, float]:
    """Best-effort current prices for the unrealized PnL leg.

    Solana: per-mint birdeye.token_overview (one call per token). Stables and
    wSOL are short-circuited.
    Base: Moralis token price endpoint. Stables short-circuited.
    """
    prices: dict[str, float] = {}
    sol_price: float | None = None

    for t in set(tokens):
        if not t:
            continue
        # Stable short-circuit.
        if _is_stable(t, chain):
            prices[t] = 1.0
            continue
        # wSOL short-circuit (Solana only).
        if chain == "solana" and t == _WSOL_MINT:
            if sol_price is None:
                ov = await birdeye.token_overview(_WSOL_MINT, chain="solana")
                sol_price = _f((ov or {}).get("price_usd"))
            if sol_price is not None:
                prices[t] = sol_price
            continue

        if chain == "solana":
            ov = await birdeye.token_overview(t, chain="solana")
            p = _f((ov or {}).get("price_usd"))
            if p is not None:
                prices[t] = p
        elif chain == "base":
            body = await _moralis_get(f"/erc20/{t}/price",
                                       params={"chain": "base"},
                                       label=f"moralis base price {t[:10]}")
            if isinstance(body, dict):
                p = _f(body.get("usdPrice") or body.get("usd_price"))
                if p is not None:
                    prices[t] = p
    return prices


# ---------- public surface ----------

async def wallet_transactions(addresses: str | list[str], *,
                               chain: str = "auto",
                               lookback_days: int = 30) -> dict[str, list[dict]]:
    """Raw transfer ledger per address (the FIFO input).

    Returns {address: [transfer rows]}. Each transfer row:
        {ts, token, side, qty, usd_value}
    """
    addrs = [addresses] if isinstance(addresses, str) else list(addresses or [])
    out: dict[str, list[dict]] = {}
    for addr in addrs:
        addr = (addr or "").strip()
        if not addr:
            continue
        c = chain if chain in ("solana", "base") else detect_chain(addr)
        if c == "solana":
            transfers = await _solana_transfers(addr, lookback_days=lookback_days)
        elif c == "base":
            transfers = await _base_transfers(addr, lookback_days=lookback_days)
        else:
            transfers = []
        out[addr] = transfers
    return out


async def wallet_pnl(addresses: str | list[str], *,
                      chain: str = "auto",
                      lookback_days: int = 30,
                      prices: dict[str, float] | None = None,
                      include_counter_assets: bool = False) -> dict[str, dict]:
    """FIFO PnL per token, per address.

    Returns {address: {"chain": str, "tokens": {token: pnl_row}, "summary": {...}}}.

    `prices`: optional override for the unrealized-PnL price lookup. Maps
    token → current USD price. Anything missing from this dict is fetched
    live (birdeye for Solana, Moralis for Base). Pass an empty dict to
    skip live price fetching entirely.
    """
    addrs = [addresses] if isinstance(addresses, str) else list(addresses or [])
    transfers_by_addr = await wallet_transactions(addrs, chain=chain,
                                                    lookback_days=lookback_days)
    out: dict[str, dict] = {}
    for addr in addrs:
        addr = (addr or "").strip()
        if not addr:
            continue
        c = chain if chain in ("solana", "base") else detect_chain(addr)
        transfers = transfers_by_addr.get(addr, [])

        # Pricing: caller override wins; fetch live for anything missing.
        if prices is None:
            tokens_needing_price = {t["token"] for t in transfers if t.get("token")}
            live_prices = await _fetch_prices(tokens_needing_price, c or "solana")
        else:
            tokens_needing_price = ({t["token"] for t in transfers if t.get("token")}
                                     - set(prices.keys()))
            live_prices = (await _fetch_prices(tokens_needing_price, c or "solana")
                            if tokens_needing_price else {})
            live_prices.update(prices)

        per_token = apply_fifo(transfers, current_prices=live_prices)
        # By default, drop counter assets (wSOL on Solana, stablecoins on
        # both chains). These are means-of-exchange in swaps, not positions
        # the caller wants to evaluate. Pass include_counter_assets=True to
        # keep them in the output.
        if not include_counter_assets:
            counter = {_WSOL_MINT, _WETH_BASE.lower()} | {m for m in _STABLE_USD_MINTS}
            counter |= {m.lower() for m in _STABLE_USD_MINTS}
            per_token = {t: v for t, v in per_token.items()
                          if t not in counter and t.lower() not in counter}

        realized_total = sum((r["realized_pnl_usd"] or 0) for r in per_token.values())
        unrealized_total = sum((r["unrealized_pnl_usd"] or 0)
                                for r in per_token.values()
                                if r["unrealized_pnl_usd"] is not None)
        out[addr] = {
            "chain": c,
            "tokens": per_token,
            "summary": {
                "n_tokens": len(per_token),
                "realized_pnl_usd": realized_total,
                "unrealized_pnl_usd": unrealized_total,
                "total_pnl_usd": realized_total + unrealized_total,
                "ts": _now_iso(),
            },
        }
    return out
