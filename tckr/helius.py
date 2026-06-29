"""Helius API — Solana wallet data via standard RPC + DAS.

Free API key (set HELIUS_API_KEY). Endpoint: https://mainnet.helius-rpc.com/.

Public functions cover the wallet / whale-tracking subset:

    native_balance   SOL balance (in SOL, not lamports)
    token_holdings   all owned fungibles via DAS getAssetsByOwner — includes
                     native SOL balance, per-token symbol/decimals/balance, and
                     price/value when Helius has it
    transactions     recent transaction signatures

All calls degrade gracefully — missing key, network errors, or RPC errors
return [] / None / {} rather than raising.

Docs: https://docs.helius.dev/
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.helius")

_cache = TTLCache()


def _endpoint() -> str | None:
    if not settings.HELIUS_API_KEY:
        log.warning("helius: HELIUS_API_KEY not set")
        return None
    return f"https://mainnet.helius-rpc.com/?api-key={settings.HELIUS_API_KEY}"


def _ts_to_iso(secs) -> str | None:
    try:
        return datetime.fromtimestamp(int(secs), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def _rpc(method: str, params, *, label: str = ""):
    url = _endpoint()
    if not url:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = await _http.post_json(url, payload, label=label or f"helius {method}")
    if not isinstance(body, dict):
        return None
    if body.get("error"):
        log.warning("helius %s error: %s", method, body["error"])
        return None
    return body.get("result")


async def native_balance(address: str) -> float | None:
    """SOL balance for `address` (in SOL, not lamports)."""
    if not address:
        return None
    ck = ("native_balance", address)
    cached = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached
    result = await _rpc("getBalance", [address],
                        label=f"helius getBalance {address[:8]}…")
    if not isinstance(result, dict):
        return None
    lamports = result.get("value")
    if not isinstance(lamports, (int, float)):
        return None
    sol = lamports / 1_000_000_000
    _cache.put(ck, sol)
    return sol


def _parse_asset(raw: dict) -> dict:
    """Flatten a DAS asset row to {mint, symbol, name, decimals, balance,
    price_usd, value_usd, supply, interface}.
    """
    content_meta = (raw.get("content") or {}).get("metadata") or {}
    token_info = raw.get("token_info") or {}
    decimals = token_info.get("decimals")
    raw_balance = token_info.get("balance")
    balance: float | None = None
    if raw_balance is not None and decimals is not None:
        try:
            balance = float(raw_balance) / (10 ** int(decimals))
        except (TypeError, ValueError):
            balance = None
    price_info = token_info.get("price_info") or {}
    return {
        "mint": raw.get("id"),
        "interface": raw.get("interface"),
        "symbol": token_info.get("symbol") or content_meta.get("symbol"),
        "name": content_meta.get("name"),
        "decimals": decimals,
        "balance_raw": raw_balance,
        "balance": balance,
        "price_usd": _f(price_info.get("price_per_token")),
        "value_usd": _f(price_info.get("total_price")),
        "supply": token_info.get("supply"),
    }


async def token_holdings(address: str, *, limit: int = 100,
                         fungible_only: bool = True) -> dict:
    """All token holdings via Helius DAS getAssetsByOwner.

    Returns {native_balance_sol, native_value_usd, native_price_per_sol,
             fungibles: [...sorted by value_usd desc], total, page, limit}.
    """
    empty: dict = {
        "native_balance_sol": None,
        "native_value_usd": None,
        "native_price_per_sol": None,
        "fungibles": [],
        "total": 0,
        "page": 1,
        "limit": limit,
    }
    if not address:
        return empty
    ck = ("holdings", address, limit, fungible_only)
    cached = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached

    params = {
        "ownerAddress": address,
        "page": 1,
        "limit": max(1, min(limit, 1000)),
        "displayOptions": {
            "showFungible": True,
            "showNativeBalance": True,
        },
    }
    result = await _rpc("getAssetsByOwner", params,
                        label=f"helius getAssetsByOwner {address[:8]}…")
    if not isinstance(result, dict):
        # RPC error — return the empty shape but DON'T cache it, so a transient
        # failure doesn't suppress real holdings for the whole TTL.
        return empty

    fungibles = []
    for it in result.get("items") or []:
        if not isinstance(it, dict):
            continue
        iface = it.get("interface") or ""
        if fungible_only and iface not in ("FungibleToken", "FungibleAsset"):
            continue
        fungibles.append(_parse_asset(it))
    fungibles.sort(key=lambda r: r.get("value_usd") or 0, reverse=True)

    nat = result.get("nativeBalance") or {}
    nat_lamports = nat.get("lamports")
    nat_sol = (nat_lamports / 1_000_000_000) if isinstance(nat_lamports, (int, float)) else None

    out = {
        "native_balance_sol": nat_sol,
        "native_value_usd": _f(nat.get("total_price")),
        "native_price_per_sol": _f(nat.get("price_per_sol")),
        "fungibles": fungibles,
        "total": result.get("total"),
        "page": result.get("page"),
        "limit": result.get("limit"),
    }
    _cache.put(ck, out)
    return out


async def resolve_owner(address: str) -> tuple[str, str]:
    """If `address` is an SPL associated-token-account (ATA), return the owner
    wallet that controls it. If it's a regular wallet (or anything else),
    return as-is. Returns (resolved_address, kind) where kind is 'wallet'
    or 'ata'.

    Bitquery's trade endpoints often return ATA addresses where you might
    expect owner wallets; this normalizer lets callers feed either kind into
    swap_history / wallet_pnl without ceremony.
    """
    addr = (address or "").strip()
    if not addr or not settings.HELIUS_API_KEY:
        return addr, "wallet"
    info = await _rpc(
        "getAccountInfo",
        [addr, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        label=f"helius resolve_owner {addr[:8]}",
    )
    if not isinstance(info, dict):
        return addr, "wallet"
    value = info.get("value")
    if not isinstance(value, dict):
        return addr, "wallet"
    try:
        parsed = value["data"]["parsed"]
        if parsed.get("type") == "account":
            owner = parsed["info"].get("owner")
            if owner and owner != addr:
                return owner, "ata"
    except (KeyError, TypeError):
        pass
    return addr, "wallet"


async def swap_history(address: str, *, limit: int = 100,
                       before: str | None = None) -> list[dict]:
    """Parsed swap transactions for a Solana wallet via Helius Enhanced TX API.

    Helius classifies each tx (SWAP, TRANSFER, NFT_*, etc.) and for SWAP rows
    returns a structured `tokenTransfers` list with mints, amounts, and
    direction relative to the queried address. This sidesteps the need to
    parse Jupiter / Raydium / Orca instruction layouts ourselves.

    Returns a list of normalized swap events, newest first. Each row:
        {ts, signature, source, sold: {mint, amount}, bought: {mint, amount},
         fee_sol, native_fee_payer}
    `sold` is what the wallet sent out, `bought` is what it received. For
    SOL legs, mint = 'So11111111111111111111111111111111111111112' (wSOL).

    `before`: pagination — pass the last signature from a previous page to
    walk further back. None means "from latest."
    """
    if not (address or "").strip() or not settings.HELIUS_API_KEY:
        if not settings.HELIUS_API_KEY:
            log.warning("helius: HELIUS_API_KEY not set — swap_history skipped")
        return []
    capped = max(1, min(int(limit), 100))
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    params: dict = {"api-key": settings.HELIUS_API_KEY, "limit": capped,
                    "type": "SWAP"}
    if before:
        params["before"] = before
    body = await _http.get_json(url, params=params,
                                label=f"helius swap_history {address[:8]}")
    if not isinstance(body, list):
        return []
    wsol = "So11111111111111111111111111111111111111112"
    rows: list[dict] = []
    for tx in body:
        if not isinstance(tx, dict):
            continue
        # Aggregate the wallet's net balance changes across its associated
        # token accounts. `tokenTransfers` looks at individual legs (which
        # for AMMs route through PDAs that aren't the wallet itself);
        # `accountData[*].tokenBalanceChanges[*]` carries the userAccount
        # field — the actual owner of each ATA. We sum per mint to get
        # net change attributed to the wallet.
        net_by_mint: dict[str, float] = {}
        for ad in tx.get("accountData") or []:
            if not isinstance(ad, dict):
                continue
            for tbc in ad.get("tokenBalanceChanges") or []:
                if not isinstance(tbc, dict):
                    continue
                if tbc.get("userAccount") != address:
                    continue
                mint = tbc.get("mint")
                raw = tbc.get("rawTokenAmount") or {}
                amt_raw = raw.get("tokenAmount")
                decimals = raw.get("decimals")
                if mint is None or amt_raw is None or decimals is None:
                    continue
                try:
                    amt = int(amt_raw) / (10 ** int(decimals))
                except (TypeError, ValueError):
                    continue
                net_by_mint[mint] = net_by_mint.get(mint, 0.0) + amt
            # Native SOL change: read from the wallet's own accountData entry.
            if ad.get("account") == address:
                native_change = ad.get("nativeBalanceChange")
                if isinstance(native_change, (int, float)) and native_change != 0:
                    delta_sol = native_change / 1_000_000_000
                    net_by_mint[wsol] = net_by_mint.get(wsol, 0.0) + delta_sol

        # Subtract the tx fee from the SOL leg so it's not mistaken for proceeds.
        fee_sol = (_f(tx.get("fee")) or 0) / 1_000_000_000
        if tx.get("feePayer") == address and wsol in net_by_mint:
            net_by_mint[wsol] += fee_sol  # add back: nativeBalanceChange already deducted it

        # Identify the dominant negative (sold) and positive (bought) legs.
        sold = bought = None
        if net_by_mint:
            losers  = sorted([(m, v) for m, v in net_by_mint.items() if v < -1e-12],
                              key=lambda x: x[1])
            winners = sorted([(m, v) for m, v in net_by_mint.items() if v > 1e-12],
                              key=lambda x: -x[1])
            if losers:
                sold   = {"mint": losers[0][0], "amount": -losers[0][1]}
            if winners:
                bought = {"mint": winners[0][0], "amount":  winners[0][1]}

        rows.append({
            "ts": _ts_to_iso(tx.get("timestamp")),
            "signature": tx.get("signature"),
            "source": tx.get("source"),  # 'PUMP_FUN', 'PUMP_AMM', 'JUPITER', 'RAYDIUM', ...
            "sold": sold,
            "bought": bought,
            "fee_sol": fee_sol,
            "native_fee_payer": tx.get("feePayer"),
        })
    return rows


async def transactions(address: str, *, limit: int = 25) -> list[dict]:
    """Recent transaction signatures for `address`.

    Each: {signature, slot, block_ts, err, memo, confirmation_status}.
    """
    if not address:
        return []
    ck = ("transactions", address, limit)
    cached = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached
    result = await _rpc("getSignaturesForAddress",
                        [address, {"limit": max(1, min(limit, 1000))}],
                        label=f"helius getSignaturesForAddress {address[:8]}…")
    if not isinstance(result, list):
        return []
    rows = []
    for r in result:
        if not isinstance(r, dict):
            continue
        rows.append({
            "signature": r.get("signature"),
            "slot": r.get("slot"),
            "block_ts": _ts_to_iso(r.get("blockTime")),
            "err": r.get("err"),
            "memo": r.get("memo"),
            "confirmation_status": r.get("confirmationStatus"),
        })
    _cache.put(ck, rows)
    return rows
