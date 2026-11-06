"""Isolated once-per-completed-session fixed evaluator, with durable ownership."""
from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from webapp.board.db import normalize_database_url
from webapp.news_export import HARD_MAX_SOURCE_CHARACTERS, ExportRefusedError, export_session
from webapp.news_pilot import report, storage
from webapp.news_research import experiment

STATE: dict[str, object] = {}
_LOCK = threading.Lock()


def enabled() -> bool:
    return os.getenv("NEWS_PILOT_ENABLED", "true").strip().lower() in {"true", "1", "yes", "on"}


def safe_status() -> dict[str, object]:
    return {k: v for k, v in STATE.items() if k in {"status", "at", "imported", "completed", "failed"}
            and isinstance(v, str | int | bool)}


def configured_engine(database_url: str) -> Engine:
    normalized = normalize_database_url(database_url)
    parsed = make_url(normalized)
    if parsed.get_backend_name() != "postgresql":
        return create_engine(normalized, future=True, pool_pre_ping=True)
    options = parsed.query.get("options", os.environ.get("PGOPTIONS", ""))
    if not isinstance(options, str):
        raise ValueError("ambiguous_database_options")
    protected = (options + " -c statement_timeout=120000 -c lock_timeout=1000"
                 " -c max_parallel_workers_per_gather=0").strip()
    return create_engine(normalized, future=True, pool_pre_ping=True, pool_size=2, max_overflow=0,
                         connect_args={"options": protected})


def import_seed(engine: Engine, now: datetime) -> int:
    rows = report.load_seed(now)
    inserted = 0
    for row in rows:
        key = storage.claim(engine, date.fromisoformat(row["day"]), now)
        if key is not None:
            storage.complete(engine, key, row, now)
            inserted += 1
    return inserted


def process_day(engine: Engine, day: date, now: datetime, *,
                clock: Callable[[], datetime] | None = None) -> str:
    report.validate_day(day, now)
    if not _LOCK.acquire(blocking=False):
        return "busy"
    key: str | None = None
    try:
        key = storage.claim(engine, day, now)
        if key is None:
            return "already_claimed"
        STATE.update(status="evaluating", at=report.aware(now).isoformat())
        record_clock = clock or (lambda: datetime.now(UTC))
        try:
            exported = export_session(engine, day, now, source_character_budget=HARD_MAX_SOURCE_CHARACTERS)
            assert exported.audit.dataset is not None
            evaluated = experiment.evaluate(exported.audit.dataset)
            summary = report.summarize(exported, evaluated)
        except ExportRefusedError as exc:
            reason = str(exc) if str(exc) in {"source_limit_exceeded", "export_busy"} else "source_export_refused"
            storage.fail(engine, key, reason, report.aware(record_clock()))
            return "failed"
        except Exception:
            storage.fail(engine, key, "evaluation_failed", report.aware(record_clock()))
            return "failed"
        storage.complete(engine, key, summary, report.aware(record_clock()))
        return "completed"
    finally:
        _LOCK.release()


async def loop(database_url: str, *, clock: Callable[[], datetime] | None = None,
               sleep: Callable[[float], Awaitable[None]] | None = None,
               max_cycles: int | None = None) -> None:
    if not enabled():
        STATE.clear()
        STATE.update(status="disabled")
        return
    if max_cycles is not None and max_cycles < 1:
        return
    now = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    engine = configured_engine(database_url)
    STATE.clear()
    STATE.update(status="starting")
    try:
        storage.ensure(engine)
        try:
            imported = import_seed(engine, report.aware(now()))
        except Exception:
            STATE.update(status="seed_invalid")
            return
        STATE.update(imported=imported, completed=0, failed=0)
        cycles = 0
        while True:
            at = report.aware(now())
            day = report.latest_completed_day(at)
            try:
                result = process_day(engine, day, at, clock=now) if day is not None else "no_session"
                STATE.update(status=result, at=report.aware(now()).isoformat())
                if result in {"completed", "failed"}:
                    STATE[result] = int(str(STATE[result])) + 1
            except Exception:
                # Native errors can contain SQL/credentials: fixed status only.
                STATE.update(status="storage_failed", at=report.aware(now()).isoformat())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            await pause(300)
    finally:
        engine.dispose()
