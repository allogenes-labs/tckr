"""GoPlus Security API — contract security scans per chain.

Free, no API key required for token-security and address-security endpoints
(rate limited; respect 30 req/min as a soft ceiling). For trading any token
under ~72 hours old this is the single most important data source — it
returns a structured risk report including:

- `is_honeypot`        : sells are blocked by the contract
- `hidden_owner`       : a backdoor admin you can't see in the explorer
- `can_take_back_ownership` : ownership has been "renounced" but actually isn't
- `selfdestruct`       : the contract can be destroyed
- `is_proxy`           : the contract is upgradable (rug surface)
- `is_blacklisted`     : wallets can be blacklisted from selling
- `is_mintable`        : supply can be inflated
- `external_call`      : the contract calls untrusted external addresses
- `holder_count`       : how many wallets hold it (1 = suspect)
- `lp_holder_count` + `lp_total_supply` : liquidity distribution
- top10 holder concentration, dev wallet info

Chains supported (chain_id):
  1 Ethereum  56 BSC  137 Polygon  43114 Avalanche  250 Fantom  42161 Arbitrum
  10 Optimism  8453 Base  324 zkSync  59144 Linea
  Solana uses a separate endpoint (handled transparently by `token_security`).

Docs: https://docs.gopluslabs.io/reference/api-overview
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.goplus")

_BASE = "https://api.gopluslabs.io/api/v1"
_cache = TTLCache()

# Chain name → Goplus chain_id. Names are lowercased on lookup.
_CHAIN_IDS: dict[str, str] = {
    "ethereum": "1", "eth": "1", "mainnet": "1",
    "bsc": "56", "bnb": "56", "binance": "56",
    "polygon": "137", "matic": "137",
    "avalanche": "43114", "avax": "43114",
    "fantom": "250", "ftm": "250",
    "arbitrum": "42161", "arb": "42161",
    "optimism": "10", "op": "10",
    "base": "8453",
    "zksync": "324",
    "linea": "59144",
    "scroll": "534352",
    "manta": "169",
}

# Boolean-ish Goplus fields encoded as "1"/"0"/empty strings. Convert them
# uniformly so consumers see Python booleans (or None when unknown).
_BOOL_FIELDS = {
    "is_honeypot", "is_mintable", "is_proxy", "is_blacklisted", "is_whitelisted",
    "is_anti_whale", "anti_whale_modifiable", "trading_cooldown",
    "transfer_pausable", "can_take_back_ownership", "owner_change_balance",
    "hidden_owner", "selfdestruct", "external_call", "gas_abuse",
    "cannot_buy", "cannot_sell_all", "slippage_modifiable", "personal_slippage_modifiable",
    "is_open_source", "is_in_dex", "is_true_token", "is_airdrop_scam",
    "trust_list",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _b(v) -> bool | None:
    """Goplus encodes booleans as '1' / '0' / '' (unknown)."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if s == "1": return True
    if s == "0": return False
    return None


def _chain_id(chain: str) -> str | None:
    key = (chain or "").strip().lower()
    if key in _CHAIN_IDS:
        return _CHAIN_IDS[key]
    # Allow raw chain id passthrough.
    if key.isdigit():
        return key
    return None


