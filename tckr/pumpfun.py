"""Pump.fun — Solana memecoin launchpad: discovery + bonding-curve state.

Pump.fun is where ~all Solana memecoin launches currently happen before they
migrate to PumpSwap (the in-house AMM that replaced the older Raydium-migration
path). For an early-stage trading thesis, the first hours on the bonding curve
are the asymmetry — this module exposes both layers.

Three backends, each covering a different surface:

1. **Discovery** — Moralis Solana gateway (REST) preferred, Bitquery (GraphQL)
   as fallback for `new_tokens` only (about_to_bond / recently_graduated are
   Moralis-only). Either key alone gets working discovery.

       new_tokens()          newly created tokens, most recent first
       about_to_bond()       tokens near bonding-curve completion
       recently_graduated()  tokens that completed and migrated to PumpSwap

   Each row carries `source: "moralis"` or `"bitquery"` so callers know the
   provenance. Requires `MORALIS_API_KEY` and/or `BITQUERY_API_KEY`.

2. **Per-token bonding-curve state** — Helius RPC, layout-independent.
   Queries the curve PDA's SPL token balance + SOL balance and derives
   curve % from the canonical formula. Does NOT decode the program's account
   IDL (pump.fun's BondingCurve layout grew from 49 to 151 bytes between
   versions and isn't safely decodable without the current anchor IDL).

       bonding_curve_pda(mint)             derive curve PDA (pure compute)
       bonding_state(curve_address, mint=) live curve state for one curve
       bonding_state_for_mint(mint)        same, with PDA derivation built in

   Requires `HELIUS_API_KEY`. Uses `solders` for PDA derivation.

3. **Bitquery-exclusive analytics** — GraphQL queries against the Bitquery
   Solana EAP schema. No Moralis equivalent for any of these.

       top_traders(mint)            wallets ranked by USD volume + net pos
       live_trades(mint)            recent trades with wallet/sol/USD/price
       migration_events(since_iso)  pump.fun -> PumpSwap migration events
       curve_trajectory(mint, h=)   per-trade price evolution + buy/sell counts
       holder_distribution(mint)    top N current holders by balance

   Requires `BITQUERY_API_KEY`.

Pump.fun program: `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`

Bonding-curve % formula (decimal-adjusted, 6-decimal tokens, 1B initial supply
with 793.1M offered on the curve and 206.9M reserved for migration LP):

    curve_progress_pct = 100 - (((token_balance - 206_900_000) * 100) / 793_100_000)

where `token_balance` is the SPL balance held by the curve PDA's associated
token account. This is the same calculation Bitquery uses; it survives any
IDL revisions of the bonding curve account itself.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from solders.pubkey import Pubkey

from tckr import _http, helius, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.pumpfun")

PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_PROGRAM_PUBKEY = Pubkey.from_string(PROGRAM_ID)

# Constants for the bonding-curve % calculation. All in decimal-adjusted token
# units (6 decimals), not raw smallest-unit. Initial real token reserves = 1B
# tokens = 793.1M offered on the curve + 206.9M reserved for LP at migration.
_INITIAL_TOKENS_ON_CURVE = 793_100_000
_RESERVED_TOKENS_FOR_LP  = 206_900_000
_TOKEN_DECIMALS          = 6
_LAMPORTS_PER_SOL        = 1_000_000_000

_MORALIS_BASE  = "https://solana-gateway.moralis.io/token/mainnet/exchange/pumpfun"
_BITQUERY_URL  = "https://streaming.bitquery.io/eap"

_cache = TTLCache()


# ---------- small parse helpers ----------

def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ts_to_iso(v) -> str | None:
    """Accept ISO string, unix seconds, or unix ms; return ISO. None passes through."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        ts = int(v)
        if ts > 10_000_000_000:  # treat as ms
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _curve_pct_from_token_balance(token_balance_decimal: float) -> float | None:
    """Apply the canonical pump.fun curve-progress formula. Clamped 0..100.

    Returns None when the balance is outside the range a standard 1B-supply
    token can produce (>1.05B or far below 0) — Pump.fun now allows creators
    to set custom supply, and the canonical formula isn't valid for those.
    Caller should fall back to the discovery row's `bonding_progress_pct`
    (which Moralis / Bitquery compute against the actual per-token supply).
    """
    if token_balance_decimal < -1 or token_balance_decimal > 1_050_000_000:
        return None
    pct = 100.0 - ((token_balance_decimal - _RESERVED_TOKENS_FOR_LP) * 100.0
                   / _INITIAL_TOKENS_ON_CURVE)
    return max(0.0, min(100.0, pct))


