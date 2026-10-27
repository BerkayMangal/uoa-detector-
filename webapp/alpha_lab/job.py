"""Failure-isolated research hook; never changes the legacy cycle's outcome."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime

from sqlalchemy.engine import Engine

from webapp.alpha_lab.engine import STATE, observe
from webapp.alpha_lab.settings import load_settings
from webapp.live_alpha.store import ScanRow

_logger = logging.getLogger(__name__)
_RUNNING = threading.Lock()


class ObservationBusyError(RuntimeError):
    """A timed-out database thread is still completing its append-only work."""


def _observe_serial(engine: Engine, scan: ScanRow, now: datetime) -> None:
    # wait_for cannot terminate an already running to_thread call. Keep a slow
    # database from accumulating overlapping cycles or freezing a later arm first.
    if not _RUNNING.acquire(blocking=False):
        raise ObservationBusyError("previous research cycle is still running")
    try:
        observe(engine, scan, now)
    finally:
        _RUNNING.release()


async def run_shadow(engine: Engine, scan: ScanRow, now: datetime) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_observe_serial, engine, scan, now),
            timeout=load_settings().cycle_timeout_seconds,
        )
    except Exception as exc:
        STATE.clear()
        STATE.update(status="failed", at=now.isoformat(), error=type(exc).__name__)
        _logger.exception("alpha laboratory failed; legacy recommendations unaffected")
