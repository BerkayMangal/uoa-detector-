"""Isolated, bounded coverage guardian using the existing one-shot collector."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import normalize_database_url
from webapp.call_research import daily_repair, repair_operator
from webapp.daily_guardian import config
from webapp.daily_guardian.model import coverage_snapshot, ownership, request_id
from webapp.history_capture.capture import CaptureStoppedError, Client
from webapp.history_capture.capture import Settings as CaptureSettings
from webapp.opening.model import utc
from webapp.research_readiness import settled_day

STATE: dict[str, object] = {}
_ET = ZoneInfo("America/New_York")
_ROOT = Path("/tmp/uoa-daily-repair-guardian")
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"uoa_daily_maintenance_admission_v1").digest()[:8], signed=True
)
_SQLITE_LOCKS: dict[str, threading.Lock] = {}
_SQLITE_LOCKS_GUARD = threading.Lock()
Runner = Callable[[Engine, str, Path], Awaitable[bool]]


def enabled() -> bool:
    return config.enabled()


def safe_status() -> dict[str, object]:
    allowed = {
        "status",
        "at",
        "expected_day",
        "ready",
        "expected",
        "missing",
        "unverified",
        "request_id",
        "owner_status",
        "result_status",
        "http_calls",
        "remaining",
    }
    return {k: v for k, v in STATE.items() if k in allowed and isinstance(v, str | int | bool)}


def configured_engine(database_url: str) -> Engine:
    url = normalize_database_url(database_url)
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return create_engine(url, future=True, pool_pre_ping=True)
    original = parsed.query.get("options", os.getenv("PGOPTIONS", ""))
    if not isinstance(original, str):
        raise ValueError("ambiguous_database_options")
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        connect_args={
            "options": original + " -c statement_timeout=2000"
            " -c lock_timeout=1000 -c max_parallel_workers_per_gather=0"
        },
    )


def window_status(now: datetime) -> str:
    if now.tzinfo is None:
        return "calendar_unknown"
    at = utc(now).astimezone(_ET)
    known = is_session_day(at.date(), load_live_alpha_settings().calendar)
    if known is None:
        return "calendar_unknown"
    if not known:
        return "closed_day"
    cfg = config.settings()
    if (
        not time.fromisoformat(cfg.start_et)
        <= at.time().replace(tzinfo=None)
        < time.fromisoformat(cfg.stop_et)
    ):
        return "outside_window"
    return "allowed"


def _limits_match() -> bool:
    expected = config.settings()
    actual = daily_repair.settings()
    return actual.model_dump() == {
        "version": "daily_repair_v1",
        "history_sessions": expected.history_sessions,
        "max_requests": expected.max_requests,
        "quota_reserve": expected.quota_reserve,
        "spacing_seconds": expected.spacing_seconds,
        "timeout_seconds": expected.timeout_seconds,
        "max_packet_rows": expected.max_packet_rows,
    }


@contextmanager
def _admission(engine: Engine) -> Iterator[bool]:
    """Shared session mutex; an unknown unlock never returns a locked pooled session."""
    if engine.dialect.name != "postgresql":
        key = str(engine.url)
        with _SQLITE_LOCKS_GUARD:
            lock = _SQLITE_LOCKS.setdefault(key, threading.Lock())
        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
        return
    connection: Connection | None = None
    acquired = False
    try:
        connection = engine.connect()
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = (
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
            ).scalar_one()
            is True
        )
    except Exception:
        if connection is not None:
            try:
                connection.invalidate()
            finally:
                connection.close()
        yield False
        return
    if not acquired:
        connection.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            if (
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
                ).scalar_one()
                is not True
            ):
                connection.invalidate()
        except Exception:
            connection.invalidate()
        finally:
            connection.close()


async def guarded_run(
    engine: Engine, output: Path, *, expected_day: date, clock: Callable[[], datetime]
) -> dict[str, Any]:
    """Original collector/budgets, with a final post-spacing per-wire time guard."""
    at = utc(clock())
    if window_status(at) != "allowed" or settled_day(at) != expected_day or not _limits_match():
        raise CaptureStoppedError("guardian_admission_changed")
    todo = daily_repair.plan(engine, at)
    if todo.expected_day != expected_day:
        raise CaptureStoppedError("guardian_settled_day_changed")
    key = os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not key:
        raise CaptureStoppedError("missing_credential")
    cfg = daily_repair.settings()
    bounded = CaptureSettings(
        version=cfg.version,
        start=min(todo.days),
        end=todo.expected_day,
        tickers=todo.names,
        benchmark="SPY",
        max_requests=cfg.max_requests,
        quota_reserve=cfg.quota_reserve,
        spacing_seconds=cfg.spacing_seconds,
        timeout_seconds=cfg.timeout_seconds,
        max_flow_pages=1,
        flow_page_limit=1,
    )

    last_at = at

    async def before_wire(_request: httpx.Request) -> None:
        nonlocal last_at
        current = utc(clock())
        if current < last_at:
            raise CaptureStoppedError("physical_clock_reversed")
        last_at = current
        if (
            window_status(current) != "allowed"
            or settled_day(current) != expected_day
            or not _limits_match()
        ):
            raise CaptureStoppedError("guardian_window_or_session_changed")

    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + key},
        timeout=cfg.timeout_seconds,
        follow_redirects=False,
        event_hooks={"request": [before_wire]},
    ) as http:
        return await daily_repair.repair(engine, Client(http, bounded), output, clock)


async def tick(
    engine: Engine,
    now: datetime,
    *,
    clock: Callable[[], datetime] | None = None,
    runner: Runner | None = None,
    root: Path | None = None,
) -> dict[str, object]:
    if now.tzinfo is None:
        return {"status": "clock_unknown"}
    at = utc(now)
    current_clock = clock or (lambda: datetime.now(UTC))
    if not enabled():
        return {"status": "disabled"}
    try:
        cfg = config.settings()
        if not _limits_match():
            return {"status": "config_drift"}
        window = window_status(at)
        if window != "allowed":
            return {"status": window}
        with _admission(engine) as locked:
            if not locked:
                return {"status": "maintenance_busy"}
            checked = utc(current_clock())
            if checked < at:
                return {"status": "clock_reversed"}
            if window_status(checked) != "allowed":
                return {"status": "outside_window"}
            data = coverage_snapshot(engine, checked)
            summary: dict[str, object] = {
                "status": data.status,
                "expected_day": data.expected_day.isoformat() if data.expected_day else "",
                "ready": data.ready,
                "expected": data.expected,
                "missing": data.missing,
                "unverified": data.unverified,
            }
            if data.status != "missing" or data.expected_day is None:
                return summary
            expected_day = data.expected_day
            if data.expected > cfg.max_requests:
                return summary | {"status": "membership_drift"}
            own_id = request_id(expected_day)
            owner = ownership(engine, checked, expected_day, own_id)
            if owner.status != "unclaimed":
                return summary | {
                    "status": owner.status,
                    "request_id": owner.request_id or own_id,
                    "owner_status": owner.status,
                    "result_status": owner.result_status or "",
                }
            operation: Runner
            if runner is None:

                async def invoke(bound: Engine, identity: str, destination: Path) -> bool:
                    async def run_once(database: Engine, output: Path) -> dict[str, Any]:
                        return await guarded_run(
                            database, output, expected_day=expected_day, clock=current_clock
                        )

                    return await repair_operator.once(bound, identity, destination, run=run_once)

                operation = invoke
            else:
                operation = runner
            try:
                await operation(engine, own_id, root or _ROOT)
            except Exception:
                return summary | {"status": "operator_pending", "request_id": own_id}
            finished = utc(current_clock())
            if finished < checked:
                return summary | {"status": "clock_reversed", "request_id": own_id}
            after = coverage_snapshot(engine, finished)
            summary.update(
                ready=after.ready,
                expected=after.expected,
                missing=after.missing,
                unverified=after.unverified,
            )
            owner = ownership(engine, finished, expected_day, own_id)
            return summary | {
                "status": "operator_finished" if owner.finished_at else "operator_pending",
                "request_id": own_id,
                "owner_status": owner.status,
                "result_status": owner.result_status or "",
                "http_calls": owner.http_calls if owner.http_calls is not None else 0,
                "remaining": owner.remaining if owner.remaining is not None else -1,
            }
    except Exception:
        return {"status": "storage_unavailable"}


async def manual_requested(
    database_url: str,
    *,
    clock: Callable[[], datetime] | None = None,
    runner: Runner | None = None,
    root: Path | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_wait_cycles: int | None = None,
) -> None:
    """Explicit manual requests keep their semantics and share guardian admission."""
    identity = os.environ.get("DAILY_REPAIR_REQUEST", "").strip()
    if not identity:
        return
    engine = configured_engine(database_url)
    current_clock = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    waits = 0
    try:
        while True:
            with _admission(engine) as locked:
                if locked:
                    owner = ownership(engine, utc(current_clock()), None, identity)
                    if owner.status != "unclaimed":
                        return
                    await (runner or repair_operator.once)(
                        engine, identity, root or Path("/tmp/uoa-daily-repair")
                    )
                    return
            waits += 1
            if max_wait_cycles is not None and waits >= max_wait_cycles:
                return
            await pause(config.settings().poll_seconds)
    finally:
        engine.dispose()


async def loop(
    database_url: str,
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
    runner: Runner | None = None,
) -> None:
    if not enabled() or (max_cycles is not None and max_cycles < 1):
        return
    current_clock = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    engine = configured_engine(database_url)
    STATE.clear()
    STATE.update(status="starting")
    try:
        cycles = 0
        while True:
            at = utc(current_clock())
            result = await tick(engine, at, clock=current_clock, runner=runner)
            STATE.clear()
            STATE.update(result, at=utc(current_clock()).isoformat())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            await pause(
                config.settings().poll_seconds
                if result.get("status") != "outside_window"
                else config.settings().idle_poll_seconds
            )
    finally:
        engine.dispose()
