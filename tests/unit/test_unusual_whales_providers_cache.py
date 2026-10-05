"""Tests for ``unusual_whales.providers._cache.TTLCache``.

Pins:
  - ttl=0 → cache disabled, every call invokes loader
  - cache hit returns stored value without calling loader
  - cache miss after TTL expiry calls loader again
  - concurrent get_or_fetch on same key dedupes loader calls
  - invalidate() drops a single entry
  - clear() drops all entries
  - monotonic clock used (wall-clock not consulted)
"""

from __future__ import annotations

import asyncio

import pytest

from uoa_detector.sources.unusual_whales.providers._cache import TTLCache


@pytest.mark.asyncio
async def test_ttl_zero_disables_cache() -> None:
    """ttl_seconds=0 → enabled=False; every call hits loader."""
    cache = TTLCache[int](ttl_seconds=0)
    assert cache.enabled is False
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return 42

    v1 = await cache.get_or_fetch("k", loader=loader)
    v2 = await cache.get_or_fetch("k", loader=loader)
    assert v1 == 42
    assert v2 == 42
    assert calls == 2  # no caching


@pytest.mark.asyncio
async def test_cache_hit_skips_loader() -> None:
    """Within TTL window, second call skips loader."""
    cache = TTLCache[int](ttl_seconds=60)
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return 99

    v1 = await cache.get_or_fetch("k", loader=loader)
    v2 = await cache.get_or_fetch("k", loader=loader)
    assert v1 == 99
    assert v2 == 99
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_calls_dedupe_loader() -> None:
    """Two concurrent calls for the same key share one loader."""
    cache = TTLCache[int](ttl_seconds=60)
    calls = 0
    enter = asyncio.Event()
    release = asyncio.Event()

    async def loader() -> int:
        nonlocal calls
        calls += 1
        enter.set()
        await release.wait()
        return 7

    task1 = asyncio.create_task(cache.get_or_fetch("k", loader=loader))
    await enter.wait()
    task2 = asyncio.create_task(cache.get_or_fetch("k", loader=loader))
    # Give task2 a moment to enter the lock
    await asyncio.sleep(0.01)
    release.set()
    v1 = await task1
    v2 = await task2
    assert v1 == 7
    assert v2 == 7
    # Single loader execution shared between callers
    assert calls == 1


@pytest.mark.asyncio
async def test_invalidate_drops_single_entry() -> None:
    cache = TTLCache[int](ttl_seconds=60)
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_fetch("k1", loader=loader)
    await cache.get_or_fetch("k2", loader=loader)
    cache.invalidate("k1")
    await cache.get_or_fetch("k1", loader=loader)
    await cache.get_or_fetch("k2", loader=loader)  # still cached
    assert calls == 3  # k1 fetched twice, k2 once


@pytest.mark.asyncio
async def test_clear_drops_all_entries() -> None:
    cache = TTLCache[int](ttl_seconds=60)
    calls = 0

    async def loader() -> int:
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_fetch("a", loader=loader)
    await cache.get_or_fetch("b", loader=loader)
    cache.clear()
    assert len(cache) == 0
    await cache.get_or_fetch("a", loader=loader)
    await cache.get_or_fetch("b", loader=loader)
    assert calls == 4


@pytest.mark.asyncio
async def test_different_keys_independent() -> None:
    """Cache entries are per-key."""
    cache = TTLCache[str](ttl_seconds=60)

    async def loader_a() -> str:
        return "a"

    async def loader_b() -> str:
        return "b"

    va = await cache.get_or_fetch("a", loader=loader_a)
    vb = await cache.get_or_fetch("b", loader=loader_b)
    assert va == "a"
    assert vb == "b"
    assert len(cache) == 2


@pytest.mark.asyncio
async def test_loader_exception_not_cached() -> None:
    """If loader raises, the cache stays empty so next call retries."""
    cache = TTLCache[int](ttl_seconds=60)
    attempts = 0

    async def loader() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            msg = "transient"
            raise RuntimeError(msg)
        return 100

    with pytest.raises(RuntimeError, match="transient"):
        await cache.get_or_fetch("k", loader=loader)
    v = await cache.get_or_fetch("k", loader=loader)
    assert v == 100
    assert attempts == 2


@pytest.mark.asyncio
async def test_timed_out_waiter_leaves_the_loader_running() -> None:
    """A stage timeout must not kill the fetch (live 2026-10-05, M25).

    Every stage wraps its provider call in ``asyncio.wait_for`` (D7). When the
    loader ran inside the waiter's task, that cancellation discarded the
    in-flight fetch, so a fetch slower than the stage timeout could never
    finish: every event restarted it and the axis scored neutral forever.
    """
    calls = 0

    async def slow_loader() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return 7

    cache = TTLCache[int](ttl_seconds=60)
    # Event 1: gives up after 50 ms, as a stage timeout would.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(cache.get_or_fetch("k", loader=slow_loader), timeout=0.05)
    # The fetch is still running, so event 2 joins it instead of restarting it.
    assert await asyncio.wait_for(cache.get_or_fetch("k", loader=slow_loader), timeout=1.0) == 7
    # Event 3 is a plain cache hit.
    assert await cache.get_or_fetch("k", loader=slow_loader) == 7
    assert calls == 1


@pytest.mark.asyncio
async def test_a_loader_every_waiter_abandoned_still_caches() -> None:
    """Nobody is awaiting when the fetch lands; the next caller still gets it."""
    calls = 0

    async def slow_loader() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 11

    cache = TTLCache[int](ttl_seconds=60)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(cache.get_or_fetch("k", loader=slow_loader), timeout=0.01)
    await asyncio.sleep(0.1)  # the abandoned loader finishes alone
    assert await cache.get_or_fetch("k", loader=slow_loader) == 11
    assert calls == 1
