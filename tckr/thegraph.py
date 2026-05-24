"""The Graph — generic GraphQL access to indexed subgraphs.

Two access modes:

1. **Hosted / public** — gateway URL with a free anonymous quota. Useful for
   ad-hoc queries; rate-limited.
2. **Authenticated decentralized network** — `https://gateway.thegraph.com/api/{key}/...`
   when `THEGRAPH_API_KEY` is set. Higher quota, signed.

We expose:

- `query_subgraph(subgraph_id, query, variables=None)` — generic POST. Returns
  the parsed `data` field (or None on error).
- `query_subgraph_name(owner, name, query, variables=None)` — legacy hosted
  service shape (`/subgraphs/name/<owner>/<name>`). Still works for many.
- A handful of named convenience queries for common subgraphs (Uniswap V3,
  Aave V3) so callers don't have to hand-write GraphQL for the popular cases.
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.thegraph")

_cache = TTLCache()


def _url_for_id(subgraph_id: str) -> str:
    """Pick gateway URL based on whether we have a key."""
    if settings.THEGRAPH_API_KEY:
        return f"https://gateway.thegraph.com/api/{settings.THEGRAPH_API_KEY}/subgraphs/id/{subgraph_id}"
    # Public gateway — heavily throttled but works keyless.
    return f"https://gateway-arbitrum.network.thegraph.com/api/subgraphs/id/{subgraph_id}"


def _url_for_hosted(owner: str, name: str) -> str:
    return f"https://api.thegraph.com/subgraphs/name/{owner}/{name}"


async def query_subgraph(subgraph_id: str, query: str,
                          variables: dict | None = None) -> dict | None:
    """Run a GraphQL query against a subgraph by id (decentralized network)."""
    url = _url_for_id(subgraph_id)
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    key = (subgraph_id, query, tuple(sorted((variables or {}).items())))
    ttl = settings.THEGRAPH_TTL_S
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.post_json(url, payload, label=f"thegraph {subgraph_id[:10]}")
        if not isinstance(data, dict):
            return None
        if "errors" in data:
            log.warning("thegraph errors: %s", data["errors"][:2])
            return None
        result = data.get("data")
        if result is not None:
            _cache.put(key, result)
        return result


async def query_subgraph_name(owner: str, name: str, query: str,
                               variables: dict | None = None) -> dict | None:
    """Legacy hosted-service shape: `/subgraphs/name/<owner>/<name>`."""
    url = _url_for_hosted(owner, name)
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    key = (owner, name, query, tuple(sorted((variables or {}).items())))
    ttl = settings.THEGRAPH_TTL_S
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.post_json(url, payload, label=f"thegraph {owner}/{name}")
        if not isinstance(data, dict):
            return None
        if "errors" in data:
            log.warning("thegraph errors: %s", data["errors"][:2])
            return None
        result = data.get("data")
        if result is not None:
            _cache.put(key, result)
        return result


# ============================================================================
# Convenience queries for popular subgraphs
# ============================================================================

# Uniswap V3 on Ethereum mainnet (canonical subgraph id).
UNISWAP_V3_ETH_SUBGRAPH_ID = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"


async def uniswap_v3_top_pools(*, first: int = 10,
                                subgraph_id: str = UNISWAP_V3_ETH_SUBGRAPH_ID) -> list[dict] | None:
    """Top Uniswap V3 pools by total value locked."""
    query = """
    query TopPools($first: Int!) {
      pools(first: $first, orderBy: totalValueLockedUSD, orderDirection: desc) {
        id
        feeTier
        liquidity
        sqrtPrice
        token0 { id symbol name }
        token1 { id symbol name }
        totalValueLockedUSD
        volumeUSD
      }
    }
    """
    data = await query_subgraph(subgraph_id, query,
                                 variables={"first": min(max(int(first), 1), 100)})
    if not isinstance(data, dict):
        return None
    return data.get("pools")
