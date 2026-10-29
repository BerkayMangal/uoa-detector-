"""Keep PostgreSQL background I/O on its own loop without moving clients/sessions.

The caller still owns supervision/backoff. A canceled native I/O call cannot be
forcibly interrupted; its daemon must finish existing cleanup, never start a
second attempt. SQLite deliberately keeps its original thread/loop affinity.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextlib import suppress

from sqlalchemy.engine import make_url

from webapp.board.db import normalize_database_url


async def run_service(database_url: str, factory: Callable[[], Awaitable[None]], name: str) -> None:
    if make_url(normalize_database_url(database_url)).get_backend_name() != "postgresql":
        await factory()
        return
    finished: Future[None] = Future()
    control: Future[tuple[asyncio.AbstractEventLoop, asyncio.Task[None]]] = Future()
    stopped = threading.Event()
    context = contextvars.copy_context()

    def execute() -> None:
        if not finished.set_running_or_notify_cancel():
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        error: BaseException | None = None

        async def invoke() -> None:
            await factory()

        task = loop.create_task(invoke(), name=name)
        control.set_result((loop, task))
        if stopped.is_set():
            task.cancel()
        try:
            loop.run_until_complete(task)
        except BaseException as exc:
            error = exc
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for child in pending:
                    child.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except BaseException as exc:
                if error is None:
                    error = exc
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        if error is None:
            finished.set_result(None)
        else:
            finished.set_exception(error)

    thread = threading.Thread(target=context.run, args=(execute,), name=f"uoa-{name}", daemon=True)
    thread.start()
    try:
        await asyncio.wrap_future(finished)
    except asyncio.CancelledError:
        stopped.set()
        if control.done():
            loop, task = control.result()
            # A completed worker may already have closed its own loop.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        raise
