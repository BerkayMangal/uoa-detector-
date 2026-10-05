"""Generic in-memory TTL cache for UW providers.

A small, async-friendly cache keyed by hashable request shapes. Each
provider holds one cache instance configured with its TTL from
``UnusualWhalesProviderCacheTTL``. TTL=0 disables caching (every
``get_or_fetch`` call invokes the loader).

The cache uses a monotonic clock so wall-clock jumps don't expire
entries early. Concurrent ``get_or_fetch`` calls for the same key
share a single in-flight loader task — prevents the thundering-herd
effect when many stages query the same value at once.

decision (the loader survives a cancelled waiter):
  Every stage wraps its provider call in ``asyncio.wait_for`` (D7), so a
  slow fetch is cancelled at the stage timeout. When the loader ran inside
  the waiter's own task, that cancellation killed the fetch before it could
  be cached, and the next event started the same fetch from zero: a fetch
  slower than the stage timeout could never complete, so the axis scored
  neutral forever (live 2026-10-05: M25 peer flow timed out on every cycle
  for NVDA, AMD, MSFT, AMZN and AAPL). The loader now runs in its own task
  that caches its own result, and waiters ``shield`` it. A timed-out waiter
  therefore leaves the fetch running and the next caller finds it cached or
  joins it mid-flight.

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
    _inflight: dict[Hashable, asyncio.Task[T]] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def _start(self, key: Hashable, loader: Callable[[], Awaitable[T]]) -> asyncio.Task[T]:
        """The one running loader for ``key``, started if there is none."""
        task = self._inflight.get(key)
        if task is not None and not task.done():
            return task

        async def _load_and_store() -> T:
            value = await loader()
            self._entries[key] = _Entry(
                value=value,
                expires_at=time_module.monotonic() + float(self.ttl_seconds),
            )
            return value

        def _done(finished: asyncio.Task[T]) -> None:
            # Free the slot, and read any exception so a failed load whose
            # waiters all timed out is not reported as "never retrieved".
            self._inflight.pop(key, None)
            if not finished.cancelled():
                finished.exception()

        task = asyncio.ensure_future(_load_and_store())
        self._inflight[key] = task
        task.add_done_callback(_done)
        return task

    async def get_or_fetch(
        self,
        key: Hashable,
        *,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        """Return cached value, or call ``loader()`` and cache its result.

        ``loader`` is awaited at most once per (key, TTL window) — a
        second concurrent caller for the same key joins the in-flight
        loader rather than firing a duplicate request. Cancelling this
        call (a stage timeout) does not cancel the loader.
        """
        if not self.enabled:
            return await loader()

        # Fast path: cache hit, not expired.
        now = time_module.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.value

        return await asyncio.shield(self._start(key, loader))

    def invalidate(self, key: Hashable) -> None:
        """Drop a single cached entry (operator escape hatch)."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop all cached entries."""
        self._entries.clear()

    def __len__(self) -> int:
        """Number of active (possibly expired) cache entries."""
        return len(self._entries)
