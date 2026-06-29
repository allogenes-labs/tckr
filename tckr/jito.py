"""Jito — Solana MEV intel: tip floor, bundle status, snipe detection.

Jito Labs operates the dominant MEV infrastructure on Solana. Searchers
submit atomic transaction "bundles" along with a SOL tip to one of 8
well-known tip accounts; validators running Jito-Solana run bundles before
regular txs in exchange for the tip. For the new-pair / early-stage trading
thesis, Jito presence is the canonical bot-vs-organic discriminator: a token
whose first 20 buys all carry Jito tips is being heavily sniped; one whose
first buys are regular txs is more organic.

Public surface:

    tip_floor()                      current MEV tip percentiles (25/50/75/95/99 + EMA)
    tip_accounts(refresh=False)      the 8 hardcoded Jito tip accounts
    bundle_status(bundle_ids)        historical bundle outcomes (slot, confirmation)
    inflight_bundle_status(bundle_ids)  recent (5-min) bundle status
    tx_jito_info(signature)          for one tx: was it Jito? how big was the tip?
    snipe_score(signatures)          aggregate analytics for a batch of txs

Endpoints (all public, no auth required):
    Block Engine: https://mainnet.block-engine.jito.wtf/api/v1
    Tip Floor:    https://bundles.jito.wtf/api/v1/bundles/tip_floor

`tx_jito_info` and `snipe_score` reach into Helius RPC to fetch the parsed
tx, so they require `HELIUS_API_KEY`. The Jito endpoints themselves are
keyless.

Docs: https://docs.jito.wtf
"""
from __future__ import annotations

import logging
import statistics
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.jito")

_BLOCK_ENGINE = "https://mainnet.block-engine.jito.wtf/api/v1"
_TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"

# The 8 hardcoded Jito tip accounts (frozenset for O(1) membership checks).
# Refreshable via `tip_accounts(refresh=True)`, but in practice these are
# stable across years — Jito would coordinate a rotation if it ever happened.
TIP_ACCOUNTS: frozenset[str] = frozenset({
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
})

_LAMPORTS_PER_SOL = 1_000_000_000

