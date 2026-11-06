"""Bounded operator export into the unchanged physical-news experiment.

This module is deliberately outside the frozen news research implementation.
It exposes inputs, never runs an optimizer or changes a live recommendation.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import session_factory
from webapp.live_alpha.store import LiveScan
from webapp.news_research import adapter, config
from webapp.opening.model import utc

MAX_SCANS = 256
MAX_SOURCE_CHARACTERS = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
HARD_MAX_SOURCE_CHARACTERS = 64 * 1024 * 1024
_LOCK = threading.Lock()


class ExportRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    day: date
    metadata_only: bool = False
    source_character_budget: int | None = Field(default=None, strict=True, ge=1, le=HARD_MAX_SOURCE_CHARACTERS)


class ExportRefusedError(ValueError):
    """Only public, fixed error codes cross the HTTP adapter boundary."""


@dataclass(frozen=True)
class Export:
    day: date
    since: datetime
    as_of: datetime
    exported_at: datetime
    profile_hash: str
    implementation_hash: str
    source_scans: int
    source_characters: int
    source_character_budget: int
    audit: adapter.ExportAudit
    method: str = "physical_news_session_export_v1"
    strategy_tests_executed: int = 0


def bounds(day: date, now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ExportRefusedError("aware_clock_required")
    now = utc(now)
    cal = load_live_alpha_settings().calendar
    zone = ZoneInfo(cal.timezone)
    start = datetime.combine(day, time(), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
    if end > now:
        raise ExportRefusedError("session_not_finished")
    if is_session_day(day, cal) is not True:
        raise ExportRefusedError("not_a_verified_session")
    # The frozen adapter's upper bound is inclusive. Adjacent exports must not
    # both include a scan completed exactly at midnight.
    return start, end - timedelta(microseconds=1)


@dataclass(frozen=True)
class SourceMetadata:
    day: date
    since: datetime
    as_of: datetime
    source_scans: int
    source_characters: int
    max_scans: int = MAX_SCANS
    default_character_budget: int = MAX_SOURCE_CHARACTERS
    hard_character_budget: int = HARD_MAX_SOURCE_CHARACTERS
    method: str = "physical_news_source_metadata_v1"
    strategy_tests_executed: int = 0


def source_metadata(engine: Engine, day: date, now: datetime) -> SourceMetadata:
    start, end = bounds(day, now)
    if LiveScan.__tablename__ not in inspect(engine).get_table_names():
        raise ExportRefusedError("scan_storage_unavailable")
    with session_factory(engine)() as session:
        first = session.scalar(select(func.min(LiveScan.finished_at)).where(
            LiveScan.finished_at <= end,
            LiveScan.snapshot_json.contains('"news_research_context":'),
        ))
        if first is None:
            raise ExportRefusedError("physical_context_unavailable")
        if day < utc(first).astimezone(ZoneInfo(load_live_alpha_settings().calendar.timezone)).date():
            raise ExportRefusedError("before_physical_context")
        count, characters = session.execute(select(
            func.count(), func.coalesce(func.sum(func.length(LiveScan.snapshot_json)), 0),
        ).where(LiveScan.finished_at >= start, LiveScan.finished_at <= end)).one()
    return SourceMetadata(day, start, end, count, characters)


def export_session(engine: Engine, day: date, now: datetime, *,
                   source_character_budget: int | None = None) -> Export:
    start, end = bounds(day, now)
    if source_character_budget is not None and (type(source_character_budget) is not int
            or not 1 <= source_character_budget <= HARD_MAX_SOURCE_CHARACTERS):
        raise ExportRefusedError("invalid_source_budget")
    budget = MAX_SOURCE_CHARACTERS if source_character_budget is None else source_character_budget
    if not _LOCK.acquire(blocking=False):
        raise ExportRefusedError("export_busy")
    try:
        metadata = source_metadata(engine, day, now)
        count, characters = metadata.source_scans, metadata.source_characters
        if count > MAX_SCANS or characters > budget:
            raise ExportRefusedError("source_limit_exceeded")
        try:
            audit = adapter.audit(engine, end, since=start)
        except ValueError as exc:
            raise ExportRefusedError("source_export_refused") from exc
        if audit.dataset is None:
            raise ExportRefusedError("dataset_unavailable")
        result = Export(day, start, end, utc(now), config.profile_hash(),
                        config.implementation_hash(), count, characters, budget, audit)
        # Inputs contain typed numbers/identities, never raw snapshot/headline JSON.
        if len(json.dumps(asdict(result), default=str, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ExportRefusedError("response_limit_exceeded")
        return result
    finally:
        _LOCK.release()