# ---------- PDA derivation ----------

def bonding_curve_pda(mint: str) -> str | None:
    """Derive the bonding-curve PDA for a Pump.fun mint.

    Seeds: [b"bonding-curve", mint_pubkey_bytes] under the pump.fun program.
    Returns the base58 address, or None if the mint string is invalid.
    """
    s = (mint or "").strip()
    if not s:
        return None
    try:
        mint_pk = Pubkey.from_string(s)
    except Exception:  # noqa: BLE001 — invalid base58 / wrong length
        return None
    pda, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)],
                                             _PROGRAM_PUBKEY)
    return str(pda)


# ---------- discovery (Moralis) ----------

async def _moralis_get(path: str, *, params: dict | None = None,
                       label: str = "") -> object | None:
    if not settings.MORALIS_API_KEY:
        return None
    url = f"{_MORALIS_BASE}/{path}"
    headers = {"X-API-Key": settings.MORALIS_API_KEY, "accept": "application/json"}
    return await _http.get_json(url, params=params, headers=headers,
                                label=label or f"moralis pumpfun {path}")


def _parse_moralis_token(r: dict) -> dict:
    """Normalize one Moralis pump.fun token row to the unified schema."""
    mint = r.get("tokenAddress")
    return {
        "source": "moralis",
        "mint": mint,
        "symbol": r.get("symbol"),
        "name": r.get("name"),
        "logo": r.get("logo"),
        "decimals": _i(r.get("decimals")),
        "price_sol": _f(r.get("priceNative")),
        "price_usd": _f(r.get("priceUsd")),
        "liquidity_usd": _f(r.get("liquidity")),
        "fdv_usd": _f(r.get("fullyDilutedValuation")),
        "bonding_progress_pct": _f(r.get("bondingCurveProgress")),
        # Moralis does not return the curve address — derive it from mint
        # so callers can hand it straight to bonding_state().
        "bonding_curve_address": bonding_curve_pda(mint) if mint else None,
    }


# ---------- discovery (Bitquery) ----------

_BITQUERY_NEW_TOKENS = """
query NewPumpfunTokens($limit: Int!) {
  Solana {
    TokenSupplyUpdates(
      where: {
        Instruction: { Program: {
          Address: { is: "%s" }
          Method:  { in: ["create", "create_v2"] }
        } }
      }
      limit: { count: $limit }
      orderBy: { descending: Block_Time }
    ) {
      Block { Time }
      TokenSupplyUpdate {
        Currency { Symbol Name MintAddress Decimals }
        PostBalance
      }
    }
  }
}
""" % PROGRAM_ID


_BITQUERY_TOP_TRADERS = """
query TopTraders($mint: String!, $limit: Int!) {
  Solana {
    DEXTradeByTokens(
      where: {
        Trade: {
          Dex: { ProtocolName: { is: "pump" } }
          Currency: { MintAddress: { is: $mint } }
        }
      }
      limit: { count: $limit }
      orderBy: { descendingByField: "volume_usd" }
    ) {
      Trade { Account { Address } }
      buy_amount:    sum(of: Trade_Side_AmountInUSD, if: {Trade: {Side: {Type: {is: buy}}}})
      sell_amount:   sum(of: Trade_Side_AmountInUSD, if: {Trade: {Side: {Type: {is: sell}}}})
      volume_usd:    sum(of: Trade_Side_AmountInUSD)
      trades:        count
    }
  }
}
"""


