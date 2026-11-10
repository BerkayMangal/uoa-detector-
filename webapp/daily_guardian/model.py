"""Bounded receipt-verified coverage and durable maintenance ownership reads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import previous_session_before
from webapp.alpha_lab.db import digest
from webapp.board.db import session_factory
from webapp.calibration.settings import calendar
from webapp.calibration.storage import DailyBar
from webapp.call_research import coverage, repair_operator
from webapp.call_research.settings import settings as research_settings
from webapp.call_research.storage import DailyReceipt
from webapp.daily_guardian import config
from webapp.opening.model import utc
from webapp.research_readiness import settled_day


@dataclass(frozen=True)
class CoverageState:
    status: str
    expected_day: date | None
    ready: int
    expected: int
    missing: int
    unverified: int = 0
    limited: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Ownership:
    status: str = "unclaimed"
    request_id: str | None = None
    result_status: str | None = None
    http_calls: int | None = None
    remaining: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reason: str = ""


def request_id(day: date) -> str:
    return config.settings().version + "_" + day.strftime("%Y%m%d")


def coverage_snapshot(engine: Engine, now: datetime) -> CoverageState:
    at = utc(now)
    expected = settled_day(at)
    if expected is None:
        return CoverageState("calendar_unknown", None, 0, 0, 0)
    names = coverage.targets()
    count = len(names)
    tables = set(inspect(engine).get_table_names())
    if not {DailyBar.__tablename__, DailyReceipt.__tablename__} <= tables:
        return CoverageState("storage_unavailable", expected, 0, count, count)
    raw, raw_limited = coverage.observed(engine, at)
    verified, verified_limited = coverage.observed(engine, at, verified=True)
    if raw_limited or verified_limited:
        return CoverageState("limited", expected, 0, count, count, limited=True)
    days: list[date] = [expected]
    for _ in range(research_settings().persistence_sessions):
        previous = previous_session_before(days[-1], calendar())
        if previous is None:
            return CoverageState("calendar_unknown", expected, 0, count, count)
        days.append(previous)
    wanted = {(name, day) for name in names for day in days}
    raw_points = {(p.bar.ticker, p.bar.day): p for p in raw if (p.bar.ticker, p.bar.day) in wanted}
    verified_points = {
        (p.bar.ticker, p.bar.day): p for p in verified if (p.bar.ticker, p.bar.day) in wanted
    }
    unverified = sum(key not in verified_points for key in raw_points)
    invalid = sum(not p.bar.valid for p in raw_points.values())
    ready = sum(
        all((name, day) in verified_points and verified_points[name, day].bar.valid for day in days)
        for name in names
    )
    if unverified:
        return CoverageState("unverified", expected, ready, count, count - ready, unverified)
    if invalid:
        return CoverageState(
            "invalid_data",
            expected,
            ready,
            count,
            count - ready,
            reason="Existing required bars are invalid; append-only repair cannot replace them.",
        )
    return CoverageState(
        "ready" if ready == count else "missing", expected, ready, count, count - ready
    )


def _count(value: object, maximum: int | None = None) -> int | None:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        return None
    return value


def ownership(
    engine: Engine, now: datetime, day: date | None, own_id: str | None = None
) -> Ownership:
    """Any unresolved historical owner blocks new work; raw source packets are never read."""
    at = utc(now)
    if not inspect(engine).has_table(repair_operator.RepairRecord.__tablename__):
        return Ownership()
    limit = config.settings().owner_read_limit
    with session_factory(engine)() as s:
        claims = tuple(
            s.scalars(
                select(repair_operator.RepairRecord)
                .where(repair_operator.RepairRecord.kind == "claimed")
                .order_by(repair_operator.RepairRecord.observed_at.desc())
                .limit(limit + 1)
            )
        )
        if len(claims) > limit:
            return Ownership(
                "ownership_limited", reason="Maintenance ownership read bound reached."
            )
        ids = tuple(row.request_id for row in claims)
        results = (
            tuple(
                s.scalars(
                    select(repair_operator.RepairRecord)
                    .where(
                        repair_operator.RepairRecord.kind == "result",
                        repair_operator.RepairRecord.request_id.in_(ids),
                    )
                    .limit(limit + 1)
                )
            )
            if ids
            else ()
        )
    if len(results) > limit:
        return Ownership("ownership_limited")
    found = {row.request_id: row for row in results}
    own: Ownership | None = None
    same_day: Ownership | None = None
    for claim in claims:
        started = utc(claim.observed_at)
        if started > at or claim.key != digest((claim.request_id, "claimed", "")):
            return Ownership("ownership_unverified", reason="Future or unmatched ownership record.")
        final = found.get(claim.request_id)
        if final is None:
            return Ownership(
                "already_claimed" if claim.request_id == own_id else "manual_pending",
                claim.request_id,
                started_at=started,
                reason="Claim has no final result; never replay.",
            )
        ended = utc(final.observed_at)
        if (
            ended < started
            or ended > at
            or final.key != digest((claim.request_id, "result", ""))
            or len(final.payload_json.encode()) > config.settings().max_result_bytes
        ):
            return Ownership("ownership_unverified")
        try:
            # Any is restricted to the existing immutable result JSON adapter.
            body: dict[str, Any] = json.loads(final.payload_json)
            if not isinstance(body, dict) or body.get("status") not in {
                "complete",
                "partial",
                "stopped",
            }:
                raise ValueError("unknown_result")
            result_day = (
                date.fromisoformat(str(body["expected_day"]))
                if body.get("expected_day")
                else settled_day(started)
            )
        except (ValueError, TypeError):
            return Ownership("ownership_unverified")
        row = Ownership(
            "already_claimed",
            claim.request_id,
            str(body["status"]),
            _count(body.get("http_calls"), config.settings().max_requests),
            _count(body.get("remaining")),
            started,
            ended,
        )
        if claim.request_id == own_id:
            own = row
        if day is not None and result_day == day:
            same_day = Ownership(
                "manual_completed",
                row.request_id,
                row.result_status,
                row.http_calls,
                row.remaining,
                started,
                ended,
                "A completed attempt for this settled session already exists; no automatic retry.",
            )
    return own or same_day or Ownership()
