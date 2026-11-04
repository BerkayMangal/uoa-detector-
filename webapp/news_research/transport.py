"""Observe existing UW news requests without changing their calls or return values."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine
from webapp.live_alpha.uw import JsonClient
from webapp.news_research import storage
from webapp.news_research.config import settings

STATE: dict[str, object] = {}
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)
_PATH = "/api/news/headlines"


class RecordingClient:
    def __init__(self, client: JsonClient, engine: Engine, *, clock: Callable[[], datetime] | None = None, timeout_seconds: float | None = None, application_checked_at: datetime | None = None) -> None:
        self.client = client
        self.engine = engine
        self.application_checked_at = application_checked_at
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings().recording_timeout_seconds

    def __getattr__(self, name: str) -> object:
        # Preserve optional native quota-counter attributes and their absence.
        value: object = getattr(self.client, name)
        return value

    def _failure(self, ticker: str, received: datetime, category: str) -> None:
        storage.mark_failure(self.engine, ticker, received)
        STATE.update(status="failed", at=received.isoformat(), ticker=ticker, error=category)
        _LOG.warning("news research recording failed (%s)", category)

    async def _record(self, ticker: str, payload: object, requested: datetime, received: datetime, succeeded: bool) -> None:
        if not _LOCK.acquire(blocking=False):
            self._failure(ticker, received, "writer_busy")
            return

        def write() -> str:
            try:
                return storage.append(self.engine, ticker, payload, requested, received, succeeded=succeeded, clock=self.clock, application_checked_at=self.application_checked_at)
            finally:
                # wait_for cannot stop an already running DB thread. Keep this
                # lock until its actual completion, preventing a thread backlog.
                _LOCK.release()

        pending = asyncio.create_task(asyncio.to_thread(write))

        def consume(done: asyncio.Task[str]) -> None:
            if not done.cancelled():
                done.exception()  # retrieve late failures after a timeout

        pending.add_done_callback(consume)
        try:
            key = await asyncio.wait_for(asyncio.shield(pending), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            self._failure(ticker, received, "recording_cancelled")
            raise
        except Exception as error:
            self._failure(ticker, received, type(error).__name__)
        else:
            STATE.update(status="ok" if succeeded else "retrieval_failed", at=received.isoformat(), ticker=ticker, packet_key=key, error=None)

    async def request_json(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
        # Any is the existing untyped vendor JSON boundary; payloads are returned
        # by identity, never parsed or rewritten on the live decision path.
        if path != _PATH or method.upper() != "GET":
            return await self.client.request_json(path, params=params, method=method)
        raw = (params or {}).get("ticker")
        ticker = raw.strip().upper() if isinstance(raw, str) else ""
        if not re.fullmatch(r"[A-Z0-9.\-]{1,16}", ticker):
            return await self.client.request_json(path, params=params, method=method)
        try:
            requested = storage.aware(self.clock())
        except Exception:
            self._failure(ticker, datetime.now(UTC), "physical_clock_invalid")
            return await self.client.request_json(path, params=params, method=method)
        try:
            response = await self.client.request_json(path, params=params, method=method)
        except asyncio.CancelledError:
            self._failure(ticker, datetime.now(UTC), "retrieval_cancelled")
            raise
        except Exception as error:
            try:
                received = storage.aware(self.clock())
            except Exception:
                self._failure(ticker, datetime.now(UTC), "physical_clock_invalid")
            else:
                await self._record(ticker, {"research_failure": type(error).__name__}, requested, received, False)
            raise
        try:
            received = storage.aware(self.clock())
        except Exception:
            self._failure(ticker, datetime.now(UTC), "physical_clock_invalid")
            return response
        await self._record(ticker, response, requested, received, True)
        return response