_BITQUERY_LIVE_TRADES_BUYS = """
query LiveTrades($mint: String!, $limit: Int!) {
  Solana {
    DEXTrades(
      where: {
        Trade: {
          Dex: { ProtocolName: { is: "pump" } }
          Buy:  { Currency: { MintAddress: { is: $mint } } }
        }
      }
      limit: { count: $limit }
      orderBy: { descending: Block_Time }
    ) {
      Block { Time }
      Transaction { Signature }
      Trade {
        Buy  { Account { Address } Amount Price PriceInUSD }
        Sell { Account { Address } Amount AmountInUSD Price }
      }
    }
  }
}
"""

# Mirror of the buys query with the mint on the Sell side: someone selling the
# token back for SOL. USD fields move with the legs (SOL leg = Buy here).
_BITQUERY_LIVE_TRADES_SELLS = """
query LiveTrades($mint: String!, $limit: Int!) {
  Solana {
    DEXTrades(
      where: {
        Trade: {
          Dex: { ProtocolName: { is: "pump" } }
          Sell: { Currency: { MintAddress: { is: $mint } } }
        }
      }
      limit: { count: $limit }
      orderBy: { descending: Block_Time }
    ) {
      Block { Time }
      Transaction { Signature }
      Trade {
        Buy  { Account { Address } Amount AmountInUSD Price }
        Sell { Account { Address } Amount Price PriceInUSD }
      }
    }
  }
}
"""


_BITQUERY_MIGRATION_EVENTS = """
query MigrationEvents($since: DateTime!, $limit: Int!) {
  Solana {
    Instructions(
      where: {
        Instruction: { Program: {
          Address: { is: "%s" }
          Method:  { in: ["migrate", "migrate_to_pump_swap", "withdraw"] }
        } }
        Block: { Time: { since: $since } }
      }
      limit: { count: $limit }
      orderBy: { descending: Block_Time }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Program { Method }
        Accounts { Address IsWritable }
      }
    }
  }
}
""" % PROGRAM_ID


_BITQUERY_CURVE_TRAJECTORY = """
query CurveTrajectory($mint: String!, $since: DateTime!, $limit: Int!) {
  Solana {
    DEXTradeByTokens(
      where: {
        Trade: {
          Dex: { ProtocolName: { is: "pump" } }
          Currency: { MintAddress: { is: $mint } }
        }
        Block: { Time: { since: $since } }
      }
      limit: { count: $limit }
      orderBy: { ascending: Block_Time }
    ) {
      Block { Time }
      Trade { Price PriceInUSD }
      volume_usd: sum(of: Trade_Side_AmountInUSD)
      buys:  count(if: {Trade: {Side: {Type: {is: buy}}}})
      sells: count(if: {Trade: {Side: {Type: {is: sell}}}})
    }
  }
}
"""


_BITQUERY_HOLDERS = """
query Holders($mint: String!, $limit: Int!) {
  Solana {
    BalanceUpdates(
      where: { BalanceUpdate: { Currency: { MintAddress: { is: $mint } } } }
      limit: { count: $limit }
      orderBy: { descendingByField: "BalanceUpdate_Balance_maximum" }
    ) {
      BalanceUpdate {
        Account { Address }
        Balance: PostBalance(maximum: Block_Slot)
      }
    }
  }
}
"""


async def _bitquery_post(query: str, variables: dict | None = None,
                          label: str = "") -> dict | None:
    if not settings.BITQUERY_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {settings.BITQUERY_API_KEY}",
        "content-type": "application/json",
    }
    body = await _http.post_json(_BITQUERY_URL,
                                 {"query": query, "variables": variables or {}},
                                 headers=headers, label=label or "bitquery")
    if not isinstance(body, dict):
        return None
    if body.get("errors"):
        log.warning("bitquery %s errors: %s", label, body["errors"])
        return None
    return body.get("data")


