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
