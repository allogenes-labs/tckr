"""Async TTL cache for tckr source modules.

A cache entry is {value, fetched_at}. Stale entries remain available via
get_stale() so callers can fall back to last-known data (flagged stale) when a
fresh fetch fails. Keys are arbitrary hashable tuples chosen by each module.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    fetched_at: float

    def age(self) -> float:
        return time.time() - self.fetched_at

    def fresh(self, ttl_s: float) -> bool:
        return self.age() < ttl_s


class TTLCache:
    def __init__(self) -> None:
        self._d: dict[tuple, CacheEntry] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

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
        self._d[key] = CacheEntry(value=value, fetched_at=time.time())
