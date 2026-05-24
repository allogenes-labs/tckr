"""Dexscreener API — DEX pairs, search, and new-token discovery.

Free, no API key. Covers Base, Solana, and ~every chain Dexscreener indexes.
Complements geckoterminal: a second, independent price/liquidity source for
cross-checking, plus a new-launch radar via token profiles.

Pair shape is flattened and uniform. Chain ids are Dexscreener's own
(`base`, `solana`, `ethereum`, ...); settings.normalize_network output is
translated where it differs.

Docs: https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.dexscreener")

_BASE = "https://api.dexscreener.com"
_cache = TTLCache()

# settings.normalize_network yields GeckoTerminal-style ids; Dexscreener differs
# only for Ethereum mainnet (Base and Solana ids are identical on both).
_DS_CHAIN_OVERRIDES = {"eth": "ethereum"}


def _ds_chain(network: str | None) -> str | None:
    if not network:
        return None
    canon = settings.normalize_network(network)
    return _DS_CHAIN_OVERRIDES.get(canon, canon)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _token(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {"address": raw.get("address"), "symbol": raw.get("symbol"),
            "name": raw.get("name")}


def _parse_pair(raw: dict) -> dict:
    return {
        "chain": raw.get("chainId"),
        "dex": raw.get("dexId"),
        "pair_address": raw.get("pairAddress"),
        "url": raw.get("url"),
        "labels": raw.get("labels") or [],
        "base_token": _token(raw.get("baseToken")),
        "quote_token": _token(raw.get("quoteToken")),
        "price_usd": _f(raw.get("priceUsd")),
        "price_native": _f(raw.get("priceNative")),
        "fdv_usd": _f(raw.get("fdv")),
        "market_cap_usd": _f(raw.get("marketCap")),
        "liquidity_usd": _f((raw.get("liquidity") or {}).get("usd")),
        "volume": raw.get("volume") or {},
        "price_change_pct": raw.get("priceChange") or {},
        "txns": raw.get("txns") or {},
        "created_at": _ms_to_iso(raw.get("pairCreatedAt")),
        "ts": _now_iso(),
    }


def _filter_chain(pairs: list[dict], ds_chain: str | None) -> list[dict]:
    if not ds_chain:
        return pairs
    return [p for p in pairs if p.get("chain") == ds_chain]


async def token_pairs(address, *, chain: str | None = None) -> list[dict]:
    """All DEX pairs for one or more token contract addresses.

    `address` is a single address or an iterable (max 30). When `chain` is
    given, results are filtered to that chain.
    """
    addrs = [address] if isinstance(address, str) else list(address)
    addrs = [a.strip() for a in addrs if a and a.strip()]
    if not addrs:
        return []
    joined = ",".join(addrs)
    ck = ("token_pairs", joined.lower())
    pairs = _cache.get(ck, settings.DEX_TTL_S)
    if pairs is None:
        body = await _http.get_json(
            f"{_BASE}/latest/dex/tokens/{joined}",
            label=f"dexscreener tokens {joined[:40]}",
        )
        pairs = [_parse_pair(p) for p in (body or {}).get("pairs") or []]
        _cache.put(ck, pairs)
    return _filter_chain(pairs, _ds_chain(chain))


async def search(query: str, *, chain: str | None = None) -> list[dict]:
    """Search DEX pairs by free-text query (symbol, name, or address)."""
    query = (query or "").strip()
    if not query:
        return []
    ck = ("search", query.lower())
    pairs = _cache.get(ck, settings.DEX_TTL_S)
    if pairs is None:
        body = await _http.get_json(
            f"{_BASE}/latest/dex/search",
            params={"q": query},
            label=f"dexscreener search {query!r}",
        )
        pairs = [_parse_pair(p) for p in (body or {}).get("pairs") or []]
        _cache.put(ck, pairs)
    return _filter_chain(pairs, _ds_chain(chain))


async def pair(chain: str, pair_address: str) -> dict | None:
    """A single DEX pair by chain + pair address."""
    ds_chain = _ds_chain(chain)
    pair_address = (pair_address or "").strip()
    if not ds_chain or not pair_address:
        return None
    ck = ("pair", ds_chain, pair_address.lower())
    cached = _cache.get(ck, settings.DEX_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(
        f"{_BASE}/latest/dex/pairs/{ds_chain}/{pair_address}",
        label=f"dexscreener pair {ds_chain}/{pair_address}",
    )
    raw_pairs = (body or {}).get("pairs") or []
    if not raw_pairs and (body or {}).get("pair"):
        raw_pairs = [body["pair"]]
    if not raw_pairs:
        return None
    parsed = _parse_pair(raw_pairs[0])
    _cache.put(ck, parsed)
    return parsed


async def latest_boosted_tokens(*, chain: str | None = None) -> list[dict]:
    """Most-recently boosted tokens (paid promotion signal).

    Boosts are a marketing signal — useful as both bullish (someone's actively
    promoting the token, narrative is forming) and bearish (heavy boost on a
    <24h-old token often correlates with exit-liquidity-seeking launches).

    Returns lightweight {chain, token_address, amount, total_amount, icon,
    url, description, links} rows. Filter to one chain with `chain`.
    """
    ck = ("boosts_latest",)
    rows = _cache.get(ck, settings.DEX_TTL_S)
    if rows is None:
        body = await _http.get_json(
            f"{_BASE}/token-boosts/latest/v1",
            label="dexscreener token-boosts latest",
        )
        rows = [
            {
                "chain": r.get("chainId"),
                "token_address": r.get("tokenAddress"),
                "amount": _f(r.get("amount")),
                "total_amount": _f(r.get("totalAmount")),
                "url": r.get("url"),
                "description": r.get("description"),
                "icon": r.get("icon"),
                "links": r.get("links") or [],
            }
            for r in (body if isinstance(body, list) else [])
            if isinstance(r, dict)
        ]
        _cache.put(ck, rows)
    ds_chain = _ds_chain(chain)
    if not ds_chain:
        return rows
    return [r for r in rows if r.get("chain") == ds_chain]


async def top_boosted_tokens(*, chain: str | None = None) -> list[dict]:
    """Most-boosted tokens overall (cumulative paid promotion ranking). Same
    shape as `latest_boosted_tokens`, sorted by `total_amount` descending."""
    ck = ("boosts_top",)
    rows = _cache.get(ck, settings.DEX_TTL_S)
    if rows is None:
        body = await _http.get_json(
            f"{_BASE}/token-boosts/top/v1",
            label="dexscreener token-boosts top",
        )
        rows = [
            {
                "chain": r.get("chainId"),
                "token_address": r.get("tokenAddress"),
                "amount": _f(r.get("amount")),
                "total_amount": _f(r.get("totalAmount")),
                "url": r.get("url"),
                "description": r.get("description"),
                "icon": r.get("icon"),
                "links": r.get("links") or [],
            }
            for r in (body if isinstance(body, list) else [])
            if isinstance(r, dict)
        ]
        rows.sort(key=lambda r: r.get("total_amount") or 0, reverse=True)
        _cache.put(ck, rows)
    ds_chain = _ds_chain(chain)
    if not ds_chain:
        return rows
    return [r for r in rows if r.get("chain") == ds_chain]


async def latest_token_profiles(*, chain: str | None = None) -> list[dict]:
    """Most recently listed token profiles — a new-launch radar.

    Returns lightweight {chain, token_address, url, description, icon, links}
    dicts. Filter to one chain with `chain`.
    """
    ck = ("profiles",)
    rows = _cache.get(ck, settings.DEX_TTL_S)
    if rows is None:
        body = await _http.get_json(
            f"{_BASE}/token-profiles/latest/v1",
            label="dexscreener token-profiles",
        )
        rows = [
            {
                "chain": r.get("chainId"),
                "token_address": r.get("tokenAddress"),
                "url": r.get("url"),
                "description": r.get("description"),
                "icon": r.get("icon"),
                "links": r.get("links") or [],
            }
            for r in (body if isinstance(body, list) else [])
            if isinstance(r, dict)
        ]
        _cache.put(ck, rows)
    ds_chain = _ds_chain(chain)
    if not ds_chain:
        return rows
    return [r for r in rows if r.get("chain") == ds_chain]
