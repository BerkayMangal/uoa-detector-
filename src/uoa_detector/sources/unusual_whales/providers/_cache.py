"""Generic in-memory TTL cache for UW providers.

A small, async-friendly cache keyed by hashable request shapes. Each
provider holds one cache instance configured with its TTL from
``UnusualWhalesProviderCacheTTL``. TTL=0 disables caching (every
``get_or_fetch`` call invokes the loader).

The cache uses a monotonic clock so wall-clock jumps don't expire
entries early. Concurrent ``get_or_fetch`` calls for the same key
share a single in-flight loader via ``asyncio.Lock`` per key —
prevents the thundering-herd effect when many stages query the
same value at once.

Design notes:
  - Values are stored by reference (no copy). Callers must treat
    returned values as immutable.
  - Cache size is unbounded — UW providers are queried by ticker /
    contract / date, and the per-provider TTL keeps memory bounded
    in practice. If memory becomes a concern, an LRU bound can be
    added without changing the interface.
  - Thread-safe is NOT a goal; this is async-only. All access goes
    through the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import time as time_module
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float  # monotonic seconds


@dataclass
class TTLCache(Generic[T]):
    """Async TTL cache with per-key in-flight loader deduplication.

    Use:
      cache = TTLCache[str](ttl_seconds=300)
      value = await cache.get_or_fetch('AAPL', loader=lambda: fetch('AAPL'))
    """

    ttl_seconds: int
    _entries: dict[Hashable, _Entry[T]] = field(default_factory=dict)
    _locks: dict[Hashable, asyncio.Lock] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def _get_lock(self, key: Hashable) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get_or_fetch(
        self,
        key: Hashable,
        *,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        """Return cached value, or call ``loader()`` and cache its result.

        ``loader`` is awaited at most once per (key, TTL window) — a
        second concurrent caller for the same key blocks on the
        in-flight loader rather than firing a duplicate request.
        """
        if not self.enabled:
            return await loader()

        # Fast path: cache hit, not expired.
        now = time_module.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.value

        # Slow path: load (with per-key lock to dedupe concurrent loads).
        lock = self._get_lock(key)
        async with lock:
            # Re-check under lock — another waiter may have populated.
            now = time_module.monotonic()
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.value
            value = await loader()
            self._entries[key] = _Entry(
                value=value,
                expires_at=now + float(self.ttl_seconds),
            )
            return value

    def invalidate(self, key: Hashable) -> None:
        """Drop a single cached entry (operator escape hatch)."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop all cached entries."""
        self._entries.clear()

    def __len__(self) -> int:
        """Number of active (possibly expired) cache entries."""
        return len(self._entries)
