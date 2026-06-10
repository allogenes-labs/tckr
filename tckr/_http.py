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
from datetime import UTC, datetime
from typing import Any

import httpx

from tckr import settings

log = logging.getLogger("tckr.http")

_RETRY_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Per-provider health tracking.
#
# Every HTTP call has a `label` like "coingecko market_chart bitcoin" or
# "hyperliquid candleSnapshot BTC 1d". We bucket by the FIRST whitespace-
# separated token (the provider name) and keep a tiny rolling summary:
# success count, failure count, last status code, last error message, last
# timestamp, and a flag for whether the last call was rate-limited (429).
#
# `tckr.health()` re-exports this so consumers can show a degraded-mode
# banner ("CoinGecko rate-limited 30s ago — falling back to Hyperliquid")
# or drive smarter routing decisions.
# ---------------------------------------------------------------------------

_health: dict[str, dict] = {}


def _provider_of(label: str) -> str:
    """Bucket key for the health table. Labels like "coingecko market_chart …"
    map to "coingecko"; raw URLs (no label) get their second-level hostname
    so health() shows "dexscreener" instead of the whole URL string."""
    if not label:
        return "_unknown"
    first = label.split(" ", 1)[0]
    if first.startswith(("http://", "https://")):
        try:
            host = first.split("://", 1)[1].split("/", 1)[0]
            parts = host.split(".")
            if len(parts) >= 2:
                # api.dexscreener.com -> dexscreener; pro-api.coingecko.com -> coingecko
                return parts[-2]
            return host
        except IndexError:
            return first
    return first


def _record(label: str, *, status: int | None = None,
            ok: bool = False, error: str | None = None) -> None:
    p = _provider_of(label)
    row = _health.setdefault(p, {
        "ok_count": 0, "fail_count": 0,
        "last_status": None, "last_error": None,
        "last_ts": None, "last_429_ts": None,
    })
    row["last_status"] = status
    row["last_ts"] = datetime.now(UTC).isoformat()
    if ok:
        row["ok_count"] += 1
        row["last_error"] = None
    else:
        row["fail_count"] += 1
        row["last_error"] = (error or "")[:200] or (f"http {status}" if status else "unknown")
        if status == 429:
            row["last_429_ts"] = row["last_ts"]


def health() -> dict:
    """Snapshot of per-provider HTTP health: counts, last status, last error,
    and last-rate-limit timestamp. Keys are provider names extracted from the
    call labels ("coingecko", "hyperliquid", "geckoterminal", ...)."""
    return {k: dict(v) for k, v in _health.items()}


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
                    _record(tag, status=200, ok=True)
                    return r.json()
                if r.status_code in _RETRY_STATUS and attempt < settings.HTTP_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                log.warning("%s -> http %d: %s", tag, r.status_code, r.text[:200])
                _record(tag, status=r.status_code, error=r.text[:200])
                return None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < settings.HTTP_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as e:  # noqa: BLE001 — never let a fetch crash a caller
                log.warning("%s -> unexpected %s: %s", tag, type(e).__name__, e)
                _record(tag, error=f"{type(e).__name__}: {e}")
                return None
    log.warning("%s -> giving up after retries: %s", tag, last_exc)
    _record(tag, error=f"retry-exhausted: {last_exc}")
    return None


async def get_json(url: str, *, params: dict | None = None,
                   headers: dict | None = None, label: str = "") -> Any | None:
    """GET `url`; return parsed JSON, or None on any failure."""
    return await _request("GET", url, params=params, headers=headers, label=label)


async def post_json(url: str, payload: Any, *, params: dict | None = None,
                    headers: dict | None = None, label: str = "") -> Any | None:
    """POST `payload` as JSON to `url`; return parsed JSON, or None on failure."""
    return await _request("POST", url, json=payload, params=params,
                          headers=headers, label=label)


async def aclose() -> None:
    """No-op kept for backward compatibility with callers from the pooled era."""
    return None
