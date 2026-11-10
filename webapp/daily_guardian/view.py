"""SELECT-only authenticated maintenance projection, no provider or schema action."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.engine import Engine

from webapp.daily_guardian import config
from webapp.daily_guardian.model import coverage_snapshot, ownership, request_id
from webapp.opening.model import utc


@dataclass(frozen=True)
class Report:
    as_of: datetime
    status: str
    configured_enabled: bool
    method: str = "daily_guardian_v1"
    expected_day: date | None = None
    ready: int = 0
    expected: int = 0
    missing: int = 0
    unverified: int = 0
    limited: bool = False
    request_id: str | None = None
    owner_status: str | None = None
    result_status: str | None = None
    http_calls: int | None = None
    remaining: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reason: str = ""


def build(engine: Engine, now: datetime) -> Report:
    at = utc(now)
    try:
        data = coverage_snapshot(engine, at)
        own_id = request_id(data.expected_day) if data.expected_day else None
        owner = ownership(engine, at, data.expected_day, own_id)
        return Report(
            at,
            data.status,
            config.enabled(),
            expected_day=data.expected_day,
            ready=data.ready,
            expected=data.expected,
            missing=data.missing,
            unverified=data.unverified,
            limited=data.limited,
            request_id=owner.request_id or own_id,
            owner_status=owner.status,
            result_status=owner.result_status,
            http_calls=owner.http_calls,
            remaining=owner.remaining,
            started_at=owner.started_at,
            finished_at=owner.finished_at,
            reason=owner.reason or data.reason,
        )
    except Exception:
        return Report(
            at,
            "unavailable",
            config.enabled(),
            reason="Coverage or ownership could not be verified.",
        )
