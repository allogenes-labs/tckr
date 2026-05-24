"""Token Terminal — protocol fundamentals (revenue, fees, P/E, TVL multiples).

Base: `https://api.tokenterminal.com/v2`. Auth via
`Authorization: Bearer <TOKENTERMINAL_API_KEY>`.

Tier caveat: free tier exposes the project catalog + a handful of metrics.
Detailed historical series and many metrics are paid. Module fails gracefully
on 401/402.

Endpoints wrapped:
- `projects(market_sector=None)` — list projects with current snapshot of key
  metrics (revenue_24h, fees_24h, market_cap, treasury, etc.).
- `project(project_id)` — single project detail.
- `project_metrics(project_id)` — list of available metric ids for a project.
- `metric_history(project_id, metric_id, since, until)` — historical series.
- `market_sectors()` — sector classifications.
"""
from __future__ import annotations

import logging

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.tokenterminal")

_BASE = "https://api.tokenterminal.com/v2"
_cache = TTLCache()


def _headers() -> dict | None:
    if not settings.TOKENTERMINAL_API_KEY:
        log.warning("TOKENTERMINAL_API_KEY not set — tokenterminal skipped")
        return None
    return {"Authorization": f"Bearer {settings.TOKENTERMINAL_API_KEY}"}


async def _get(path: str, params: dict | None = None,
               ttl_s: float | None = None, label: str | None = None):
    headers = _headers()
    if headers is None:
        return None
    ttl = ttl_s if ttl_s is not None else settings.TOKENTERMINAL_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(f"{_BASE}{path}", params=params, headers=headers,
                                    label=label or f"tokenterminal {path}")
        if not isinstance(data, dict):
            return None
        result = data.get("data", data)
        _cache.put(key, result)
        return result


# ============================================================================
# Public API
# ============================================================================

async def projects(market_sector: str | None = None) -> list[dict] | None:
    """List all projects with current metric snapshot.

    `market_sector` filters by classification (e.g. 'Blockchains (L1)',
    'DeFi', 'NFT marketplaces').
    """
    params = {"market_sector": market_sector} if market_sector else None
    r = await _get("/projects", params=params, label="tokenterminal projects")
    return r if isinstance(r, list) else None


async def project(project_id: str) -> dict | None:
    """Single project detail."""
    return await _get(f"/projects/{project_id}",
                      label=f"tokenterminal project {project_id}")


async def project_metrics(project_id: str) -> list[dict] | None:
    """List of metric ids available for a project."""
    r = await _get(f"/projects/{project_id}/metrics",
                   label=f"tokenterminal metrics {project_id}")
    return r if isinstance(r, list) else None


async def metric_history(project_id: str, metric_id: str, *,
                          since: str | None = None,
                          until: str | None = None) -> list[dict] | None:
    """Historical series for one metric on one project.

    `since` / `until` are ISO dates (YYYY-MM-DD). Without them returns the
    plan-default lookback (typically 90 days on free tier).
    """
    params: dict = {}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    r = await _get(f"/projects/{project_id}/metrics/{metric_id}",
                   params=params or None,
                   ttl_s=settings.TOKENTERMINAL_HISTORY_TTL_S,
                   label=f"tokenterminal {project_id}/{metric_id}")
    return r if isinstance(r, list) else None


async def market_sectors() -> list[dict] | None:
    """List of market sectors with the projects classified under each."""
    r = await _get("/market_sectors", label="tokenterminal market_sectors")
    return r if isinstance(r, list) else None
