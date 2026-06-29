"""Async TTL cache for tckr source modules.

A cache entry is {value, fetched_at}. Stale entries remain available via
get_stale() so callers can fall back to last-known data (flagged stale) when a
fresh fetch fails. Keys are arbitrary hashable tuples chosen by each module.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Hard cap on distinct cache keys per TTLCache instance. Modules keyed per
# wallet / token / contract would otherwise accumulate an entry (and a lock)
# per distinct key for the life of a long-running host process. Override per
# instance via TTLCache(max_entries=...).
_DEFAULT_MAX_ENTRIES = 4096


@dataclass
class CacheEntry:
    value: Any
    fetched_at: float

    def age(self) -> float:
        # Monotonic clock: immune to wall-clock steps (NTP, VM resume) that would
        # otherwise make entries look permanently fresh or expire them early.
        return time.monotonic() - self.fetched_at

    def fresh(self, ttl_s: float) -> bool:
        return self.age() < ttl_s


class TTLCache:
    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._d: dict[tuple, CacheEntry] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._max = max(1, int(max_entries))

    def lock(self, key: tuple) -> asyncio.Lock:
        # Lazy lock to avoid event-loop-bound init issues.
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def get(self, key: tuple, ttl_s: float) -> Any | None:
        e = self._d.get(key)
        if e is None or not e.fresh(ttl_s):
            return None
        return e.value

    def get_stale(self, key: tuple) -> tuple[Any, float] | None:
        e = self._d.get(key)
        return (e.value, e.age()) if e is not None else None

    def put(self, key: tuple, value: Any) -> None:
        # Re-insert at the end so writes refresh recency (dicts are insertion
        # ordered); evict oldest-written keys once over the cap.
        if key in self._d:
            del self._d[key]
        self._d[key] = CacheEntry(value=value, fetched_at=time.monotonic())
        while len(self._d) > self._max:
            oldest = next(iter(self._d))
            self._d.pop(oldest, None)
            self._locks.pop(oldest, None)

    async def cached(self, key: tuple, ttl_s: float,
                     factory: Callable[[], Awaitable[Any]]) -> Any:
        """Get `key`, else fetch via `factory()` under a per-key lock.

        Implements the canonical double-checked-lock pattern so concurrent
        callers on a cold/expired key wait for one fetch instead of each firing
        a duplicate upstream request (thundering herd). `factory` is an async
        callable returning the value to cache; a `None` result is treated as a
        failed/empty fetch and is NOT cached (the package's graceful-miss
        convention), so transient failures aren't pinned for the TTL."""
        v = self.get(key, ttl_s)
        if v is not None:
            return v
        async with self.lock(key):
            v = self.get(key, ttl_s)
            if v is not None:
                return v
            v = await factory()
            if v is not None:
                self.put(key, v)
            return v