def _normalize_security_row(raw: dict) -> dict:
    """Flatten + type-coerce a Goplus token_security response for one address.

    Goplus returns ~50 fields per token; we surface the trade-relevant ones
    and keep the raw dict accessible under `raw` for callers that need more.
    """
    out: dict = {"raw": raw}
    # Coerce all known boolean-ish fields.
    for field in _BOOL_FIELDS:
        if field in raw:
            out[field] = _b(raw.get(field))
    # Numeric / string fields with light coercion.
    out["token_name"] = raw.get("token_name")
    out["token_symbol"] = raw.get("token_symbol")
    out["total_supply"] = _f(raw.get("total_supply"))
    out["holder_count"] = int(raw.get("holder_count")) if str(raw.get("holder_count", "")).isdigit() else None
    out["lp_holder_count"] = int(raw.get("lp_holder_count")) if str(raw.get("lp_holder_count", "")).isdigit() else None
    out["lp_total_supply"] = _f(raw.get("lp_total_supply"))
    out["buy_tax"] = _f(raw.get("buy_tax"))
    out["sell_tax"] = _f(raw.get("sell_tax"))
    out["creator_address"] = raw.get("creator_address")
    out["creator_balance"] = _f(raw.get("creator_balance"))
    out["creator_percent"] = _f(raw.get("creator_percent"))
    out["owner_address"] = raw.get("owner_address")
    out["owner_balance"] = _f(raw.get("owner_balance"))
    out["owner_percent"] = _f(raw.get("owner_percent"))

    # Top10 holder concentration (sum of top10 percentages). GoPlus reports
    # `percent` as a fraction of 1 (0.18 = 18%); we expose 0-100 to match the
    # `_pct` suffix. None (not 0.0) when no percent fields were present.
    holders = raw.get("holders") or []
    if isinstance(holders, list):
        pct_sum = None
        for h in holders[:10]:
            if isinstance(h, dict):
                p = _f(h.get("percent"))
                if p is not None:
                    pct_sum = (pct_sum or 0.0) + p * 100.0
        out["top10_holder_pct"] = pct_sum
        out["top_holders"] = [
            {
                "address": h.get("address"),
                "tag": h.get("tag"),
                "is_contract": _b(h.get("is_contract")),
                "is_locked": _b(h.get("is_locked")),
                "balance": _f(h.get("balance")),
                "percent": _f(h.get("percent")),
            }
            for h in holders[:10] if isinstance(h, dict)
        ]

    # LP holder concentration (where the LP tokens live — locked / burnt is good).
    lp_holders = raw.get("lp_holders") or []
    if isinstance(lp_holders, list):
        out["lp_top_holders"] = [
            {
                "address": h.get("address"),
                "tag": h.get("tag"),
                "is_locked": _b(h.get("is_locked")),
                "percent": _f(h.get("percent")),
            }
            for h in lp_holders[:5] if isinstance(h, dict)
        ]
        # Convenience: % of LP that's locked or burnt (0-100; GoPlus fractions
        # are scaled up). A list with no percent data at all stays None —
        # 0.0 would read as "confirmed nothing locked".
        locked_pct = None
        saw_pct = any(isinstance(h, dict) and _f(h.get("percent")) is not None
                      for h in lp_holders)
        if saw_pct:
            locked_pct = 0.0
            for h in lp_holders:
                if isinstance(h, dict) and _b(h.get("is_locked")):
                    p = _f(h.get("percent"))
                    if p is not None:
                        locked_pct += p * 100.0
        out["lp_locked_pct"] = locked_pct

    return out


def _risk_summary(row: dict) -> dict:
    """Produce a short, opinionated risk summary suitable for prompting.

    Returns {risk_level, hard_blockers, soft_warnings} where risk_level is
    one of {"critical", "high", "medium", "low", "unknown"}.
    """
    hard: list[str] = []
    soft: list[str] = []

    # Safety-critical fields. If GoPlus returns a partial record that OMITS these
    # (vs. returning them False), we cannot certify the token safe — a missing
    # field must never silently read as "not risky".
    raw = row.get("raw") or {}
    _CRITICAL = ("is_honeypot", "cannot_sell_all", "hidden_owner",
                 "can_take_back_ownership", "selfdestruct")
    missing_critical = [c for c in _CRITICAL if c not in raw]

    if row.get("is_honeypot") is True:
        hard.append("HONEYPOT: contract blocks sells")
    if row.get("hidden_owner") is True:
        hard.append("HIDDEN_OWNER: undisclosed admin backdoor")
    if row.get("can_take_back_ownership") is True:
        hard.append("FAKE_RENOUNCE: ownership can be reclaimed")
    if row.get("selfdestruct") is True:
        hard.append("SELFDESTRUCT: contract can be destroyed")
    if row.get("cannot_sell_all") is True:
        hard.append("CANNOT_SELL_ALL: partial-exit-only restriction")

    if row.get("is_mintable") is True:
        soft.append("mintable: supply can be inflated")
    if row.get("is_proxy") is True:
        soft.append("upgradable proxy: logic can be swapped")
    if row.get("transfer_pausable") is True:
        soft.append("transfers can be paused")
    if row.get("is_blacklisted") is True:
        soft.append("wallets can be blacklisted")
    if row.get("slippage_modifiable") is True:
        soft.append("slippage settings can be changed by owner")
    if row.get("trading_cooldown") is True:
        soft.append("trading cooldown enforceable")
    if row.get("external_call") is True:
        soft.append("calls untrusted external contracts")

    sell_tax = row.get("sell_tax")
    if sell_tax is not None and sell_tax > 0.10:
        hard.append(f"sell_tax > 10% ({sell_tax * 100:.1f}%)")
    elif sell_tax is not None and sell_tax > 0.05:
        soft.append(f"sell_tax {sell_tax * 100:.1f}% (high but tradeable)")

    top10 = row.get("top10_holder_pct")
    if top10 is not None and top10 > 70:
        hard.append(f"top-10 holder concentration {top10:.0f}% (extreme)")
    elif top10 is not None and top10 > 50:
        soft.append(f"top-10 holder concentration {top10:.0f}% (high)")

    creator_pct = row.get("creator_percent")
    if creator_pct is not None and creator_pct > 0.20:
        soft.append(f"creator still holds {creator_pct * 100:.1f}%")

    lp_locked = row.get("lp_locked_pct")
    if lp_locked is not None and lp_locked < 50:
        soft.append(f"only {lp_locked:.0f}% of LP is locked (rug surface)")

    if row.get("is_open_source") is False:
        soft.append("contract source is NOT verified")

    # Risk synthesis.
    if hard:
        level = "critical"
    elif len(soft) >= 4:
        level = "high"
    elif len(soft) >= 2:
        level = "medium"
    elif soft:
        level = "low"
    else:
        level = "unknown" if not row.get("is_open_source") else "low"

    # A partial response that omits the hard-blocker fields can't be a clean bill
    # of health — surface the gap and never let it settle at "low".
    if missing_critical and not hard:
        soft.append(
            f"incomplete GoPlus data: missing {', '.join(missing_critical)} "
            f"— cannot confirm safe"
        )
        if level == "low":
            level = "unknown"

    return {"risk_level": level, "hard_blockers": hard, "soft_warnings": soft}


