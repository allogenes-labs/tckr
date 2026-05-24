"""Alchemy API — on-chain EVM wallet data (Base, Ethereum, …).

Free API key (set ALCHEMY_API_KEY). One endpoint per network, picked via
`network` kwarg (default "base"). Currently supports Base + Ethereum mainnet;
extend `_ALCHEMY_NETWORKS` to add others.

Public functions cover the wallet / whale-tracking subset:

    native_balance   ETH balance, in ETH (not wei)
    token_balances   ERC-20 holdings with symbol/name/decimals, sorted by raw
                     balance desc; metadata fetched per token and cached 24h
    transfers        recent asset transfers (external + erc20 by default),
                     inbound + outbound, deduped and sorted by block desc

All calls degrade gracefully — missing key, network errors, or RPC errors
return [] / None rather than raising.

Docs: https://docs.alchemy.com/reference/json-rpc-api
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.alchemy")

# canonical id -> Alchemy network slug.
_ALCHEMY_NETWORKS: dict[str, str] = {
    "base": "base-mainnet",
    "eth":  "eth-mainnet",
}

_DEFAULT_NETWORK = "base"

_cache = TTLCache()
_metadata_cache = TTLCache()


def _endpoint(network: str | None = None) -> str | None:
    canon = settings.normalize_network(network or _DEFAULT_NETWORK)
    slug = _ALCHEMY_NETWORKS.get(canon)
    if not slug:
        log.warning("alchemy: no Alchemy network slug for %r", network)
        return None
    if not settings.ALCHEMY_API_KEY:
        log.warning("alchemy: ALCHEMY_API_KEY not set")
        return None
    return f"https://{slug}.g.alchemy.com/v2/{settings.ALCHEMY_API_KEY}"


def _hex_to_int(h) -> int | None:
    if not isinstance(h, str):
        return None
    try:
        return int(h, 16) if h.startswith("0x") else int(h)
    except ValueError:
        return None


def _hex_to_float(h, decimals) -> float | None:
    raw = _hex_to_int(h)
    if raw is None or decimals is None:
        return None
    try:
        return raw / (10 ** int(decimals))
    except (TypeError, ValueError):
        return None


async def _rpc(method: str, params: list, *, network: str | None = None,
                label: str = ""):
    url = _endpoint(network)
    if not url:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = await _http.post_json(url, payload, label=label or f"alchemy {method}")
    if not isinstance(body, dict):
        return None
    if body.get("error"):
        log.warning("alchemy %s error: %s", method, body["error"])
        return None
    return body.get("result")


async def native_balance(address: str, *,
                          network: str = _DEFAULT_NETWORK) -> float | None:
    """Native token (ETH) balance for `address`, in ETH (human units)."""
    if not address:
        return None
    canon = settings.normalize_network(network)
    ck = ("native_balance", canon, address.lower())
    cached = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached
    result = await _rpc("eth_getBalance", [address, "latest"], network=network,
                        label=f"alchemy eth_getBalance {address[:8]}…")
    if not isinstance(result, str):
        return None
    eth = _hex_to_float(result, 18)
    if eth is not None:
        _cache.put(ck, eth)
    return eth


async def token_metadata(contract: str, *,
                          network: str = _DEFAULT_NETWORK) -> dict | None:
    """Symbol / name / decimals / logo for an ERC-20 contract address."""
    if not contract:
        return None
    canon = settings.normalize_network(network)
    ck = ("metadata", canon, contract.lower())
    cached = _metadata_cache.get(ck, settings.TOKEN_METADATA_TTL_S)
    if cached is not None:
        return cached
    result = await _rpc("alchemy_getTokenMetadata", [contract], network=network,
                        label=f"alchemy_getTokenMetadata {contract[:8]}…")
    if not isinstance(result, dict):
        return None
    meta = {
        "address": contract,
        "name": result.get("name"),
        "symbol": result.get("symbol"),
        "decimals": result.get("decimals"),
        "logo": result.get("logo"),
    }
    _metadata_cache.put(ck, meta)
    return meta


async def token_balances(
    address: str,
    *,
    network: str = _DEFAULT_NETWORK,
    hide_zero: bool = True,
    max_tokens: int = 50,
    with_metadata: bool = True,
) -> list[dict]:
    """ERC-20 holdings for `address`.

    Each result: {contract, balance_raw, balance, symbol, name, decimals}.
    Sorted by raw balance descending (rough proxy when no price info).
    `max_tokens` caps how many tokens get metadata-enriched.
    """
    if not address:
        return []
    canon = settings.normalize_network(network)
    ck = ("token_balances", canon, address.lower(), hide_zero)
    raw_rows = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if raw_rows is None:
        result = await _rpc("alchemy_getTokenBalances", [address, "erc20"],
                            network=network,
                            label=f"alchemy_getTokenBalances {address[:8]}…")
        if not isinstance(result, dict):
            return []
        raw_rows = []
        for r in result.get("tokenBalances") or []:
            raw = _hex_to_int(r.get("tokenBalance"))
            if raw is None or (hide_zero and raw == 0):
                continue
            raw_rows.append({"contract": r.get("contractAddress"),
                              "balance_raw": raw})
        raw_rows.sort(key=lambda r: r["balance_raw"], reverse=True)
        _cache.put(ck, raw_rows)

    rows = raw_rows[:max_tokens] if max_tokens else list(raw_rows)
    if not with_metadata:
        return rows

    async def _enrich(row: dict) -> dict:
        meta = await token_metadata(row["contract"], network=network)
        if not meta:
            return row
        decimals = meta.get("decimals")
        row.update({
            "symbol": meta.get("symbol"),
            "name": meta.get("name"),
            "decimals": decimals,
        })
        if decimals is not None:
            try:
                row["balance"] = row["balance_raw"] / (10 ** int(decimals))
            except (TypeError, ValueError):
                pass
        return row

    return list(await asyncio.gather(*(_enrich(r) for r in rows)))


async def transfers(
    address: str,
    *,
    network: str = _DEFAULT_NETWORK,
    direction: str = "both",                  # "in" | "out" | "both"
    categories: list[str] | None = None,       # external, internal, erc20, erc721, erc1155, specialnft
    limit: int = 25,
) -> list[dict]:
    """Recent asset transfers for `address`. Wraps `alchemy_getAssetTransfers`.

    Default fetches both inbound and outbound external + erc20 transfers,
    deduped by uniqueId and sorted newest-block first.
    """
    if not address:
        return []
    cats = categories or ["external", "erc20"]
    direction = (direction or "both").lower()
    canon = settings.normalize_network(network)
    ck = ("transfers", canon, address.lower(), direction, tuple(cats), limit)
    cached = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached

    async def _one(filter_param: str) -> list[dict]:
        params = [{
            filter_param: address,
            "category": cats,
            "maxCount": hex(limit),
            "order": "desc",
            "withMetadata": True,
        }]
        result = await _rpc("alchemy_getAssetTransfers", params, network=network,
                            label=f"alchemy_getAssetTransfers {filter_param[:4]} {address[:8]}…")
        if not isinstance(result, dict):
            return []
        return result.get("transfers") or []

    raw: list[dict] = []
    if direction in ("out", "both"):
        raw.extend(await _one("fromAddress"))
    if direction in ("in", "both"):
        raw.extend(await _one("toAddress"))

    seen: set[str] = set()
    parsed: list[dict] = []
    for t in raw:
        uid = t.get("uniqueId")
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        meta = t.get("metadata") or {}
        parsed.append({
            "hash": t.get("hash"),
            "block_number": _hex_to_int(t.get("blockNum")),
            "block_ts": meta.get("blockTimestamp"),
            "from": t.get("from"),
            "to": t.get("to"),
            "value": t.get("value"),
            "asset": t.get("asset"),
            "category": t.get("category"),
            "contract": ((t.get("rawContract") or {}).get("address")),
        })
    parsed.sort(key=lambda r: r.get("block_number") or 0, reverse=True)
    out = parsed[:limit]
    _cache.put(ck, out)
    return out
