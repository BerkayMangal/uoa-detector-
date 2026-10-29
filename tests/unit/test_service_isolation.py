"""Synchronous native DB waits must not occupy the HTTP loop or request pool."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable
from typing import Any

import anyio
import httpx
import pytest
from webapp import main
from webapp import service_isolation as isolation

from tests.unit.test_alfa_refresher_lifespan import _live_config

PG = "postgresql://fixture:local-only@never-contacted.invalid/fixture"


async def join(thread: threading.Thread) -> None:
    await asyncio.to_thread(thread.join, 3)
    assert not thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [PG, PG.replace("postgresql:", "postgres:"), PG.replace("postgresql:", "postgresql+psycopg:")],
)
async def test_pg_factory_entire_lifetime_owns_one_thread_loop_and_context(url: str) -> None:
    owner = threading.get_ident()
    parent_loop = asyncio.get_running_loop()
    trace = contextvars.ContextVar("fixture_trace", default="missing")
    token = trace.set("context-value")
    observed = []

    async def job() -> None:
        own = threading.current_thread()
        loop = asyncio.get_running_loop()
        observed.append((own.ident, loop, trace.get(), own.daemon))
        await asyncio.sleep(0)
        assert own.ident == threading.get_ident() and loop is asyncio.get_running_loop()

    try:
        await isolation.run_service(url, job, "fixture")
    finally:
        trace.reset(token)
    thread_id, loop, value, daemon = observed[0]
    assert thread_id != owner and loop is not parent_loop and loop.is_closed()
    assert value == "context-value" and daemon


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["sqlite:///:memory:", "sqlite:///local.db"])
async def test_sqlite_preserves_original_thread_affinity(url: str) -> None:
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()

    async def job() -> None:
        assert asyncio.get_running_loop() is loop and threading.get_ident() == owner

    await isolation.run_service(url, job, "sqlite-fixture")


@pytest.mark.asyncio
async def test_error_is_propagated_only_after_cleanup_and_closed_loop() -> None:
    refs = []
    ended = threading.Event()
    original = RuntimeError("controlled fixture")

    async def job() -> None:
        refs.append((threading.current_thread(), asyncio.get_running_loop()))
        try:
            raise original
        finally:
            ended.set()

    with pytest.raises(RuntimeError) as caught:
        await isolation.run_service(PG, job, "failure")
    assert caught.value is original and ended.is_set()
    await join(refs[0][0])
    assert refs[0][1].is_closed()


@pytest.mark.asyncio
async def test_cancel_reaches_the_owned_task_and_finishes_resources() -> None:
    started = threading.Event()
    ended = threading.Event()
    refs = []

    async def job() -> None:
        refs.append((threading.current_thread(), asyncio.get_running_loop()))
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            ended.set()

    task = asyncio.create_task(isolation.run_service(PG, job, "cancel"))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert await asyncio.to_thread(ended.wait, 3)
    await join(refs[0][0])
    assert refs[0][1].is_closed()


@pytest.mark.asyncio
async def test_cancel_during_native_wait_does_not_block_web_or_restart() -> None:
    started = threading.Event()
    released = threading.Event()
    refs = []
    calls = []

    async def job() -> None:
        calls.append("one")
        refs.append(threading.current_thread())
        started.set()
        assert released.wait(3)
        await asyncio.sleep(0)

    task = asyncio.create_task(isolation.run_service(PG, job, "blocked"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert refs[0].is_alive() and calls == ["one"]
        ping = asyncio.Event()
        asyncio.get_running_loop().call_soon(ping.set)
        await asyncio.wait_for(ping.wait(), 1)
    finally:
        released.set()
        if refs:
            await join(refs[0])
    assert calls == ["one"]


@pytest.mark.asyncio
async def test_cancel_before_thread_start_never_calls_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = []
    invoked = []

    class DelayedThread:
        def __init__(
            self, *, target: Callable[..., None], args: tuple[Any, ...], **kwargs: Any
        ) -> None:
            queued.append(lambda: target(*args))

        def start(self) -> None:
            pass

    async def job() -> None:
        invoked.append(True)

    monkeypatch.setattr(isolation.threading, "Thread", DelayedThread)
    task = asyncio.create_task(isolation.run_service(PG, job, "not-started"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert len(queued) == 1
    queued[0]()
    assert not invoked


@pytest.mark.asyncio
async def test_remaining_children_and_async_generators_are_cleaned() -> None:
    closed = threading.Event()
    child_closed = threading.Event()
    refs = []
    children = []

    async def stream():
        try:
            yield 1
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            child_closed.set()

    async def job() -> None:
        refs.append(stream())
        assert await anext(refs[-1]) == 1
        children.append(asyncio.create_task(child()))
        await asyncio.sleep(0)

    await isolation.run_service(PG, job, "cleanup")
    assert closed.is_set() and child_closed.is_set()


@pytest.mark.asyncio
async def test_real_lifespan_http_responds_while_three_db_jobs_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    refs = []
    signals = []

    def ready() -> None:
        signals.append(True)
        if len(signals) == 3:
            started.set()

    async def blocked(**kwargs: Any) -> None:
        refs.append(threading.current_thread())
        parent.call_soon_threadsafe(ready)
        assert release.wait(5)
        await asyncio.Event().wait()

    async def prerender() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "live_config_from_env", lambda: _live_config(PG))
    for name in ("run_live_worker", "gamma_refresh_loop", "board_refresh_loop"):
        monkeypatch.setattr(main, name, blocked)
    monkeypatch.setattr(main, "_board_prerender_loop", prerender)
    monkeypatch.setattr(main, "_start_live_alpha_thread", lambda _: None)
    try:
        async with main._lifespan(main.app):
            await asyncio.wait_for(started.wait(), 3)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
            ) as client:
                response = await asyncio.wait_for(client.get("/health"), 1)
                assert response.status_code == 200 and response.json()["ok"] is True
                assert all(t.ident != threading.get_ident() for t in refs)
    finally:
        release.set()
        for thread in refs:
            await join(thread)


@pytest.mark.asyncio
async def test_health_does_not_need_sync_request_pool_capacity() -> None:
    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    limiter.total_tokens = 1
    occupied = threading.Event()
    release = threading.Event()

    def blocking_request() -> None:
        occupied.set()
        assert release.wait(3)

    task = asyncio.create_task(anyio.to_thread.run_sync(blocking_request))
    try:
        while not occupied.is_set():
            await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
        ) as client:
            response = await asyncio.wait_for(client.get("/health"), 1)
        assert response.status_code == 200
        assert isinstance(main.health(), dict)  # Python-call contract preserved.
    finally:
        release.set()
        await task
        limiter.total_tokens = original