def _parse_bitquery_new_row(r: dict) -> dict:
    upd = (r.get("TokenSupplyUpdate") or {})
    cur = (upd.get("Currency") or {})
    blk = (r.get("Block") or {})
    return {
        "source": "bitquery",
        "mint": cur.get("MintAddress"),
        "symbol": cur.get("Symbol"),
        "name": cur.get("Name"),
        "logo": None,
        "decimals": _i(cur.get("Decimals")),
        "price_sol": None,
        "price_usd": None,
        "liquidity_usd": None,
        "fdv_usd": None,
        "bonding_progress_pct": None,
        "bonding_curve_address": (bonding_curve_pda(cur.get("MintAddress"))
                                   if cur.get("MintAddress") else None),
        "created_iso": _ts_to_iso(blk.get("Time")),
    }


# ---------- discovery (unified) ----------

async def _discover_moralis(path: str, capped: int, label: str) -> list[dict]:
    body = await _moralis_get(path, params={"limit": capped}, label=label)
    rows: list[dict] = []
    if isinstance(body, dict):
        result = body.get("result")
        if isinstance(result, list):
            rows = [_parse_moralis_token(r) for r in result if isinstance(r, dict)]
    elif isinstance(body, list):
        rows = [_parse_moralis_token(r) for r in body if isinstance(r, dict)]
    return rows


async def _discover_bitquery_new(capped: int) -> list[dict]:
    data = await _bitquery_post(_BITQUERY_NEW_TOKENS, {"limit": capped},
                                 label="bitquery new pumpfun")
    if not isinstance(data, dict):
        return []
    sol = data.get("Solana") or {}
    rows = sol.get("TokenSupplyUpdates") or []
    return [_parse_bitquery_new_row(r) for r in rows if isinstance(r, dict)]


async def _discovery(path: str, limit: int, *, label: str) -> list[dict]:
    """Shared logic: Moralis first, Bitquery fallback (new endpoint only for
    now — about_to_bond / recently_graduated only available via Moralis until
    we wire dedicated Bitquery queries for them)."""
    capped = max(1, min(int(limit), 100))
    ck = ("discovery", path, capped)
    cached = _cache.get(ck, settings.PUMPFUN_DISCOVERY_TTL_S)
    if cached is not None:
        return cached

    # Try Moralis first (it covers all three endpoints with the same shape).
    rows: list[dict] = []
    if settings.MORALIS_API_KEY:
        rows = await _discover_moralis(path, capped, label)

    # Bitquery fallback — currently only for `new` (the other two endpoints
    # need bespoke GraphQL queries that aren't worth the code until someone
    # actually loses Moralis access).
    if not rows and path == "new" and settings.BITQUERY_API_KEY:
        rows = await _discover_bitquery_new(capped)

    if not rows:
        if path == "new":
            if not settings.MORALIS_API_KEY and not settings.BITQUERY_API_KEY:
                log.warning("pumpfun.new: neither MORALIS_API_KEY nor BITQUERY_API_KEY set — skipped")
        else:
            # `bonding` / `graduated` are Moralis-only for now (Bitquery fallback
            # would need dedicated GraphQL queries against PumpFunPools).
            if not settings.MORALIS_API_KEY:
                log.warning("pumpfun.%s: MORALIS_API_KEY not set (Bitquery doesn't cover this endpoint) — skipped",
                            path)

    _cache.put(ck, rows)
    return rows


async def new_tokens(limit: int = 50) -> list[dict]:
    """Newly created Pump.fun tokens, most recent first."""
    return await _discovery("new", limit, label="new")