_cache = TTLCache()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ts_to_iso(secs) -> str | None:
    try:
        return datetime.fromtimestamp(int(secs), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


# ---------- Block Engine JSON-RPC ----------

async def _block_engine_rpc(method: str, params: list,
                              *, label: str = "") -> object | None:
    """POST a JSON-RPC call to the Jito Block Engine."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = await _http.post_json(f"{_BLOCK_ENGINE}/bundles", payload,
                                  label=label or f"jito {method}")
    if not isinstance(body, dict):
        return None
    if body.get("error"):
        log.warning("jito %s error: %s", method, body["error"])
        return None
    return body.get("result")


# ---------- public: tip-side ----------

async def tip_floor() -> dict | None:
    """Current Jito tip percentiles for landing a bundle this slot.

    Returns:
        {time, landed_tips_25th_percentile, landed_tips_50th_percentile,
         landed_tips_75th_percentile, landed_tips_95th_percentile,
         landed_tips_99th_percentile, ema_landed_tips_50th_percentile}
    All tip values in SOL (Jito returns floats already).
    """
    ck = ("tip_floor",)
    cached = _cache.get(ck, 10)  # tip floor moves fast
    if cached is not None:
        return cached
    body = await _http.get_json(_TIP_FLOOR_URL, label="jito tip_floor")
    if not body:
        return None
    # API returns a 1-element list.
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict):
        return None
    out = {
        "ts": body.get("time") or _now_iso(),
        "p25_sol": _f(body.get("landed_tips_25th_percentile")),
        "p50_sol": _f(body.get("landed_tips_50th_percentile")),
        "p75_sol": _f(body.get("landed_tips_75th_percentile")),
        "p95_sol": _f(body.get("landed_tips_95th_percentile")),
        "p99_sol": _f(body.get("landed_tips_99th_percentile")),
        "ema_p50_sol": _f(body.get("ema_landed_tips_50th_percentile")),
    }
    _cache.put(ck, out)
    return out


async def tip_accounts(*, refresh: bool = False) -> list[str]:
    """The 8 Jito tip accounts.

    Returns the hardcoded set by default (fast, no network). Pass
    `refresh=True` to fetch the canonical list from Jito's Block Engine —
    useful as a sanity check, but the result is the same in practice.
    """
    if not refresh:
        return sorted(TIP_ACCOUNTS)
    result = await _block_engine_rpc("getTipAccounts", [], label="jito tip_accounts")
    if isinstance(result, list):
        return [a for a in result if isinstance(a, str)]
    return sorted(TIP_ACCOUNTS)


# ---------- public: bundle-side ----------

def _normalize_bundle_ids(bundle_ids: str | list[str]) -> list[str]:
    ids = [bundle_ids] if isinstance(bundle_ids, str) else list(bundle_ids or [])
    return [b for b in (s.strip() for s in ids if s) if b][:5]


async def bundle_status(bundle_ids: str | list[str]) -> list[dict]:
    """Historical status for up to 5 bundle ids.

    Each row: {bundle_id, slot, confirmation_status, transactions, err}.
    Empty list if Jito has no record (uncommon for landed bundles).
    """
    ids = _normalize_bundle_ids(bundle_ids)
    if not ids:
        return []
    result = await _block_engine_rpc("getBundleStatuses", [ids],
                                       label="jito getBundleStatuses")
    if not isinstance(result, dict):
        return []
    rows: list[dict] = []
    for r in (result.get("value") or []):
        if not isinstance(r, dict):
            continue
        rows.append({
            "bundle_id": r.get("bundle_id"),
            "slot": r.get("slot"),
            "confirmation_status": r.get("confirmation_status"),
            "transactions": r.get("transactions") or [],
            "err": r.get("err"),
        })
    return rows


async def inflight_bundle_status(bundle_ids: str | list[str]) -> list[dict]:
    """Recent bundle status (5-min lookback). Each row: {bundle_id, status
    in (Invalid|Pending|Failed|Landed), landed_slot}."""
    ids = _normalize_bundle_ids(bundle_ids)
    if not ids:
        return []
    result = await _block_engine_rpc("getInflightBundleStatuses", [ids],
                                       label="jito getInflightBundleStatuses")
    if not isinstance(result, dict):
        return []
    rows: list[dict] = []
    for r in (result.get("value") or []):
        if not isinstance(r, dict):
            continue
        rows.append({
            "bundle_id": r.get("bundle_id"),
            "status": r.get("status"),
            "landed_slot": r.get("landed_slot"),
        })
    return rows


# ---------- public: tx-level sniper detection ----------

def _scan_native_transfers_for_tip(native_transfers: list,
                                     fee_payer: str | None) -> tuple[int, str | None]:
    """Sum lamports sent to any Jito tip account in `native_transfers`.

    Returns `(total_tip_lamports, primary_tip_account)`. A regular tx returns
    `(0, None)`. Bundles typically have exactly one tip transfer; we sum to
    handle the (rare) multi-tip case.
    """
    total = 0
    primary: str | None = None
    for n in native_transfers or []:
        if not isinstance(n, dict):
            continue
        to = n.get("toUserAccount")
        amt = n.get("amount")
        if to in TIP_ACCOUNTS and isinstance(amt, (int, float)) and amt > 0:
            total += int(amt)
            if primary is None:
                primary = to
    return total, primary


async def tx_jito_info(signature: str) -> dict | None:
    """For one Solana tx signature, determine whether it was submitted via
    Jito and how much it tipped.

    Returns:
        {signature, is_jito, tip_lamports, tip_sol, tip_account, slot,
         block_ts, source, fee_payer}

    Returns None if Helius doesn't have the tx (rare) or the input is empty.
    Detection is structural: scan the tx's parsed native transfers for any
    SOL transfer to one of the 8 Jito tip accounts. False-positive rate is
    effectively zero — only Jito searchers tip those addresses.
    """
    sig = (signature or "").strip()
    if not sig:
        return None
    ck = ("tx_jito_info", sig)
    cached = _cache.get(ck, 300)  # tx data is immutable once finalized
    if cached is not None:
        return cached

    # Use Helius enhanced TX endpoint — it gives us pre-parsed
    # `nativeTransfers` without us having to walk instruction trees.
    if not settings.HELIUS_API_KEY:
        log.warning("jito.tx_jito_info: HELIUS_API_KEY not set")
        return None
    # Key goes in `params` rather than hand-spliced into the URL string. Note
    # this keeps it out of tckr's own logs (we log the `label`, never the URL),
    # but httpx still renders the final URL — query string included — when its
    # own logger is at INFO. Consumers handling Helius keys should keep the
    # `httpx` logger at WARNING. (Helius has no header-auth alternative.)
    body = await _http.post_json(
        "https://api.helius.xyz/v0/transactions",
        {"transactions": [sig]},
        params={"api-key": settings.HELIUS_API_KEY},
        label=f"helius parse_tx {sig[:10]}",
    )
    if not isinstance(body, list) or not body or not isinstance(body[0], dict):
        return None
    tx = body[0]

    tip_lamports, tip_account = _scan_native_transfers_for_tip(
        tx.get("nativeTransfers") or [], tx.get("feePayer"),
    )
    out = {
        "signature": sig,
        "is_jito": tip_lamports > 0,
        "tip_lamports": tip_lamports,
        "tip_sol": tip_lamports / _LAMPORTS_PER_SOL if tip_lamports else 0.0,
        "tip_account": tip_account,
        "slot": tx.get("slot"),
        "block_ts": _ts_to_iso(tx.get("timestamp")) if tx.get("timestamp") else None,
        "source": tx.get("source"),     # PUMP_FUN / RAYDIUM / JUPITER / ...
        "fee_payer": tx.get("feePayer"),
    }
    _cache.put(ck, out)
    return out


async def snipe_score(signatures: list[str], *,
                       sol_price_usd: float | None = None) -> dict:
    """Aggregate Jito / snipe analytics for a batch of tx signatures.

    Killer use case: feed in the first N buy signatures for a freshly-launched
    token (from `pumpfun.live_trades(mint)` or similar) to quantify how
    heavily sniped the launch was. High `jito_pct` + high `avg_tip_sol` =
    competitive bot snipe; low both = organic flow.

    Returns:
        {n_total, n_jito, jito_pct, total_tips_sol, total_tips_usd?,
         max_tip_sol, avg_tip_sol_among_jito, median_tip_sol_among_jito,
         tip_account_distribution: {account: count}, ts}

    `sol_price_usd` optional — if provided, USD totals get populated.
    """
    sigs = [s.strip() for s in (signatures or []) if s and s.strip()]
    if not sigs:
        return {"n_total": 0, "n_jito": 0, "jito_pct": 0.0,
                "total_tips_sol": 0.0, "total_tips_usd": None,
                "max_tip_sol": 0.0, "avg_tip_sol_among_jito": 0.0,
                "median_tip_sol_among_jito": 0.0,
                "tip_account_distribution": {}, "ts": _now_iso()}

    import asyncio
    infos = await asyncio.gather(*[tx_jito_info(s) for s in sigs])
    infos = [i for i in infos if i is not None]

    jito_infos = [i for i in infos if i["is_jito"]]
    tip_amounts_sol = [i["tip_sol"] for i in jito_infos]
    total_sol = sum(tip_amounts_sol)
    account_dist: dict[str, int] = {}
    for i in jito_infos:
        if i["tip_account"]:
            account_dist[i["tip_account"]] = account_dist.get(i["tip_account"], 0) + 1

    out: dict = {
        "n_total": len(infos),
        "n_jito": len(jito_infos),
        "jito_pct": (len(jito_infos) / len(infos) * 100.0) if infos else 0.0,
        "total_tips_sol": total_sol,
        "total_tips_usd": (total_sol * sol_price_usd) if sol_price_usd else None,
        "max_tip_sol": max(tip_amounts_sol) if tip_amounts_sol else 0.0,
        "avg_tip_sol_among_jito": (sum(tip_amounts_sol) / len(tip_amounts_sol)
                                    if tip_amounts_sol else 0.0),
        "median_tip_sol_among_jito": (statistics.median(tip_amounts_sol)
                                       if tip_amounts_sol else 0.0),
        "tip_account_distribution": account_dist,
        "ts": _now_iso(),
    }
    return out