async def token_security(chain: str, address: str) -> dict | None:
    """Security report for one token. Returns the flattened row PLUS a
    `risk_summary` dict with a single risk_level label and human-readable
    blocker/warning lists.

    Returns None if the chain is unsupported or upstream fails. Returns a
    row with `risk_level: "unknown"` if upstream returned an empty record
    (uninspected contract).
    """
    cid = _chain_id(chain)
    addr = (address or "").strip()
    if not cid or not addr:
        return None
    ck = ("token_security", cid, addr.lower())
    cached = _cache.get(ck, settings.SECURITY_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(
        f"{_BASE}/token_security/{cid}",
        params={"contract_addresses": addr},
        label=f"goplus token_security {cid}/{addr[:10]}",
    )
    if not isinstance(body, dict):
        return None
    result = (body.get("result") or {})
    # Goplus keys results by lower-cased address (except Solana, which keys by addr).
    raw = result.get(addr.lower()) or result.get(addr) or {}
    if not raw:
        out = {
            "chain": chain, "address": addr,
            "risk_summary": {"risk_level": "unknown",
                              "hard_blockers": [],
                              "soft_warnings": ["no security data — contract uninspected by Goplus"]},
            "ts": _now_iso(),
        }
        _cache.put(ck, out)
        return out
    flat = _normalize_security_row(raw)
    flat["chain"] = chain
    flat["address"] = addr
    flat["risk_summary"] = _risk_summary(flat)
    flat["ts"] = _now_iso()
    _cache.put(ck, flat)
    return flat


async def token_security_many(chain: str, addresses: list[str]) -> dict[str, dict]:
    """Batch security report. Returns {address: report}. Goplus supports up to
    100 addresses per request; we batch by 50 to stay well under any soft cap.
    """
    cid = _chain_id(chain)
    addrs = [a.strip() for a in addresses if a and a.strip()]
    if not cid or not addrs:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(addrs), 50):
        batch = addrs[i:i + 50]
        body = await _http.get_json(
            f"{_BASE}/token_security/{cid}",
            params={"contract_addresses": ",".join(batch)},
            label=f"goplus token_security batch {cid} (n={len(batch)})",
        )
        if not isinstance(body, dict):
            continue
        result = body.get("result") or {}
        for addr in batch:
            raw = result.get(addr.lower()) or result.get(addr) or {}
            if not raw:
                out[addr] = {
                    "chain": chain, "address": addr,
                    "risk_summary": {"risk_level": "unknown",
                                      "hard_blockers": [],
                                      "soft_warnings": ["no security data"]},
                    "ts": _now_iso(),
                }
                continue
            flat = _normalize_security_row(raw)
            flat["chain"] = chain
            flat["address"] = addr
            flat["risk_summary"] = _risk_summary(flat)
            flat["ts"] = _now_iso()
            out[addr] = flat
    return out


async def address_security(address: str) -> dict | None:
    """Wallet-level scam/phishing flags for one EVM address. Useful for
    inspecting a token's creator or owner address."""
    addr = (address or "").strip()
    if not addr or not _http.safe_path_segment(addr):
        return None
    ck = ("address_security", addr.lower())
    cached = _cache.get(ck, settings.SECURITY_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(
        f"{_BASE}/address_security/{addr}",
        label=f"goplus address_security {addr[:10]}",
    )
    if not isinstance(body, dict):
        return None
    result = body.get("result") or {}
    out = {
        "address": addr,
        "honeypot_related": _b(result.get("honeypot_related_address")),
        "phishing_activities": _b(result.get("phishing_activities")),
        "blacklist_doubt": _b(result.get("blacklist_doubt")),
        "blackmail_activities": _b(result.get("blackmail_activities")),
        "cybercrime": _b(result.get("cybercrime")),
        "money_laundering": _b(result.get("money_laundering")),
        "stealing_attack": _b(result.get("stealing_attack")),
        "fake_kyc": _b(result.get("fake_kyc")),
        "malicious_mining_activities": _b(result.get("malicious_mining_activities")),
        "darkweb_transactions": _b(result.get("darkweb_transactions")),
        "sanctioned": _b(result.get("sanctioned")),
        "ts": _now_iso(),
    }
    out["any_flag"] = any(v is True for k, v in out.items() if isinstance(v, bool))
    _cache.put(ck, out)
    return out