async def about_to_bond(limit: int = 50) -> list[dict]:
    """Tokens close to bonding-curve completion (about to migrate to PumpSwap)."""
    return await _discovery("bonding", limit, label="bonding")


async def recently_graduated(limit: int = 50) -> list[dict]:
    """Tokens that have completed the bonding curve and migrated to PumpSwap."""
    return await _discovery("graduated", limit, label="graduated")


# ---------- per-token bonding-curve state (Helius / on-chain) ----------

async def bonding_state(curve_address: str, *, mint: str | None = None,
                        sol_price_usd: float | None = None) -> dict | None:
    """Live state of a Pump.fun bonding curve via on-chain balance queries.

    Layout-independent: queries the curve PDA's SOL balance (getBalance) and
    its SPL token balance for the mint (getTokenAccountsByOwner), then
    derives curve % via the canonical formula. Avoids decoding the
    BondingCurve account IDL (which has churned between versions).

    `mint` is optional — pass it to also get the live token balance. Without
    it, you'll get SOL balance + an indication of whether the curve account
    exists, but curve % can't be computed.

    Returns None if Helius isn't keyed or the curve account doesn't exist.
    """
    addr = (curve_address or "").strip()
    if not addr:
        return None

    ck = ("bonding_state", addr, mint or "")
    cached = _cache.get(ck, settings.PUMPFUN_STATE_TTL_S)
    if cached is not None:
        sol_bal = cached.get("sol_balance_sol")
        usd = (sol_bal * sol_price_usd
               if (sol_bal is not None and sol_price_usd) else None)
        return {**cached, "sol_balance_usd": usd}

    # 1. Curve PDA owner + existence check. We require the account to be
    # owned by the pump.fun program — otherwise the derived PDA happens to
    # point at an unrelated account (e.g., a system-owned rent-dust account
    # for a non-pump.fun mint), and returning anything would be misleading.
    acc = await helius._rpc(
        "getAccountInfo",
        [addr, {"encoding": "base64", "commitment": "confirmed"}],
        label=f"pumpfun acc {addr[:8]}",
    )
    if not isinstance(acc, dict) or not isinstance(acc.get("value"), dict):
        return None  # account doesn't exist
    owner = acc["value"].get("owner")
    if owner != PROGRAM_ID:
        return None  # not a pump.fun bonding curve account

    # 2. SOL balance of the curve PDA.
    bal = await helius._rpc("getBalance", [addr, {"commitment": "confirmed"}],
                            label=f"pumpfun sol {addr[:8]}")
    sol_balance_lamports = (bal or {}).get("value") if isinstance(bal, dict) else None
    sol_balance_sol = (sol_balance_lamports / _LAMPORTS_PER_SOL
                       if isinstance(sol_balance_lamports, (int, float)) else None)

    # 3. Token balance (only meaningful if caller gave us the mint).
    token_balance_dec: float | None = None
    curve_pct: float | None = None
    if mint:
        toks = await helius._rpc(
            "getTokenAccountsByOwner",
            [addr, {"mint": mint},
             {"encoding": "jsonParsed", "commitment": "confirmed"}],
            label=f"pumpfun tok {addr[:8]}",
        )
        if isinstance(toks, dict):
            for it in toks.get("value") or []:
                try:
                    info = it["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    token_balance_dec = _f(info.get("uiAmount"))
                    break
                except (KeyError, TypeError):
                    continue
        if token_balance_dec is not None:
            curve_pct = _curve_pct_from_token_balance(token_balance_dec)

    # `complete` heuristic: curve PDA has lost almost all tokens (graduated)
    # OR curve_pct is at the canonical 99.99%+ floor for standard tokens. We
    # never claim graduated when curve_pct is None — that path is "unknown".
    is_complete = False
    if token_balance_dec is not None and token_balance_dec < 1.0:
        is_complete = True
    elif curve_pct is not None and curve_pct >= 99.99:
        is_complete = True

    out = {
        "curve_address": addr,
        "owner": owner,  # always PROGRAM_ID here — kept for cross-checking
        "mint": mint,
        "sol_balance_sol": sol_balance_sol,
        "sol_balance_usd": (sol_balance_sol * sol_price_usd
                            if (sol_balance_sol is not None and sol_price_usd) else None),
        "token_balance": token_balance_dec,
        "curve_progress_pct": curve_pct,
        "complete": is_complete,
        "ts": _now_iso(),
    }
    _cache.put(ck, out)
    return out


async def bonding_state_for_mint(mint: str, *,
                                  sol_price_usd: float | None = None) -> dict | None:
    """Same as `bonding_state` but derives the curve PDA from the mint.

    Convenience wrapper: most callers have the mint address (from discovery)
    and don't want to derive the PDA manually.
    """
    addr = bonding_curve_pda(mint)
    if not addr:
        return None
    return await bonding_state(addr, mint=mint, sol_price_usd=sol_price_usd)


# ---------- Bitquery-exclusive helpers ----------
# These have no Moralis equivalent. Each requires BITQUERY_API_KEY; without
# it they log a warning and return [] / None.

def _require_bitquery(label: str) -> bool:
    if not settings.BITQUERY_API_KEY:
        log.warning("pumpfun.%s: BITQUERY_API_KEY not set — skipped", label)
        return False
    return True


async def top_traders(mint: str, *, limit: int = 20) -> list[dict]:
    """Top wallets by USD volume traded on a specific Pump.fun token.

    Each row: {wallet, buy_volume_usd, sell_volume_usd, volume_usd, trades,
    net_position_usd}. `net_position_usd` is buy - sell — positive means the
    wallet is a net buyer (accumulating); negative means net seller. Sorted
    by total volume descending.

    Bitquery's `DEXTradeByTokens` aggregated by Trade.Account.Address.
    """
    if not (mint or "").strip() or not _require_bitquery("top_traders"):
        return []
    capped = max(1, min(int(limit), 100))
    ck = ("top_traders", mint, capped)
    cached = _cache.get(ck, settings.PUMPFUN_DISCOVERY_TTL_S)
    if cached is not None:
        return cached

    data = await _bitquery_post(_BITQUERY_TOP_TRADERS,
                                 {"mint": mint, "limit": capped},
                                 label=f"bitquery top_traders {mint[:8]}")
    rows: list[dict] = []
    if isinstance(data, dict):
        for r in (data.get("Solana") or {}).get("DEXTradeByTokens") or []:
            if not isinstance(r, dict):
                continue
            wallet = (((r.get("Trade") or {}).get("Account") or {}).get("Address"))
            buy_v  = _f(r.get("buy_amount"))   or 0.0
            sell_v = _f(r.get("sell_amount"))  or 0.0
            rows.append({
                "wallet": wallet,
                "buy_volume_usd": buy_v,
                "sell_volume_usd": sell_v,
                "volume_usd": _f(r.get("volume_usd")),
                "trades": _i(r.get("trades")),
                "net_position_usd": buy_v - sell_v,
            })
    _cache.put(ck, rows)
    return rows


async def live_trades(mint: str, *, limit: int = 50) -> list[dict]:
    """Recent buy/sell trades on one Pump.fun token, newest first.

    Each row: {ts, signature, side, wallet, sol_amount, token_amount,
    price_sol, price_usd, usd_amount}. `side` is 'buy' (someone bought the
    token with SOL) or 'sell' (someone sold it for SOL), determined by
    whether the mint is the Buy.Currency or Sell.Currency in the trade.
    """
    if not (mint or "").strip() or not _require_bitquery("live_trades"):
        return []
    capped = max(1, min(int(limit), 500))
    ck = ("live_trades", mint, capped)
    cached = _cache.get(ck, settings.PUMPFUN_STATE_TTL_S)
    if cached is not None:
        return cached

    buys_data, sells_data = await asyncio.gather(
        _bitquery_post(_BITQUERY_LIVE_TRADES_BUYS,
                       {"mint": mint, "limit": capped},
                       label=f"bitquery live_trades buys {mint[:8]}"),
        _bitquery_post(_BITQUERY_LIVE_TRADES_SELLS,
                       {"mint": mint, "limit": capped},
                       label=f"bitquery live_trades sells {mint[:8]}"),
    )

    def _parse(data, side: str) -> list[dict]:
        out: list[dict] = []
        if not isinstance(data, dict):
            return out
        for r in (data.get("Solana") or {}).get("DEXTrades") or []:
            if not isinstance(r, dict):
                continue
            blk = r.get("Block") or {}
            tx  = r.get("Transaction") or {}
            trade = r.get("Trade") or {}
            # The mint sits on the Buy leg for buys, the Sell leg for sells;
            # the other leg is SOL. USD value: prefer the SOL leg's
            # AmountInUSD (fresh price) over the token leg's PriceInUSD *
            # Amount (often unpriced for new tokens).
            if side == "buy":
                tok_leg = trade.get("Buy") or {}
                sol_leg = trade.get("Sell") or {}
            else:
                tok_leg = trade.get("Sell") or {}
                sol_leg = trade.get("Buy") or {}
            usd_amount = _f(sol_leg.get("AmountInUSD"))
            tok_amount = _f(tok_leg.get("Amount"))
            price_usd  = _f(tok_leg.get("PriceInUSD"))
            if usd_amount in (None, 0.0) and price_usd and tok_amount:
                usd_amount = price_usd * tok_amount
            elif price_usd is None and usd_amount and tok_amount:
                price_usd = usd_amount / tok_amount
            out.append({
                "ts": _ts_to_iso(blk.get("Time")),
                "signature": tx.get("Signature"),
                "side": side,
                "wallet": (tok_leg.get("Account") or {}).get("Address"),
                "token_amount": tok_amount,
                "sol_amount":   _f(sol_leg.get("Amount")),
                "usd_amount":   usd_amount,
                "price_sol":    _f(tok_leg.get("Price")),
                "price_usd":    price_usd,
            })
        return out

    rows = _parse(buys_data, "buy") + _parse(sells_data, "sell")
    rows.sort(key=lambda r: r["ts"] or "", reverse=True)
    rows = rows[:capped]
    _cache.put(ck, rows)
    return rows


async def migration_events(since_iso: str, *, limit: int = 100) -> list[dict]:
    """Tokens that bonded and migrated to PumpSwap since `since_iso`.

    `since_iso` is an ISO-8601 timestamp ("2026-05-23T00:00:00Z"). Each row:
    {ts, signature, method, mint_guess}. `mint_guess` is the first writable
    account in the migrate instruction — typically the mint, but verify
    against the discovery feed before relying on it.

    The actual on-chain method name has changed across pump.fun versions —
    this query tries `migrate`, `migrate_to_pump_swap`, and `withdraw` to
    catch the variants.
    """
    if not (since_iso or "").strip() or not _require_bitquery("migration_events"):
        return []
    capped = max(1, min(int(limit), 500))
    ck = ("migration_events", since_iso, capped)
    cached = _cache.get(ck, settings.PUMPFUN_DISCOVERY_TTL_S)
    if cached is not None:
        return cached

    data = await _bitquery_post(_BITQUERY_MIGRATION_EVENTS,
                                 {"since": since_iso, "limit": capped},
                                 label="bitquery migration_events")
    rows: list[dict] = []
    if isinstance(data, dict):
        for r in (data.get("Solana") or {}).get("Instructions") or []:
            if not isinstance(r, dict):
                continue
            instr = r.get("Instruction") or {}
            method = ((instr.get("Program") or {}).get("Method"))
            accounts = instr.get("Accounts") or []
            writable = [a.get("Address") for a in accounts
                        if isinstance(a, dict) and a.get("IsWritable")]
            rows.append({
                "ts": _ts_to_iso((r.get("Block") or {}).get("Time")),
                "signature": (r.get("Transaction") or {}).get("Signature"),
                "method": method,
                "mint_guess": writable[0] if writable else None,
            })
    _cache.put(ck, rows)
    return rows


async def curve_trajectory(mint: str, *, hours: int = 24,
                            limit: int = 500) -> list[dict]:
    """Per-trade price evolution for a Pump.fun token over the last N hours.

    Each row: {ts, price_sol, price_usd, volume_usd, buys, sells}. Caller
    can bucket client-side into OHLCV intervals as needed. Sorted ascending
    by time. Useful for charting bonding-curve price evolution and
    backtesting curve velocity.
    """
    if not (mint or "").strip() or not _require_bitquery("curve_trajectory"):
        return []
    capped_hours = max(1, min(int(hours), 24 * 30))
    capped_limit = max(1, min(int(limit), 1000))
    since_dt = datetime.now(UTC) - timedelta(hours=capped_hours)
    since_iso = since_dt.isoformat().replace("+00:00", "Z")

    ck = ("curve_trajectory", mint, capped_hours, capped_limit)
    cached = _cache.get(ck, settings.PUMPFUN_DISCOVERY_TTL_S)
    if cached is not None:
        return cached

    data = await _bitquery_post(_BITQUERY_CURVE_TRAJECTORY,
                                 {"mint": mint, "since": since_iso,
                                  "limit": capped_limit},
                                 label=f"bitquery curve_trajectory {mint[:8]}")
    rows: list[dict] = []
    if isinstance(data, dict):
        for r in (data.get("Solana") or {}).get("DEXTradeByTokens") or []:
            if not isinstance(r, dict):
                continue
            trade = r.get("Trade") or {}
            rows.append({
                "ts": _ts_to_iso((r.get("Block") or {}).get("Time")),
                "price_sol": _f(trade.get("Price")),
                "price_usd": _f(trade.get("PriceInUSD")),
                "volume_usd": _f(r.get("volume_usd")),
                "buys":  _i(r.get("buys")),
                "sells": _i(r.get("sells")),
            })
    _cache.put(ck, rows)
    return rows


async def holder_distribution(mint: str, *, limit: int = 20) -> list[dict]:
    """Top N holders of a Pump.fun token, ranked by current balance.

    Each row: {wallet, balance, pct_of_supply}. `pct_of_supply` assumes the
    standard 1B-token pump.fun supply — non-standard supplies will produce
    misleading percentages (the formula is `balance / 1_000_000_000 * 100`).
    For canonical supply lookup, cross-reference with the mint's on-chain
    metadata.

    Complements [[birdeye]] holder data, which only indexes graduated tokens
    — this works mid-curve.
    """
    if not (mint or "").strip() or not _require_bitquery("holder_distribution"):
        return []
    capped = max(1, min(int(limit), 200))
    ck = ("holder_distribution", mint, capped)
    cached = _cache.get(ck, settings.PUMPFUN_DISCOVERY_TTL_S)
    if cached is not None:
        return cached

    data = await _bitquery_post(_BITQUERY_HOLDERS,
                                 {"mint": mint, "limit": capped},
                                 label=f"bitquery holders {mint[:8]}")
    rows: list[dict] = []
    if isinstance(data, dict):
        for r in (data.get("Solana") or {}).get("BalanceUpdates") or []:
            if not isinstance(r, dict):
                continue
            bu = r.get("BalanceUpdate") or {}
            wallet = (bu.get("Account") or {}).get("Address")
            balance = _f(bu.get("Balance"))
            pct = (balance / 1_000_000_000.0 * 100.0) if balance is not None else None
            rows.append({
                "wallet": wallet,
                "balance": balance,
                "pct_of_supply": pct,
            })
    _cache.put(ck, rows)
    return rows
