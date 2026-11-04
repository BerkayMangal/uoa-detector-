"""Independent bounded background collection, never called by a GET handler."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy.engine import Engine

from webapp.board.db import make_engine

from . import storage
from .config import Endpoint, enabled, settings
from .model import aware, parse, same_etag, stored
from .transport import Response, fetch

STATE: dict[str, object] = {}
_WRITER = threading.Lock()
_LOG = logging.getLogger(__name__)


async def _write(engine: Engine, endpoint: Endpoint, response: Response, reference: str | None,
                 clock: Callable[[], datetime]) -> bool:
    if not _WRITER.acquire(blocking=False):
        storage.mark_failure(engine, endpoint.provider, clock())
        STATE[endpoint.provider] = "writer_busy"
        return False

    def write() -> str:
        try:
            return storage.append(engine, endpoint.provider, response.requested_at, response.received_at,
                                  response.status_code, response.category, response.raw, etag=response.etag,
                                  body_packet_key=reference, parsed=response.parsed, clock=clock)
        finally:
            _WRITER.release()
    task = asyncio.create_task(asyncio.to_thread(write))
    def consume(done: asyncio.Task[str]) -> None:
        if not done.cancelled():
            done.exception()
    task.add_done_callback(consume)
    try:
        await asyncio.wait_for(asyncio.shield(task), settings().recording_timeout_seconds)
    except asyncio.CancelledError:
        storage.mark_failure(engine, endpoint.provider, clock())
        raise
    except Exception:
        storage.mark_failure(engine, endpoint.provider, clock())
        STATE[endpoint.provider] = "recording_unavailable"
        return False
    STATE[endpoint.provider] = response.category
    return True


async def collect(engine: Engine, endpoint: Endpoint, token: str, *,
                  transport: httpx.AsyncBaseTransport | None = None,
                  clock: Callable[[], datetime] | None = None) -> bool:
    """One provider/one request; injectable transport is exclusively for tests."""
    clock = clock or (lambda: datetime.now(UTC))
    cache = None
    try:
        cache = storage.previous_body(engine, endpoint.provider, aware(clock()))
        if cache:
            parse(storage.body(cache[0]), endpoint, stored(cache[0].received_at))
    except Exception:
        cache = None
    response = await fetch(endpoint, token, etag=cache[0].etag if cache else None, transport=transport, clock=clock)
    reference: str | None = None
    if response.category == "not_modified":
        if cache is None or not cache[0].etag or (response.etag is not None and not same_etag(response.etag, cache[0].etag)):
            response = Response(response.requested_at, response.received_at, response.status_code, "orphan_not_modified")
        else:
            reference = cache[0].key
    return await _write(engine, endpoint, response, reference, clock)


async def loop(database_url: str) -> None:
    """Root runs this on its existing isolated service thread for native DB I/O."""
    if not enabled():
        STATE["status"] = "disabled"
        return
    engine = make_engine(database_url)
    try:
        storage.ensure(engine)
        while True:
            tokens = {p.provider: os.environ.get(p.token_env, "") for p in settings().providers}
            if tokens["mahalle"] and tokens["mahalle"] == tokens["stajyer"]:
                # Dedicated independently scoped credentials must not silently
                # become a shared cross-origin bearer credential.
                tokens = {"mahalle": "", "stajyer": ""}
            for endpoint in settings().providers:
                try:
                    await collect(engine, endpoint, tokens[endpoint.provider])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    storage.mark_failure(engine, endpoint.provider, datetime.now(UTC))
                    STATE[endpoint.provider] = "collection_unavailable"
                    _LOG.warning("shared context collection unavailable (%s)", endpoint.provider)
            await asyncio.sleep(settings().poll_seconds)
    finally:
        engine.dispose()
