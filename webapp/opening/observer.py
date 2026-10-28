"""Failure-isolated single-flight observer of committed inputs, with no vendor I/O."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from webapp.desk.positions import build
from webapp.opening import storage
from webapp.opening.model import clock, settings, utc
from webapp.opening.outcomes import measure
from webapp.opening.policy import exits, project

STATE: dict[str, object] = {}
_RUNNING = threading.Lock()
_logger = logging.getLogger(__name__)


def observe(
    engine: Engine,
    now: datetime,
    *,
    wall_clock: Callable[[], datetime] | None = None,
    scan_id: str | None = None,
) -> None:
    if not _RUNNING.acquire(blocking=False):
        raise RuntimeError("opening observer already running")
    try:
        storage.ensure(engine)
        # Physical publication is never backdated to scan start. Tests inject a
        # physical clock; production always uses the actual UTC clock here.
        at = max(utc(now), utc((wall_clock or (lambda: datetime.now(UTC)))()))
        c = clock(at)
        scan = storage.source(engine, at, scan_id)
        if scan is None or c.day < settings().start_day:
            STATE.clear()
            STATE.update(status="no_scan", at=at.isoformat())
            return
        newest = storage.source(engine, at) if scan_id else scan
        superseded = newest is not None and newest.scan_id != scan.scan_id
        view = build(engine, at)
        at = max(at, utc((wall_clock or (lambda: datetime.now(UTC)))()))
        c = clock(at)
        exit_advice = exits((*view.manual.rows, *view.paper.rows), at, c)
        conflicts = {a.ticker for a in exit_advice}
        cards = scan.snapshot.get("cards", [])
        if len(cards) > settings().query_limit:
            raise ValueError("opening source universe bound exceeded")
        advice = exit_advice + tuple(
            project(scan, card, at, c, conflicts, superseded=superseded)
            for card in cards
            if isinstance(card, dict)
        )
        recorded = storage.publish(
            engine, scan, at, replace(c, checkpoint=None) if superseded else c, advice
        )
        measured = measure(engine, at)
        STATE.clear()
        STATE.update(
            status="ok",
            at=at.isoformat(),
            recorded=recorded,
            decisions=len(advice),
            results=measured,
        )
    finally:
        _RUNNING.release()


async def run(engine: Engine, now: datetime, scan_id: str | None = None) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(observe, engine, now, scan_id=scan_id),
            timeout=settings().cycle_timeout_seconds,
        )
    except Exception as exc:
        STATE.clear()
        STATE.update(status="failed", at=utc(now).isoformat(), error=type(exc).__name__)
        _logger.exception("opening journal failed; source cycle preserved")
