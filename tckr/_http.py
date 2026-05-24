"""Shared async HTTP helper for tckr source modules.

Per-call httpx.AsyncClient, transient-error retry, and graceful failure:
callers get None instead of an exception, so a dead upstream looks the same
as an empty result.

Why per-call (not pooled): a module-global pooled client binds its transports
to whichever asyncio loop first touched it. Calling asyncio.run() a second
time in the same process (test suites, repeated CLI invocations, a host app
that tears down and restarts) would then hit "Event loop is closed". Per-call
clients sidestep that entirely — at the cost of a small connection-reuse
penalty that's dominated by network latency and absorbed by per-source TTL
caches above this layer.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from tckr import settings

log = logging.getLogger("tckr.http")

_RETRY_STATUS = {429, 500, 502, 503, 504}


async def _request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    json: Any = None,
    label: str = "",
) -> Any | None:
    tag = label or url
    last_exc: Exception | None = None
    async with httpx.AsyncClient(
        timeout=settings.HTTP_TIMEOUT_S,
        headers={"accept": "application/json"},
        follow_redirects=True,
    ) as client:
        for attempt in range(settings.HTTP_MAX_RETRIES + 1):
            try:
                r = await client.request(method, url, params=params,
                                         headers=headers, json=json)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in _RETRY_STATUS and attempt < settings.HTTP_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                log.warning("%s -> http %d: %s", tag, r.status_code, r.text[:200])
                return None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < settings.HTTP_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as e:  # noqa: BLE001 — never let a fetch crash a caller
                log.warning("%s -> unexpected %s: %s", tag, type(e).__name__, e)
                return None
    log.warning("%s -> giving up after retries: %s", tag, last_exc)
    return None


async def get_json(url: str, *, params: dict | None = None,
                   headers: dict | None = None, label: str = "") -> Any | None:
    """GET `url`; return parsed JSON, or None on any failure."""
    return await _request("GET", url, params=params, headers=headers, label=label)


async def post_json(url: str, payload: Any, *, headers: dict | None = None,
                    label: str = "") -> Any | None:
    """POST `payload` as JSON to `url`; return parsed JSON, or None on failure."""
    return await _request("POST", url, json=payload, headers=headers, label=label)


async def aclose() -> None:
    """No-op kept for backward compatibility with callers from the pooled era."""
    return None
