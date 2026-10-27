"""Read-only prospective data diagnostics; no hypothesis or publication changes."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import (
    close_time,
    is_session_day,
    next_session_after,
    previous_session_before,
)
from webapp.alpha_lab.db import digest
from webapp.alpha_lab.flow import utc
from webapp.board.db import session_factory
from webapp.calibration.settings import calendar
from webapp.tournament import report, storage
from webapp.tournament.model import Config, Prepared, mask, prepare
from webapp.tournament.monitor import observed_bars
from webapp.tournament.settings import settings
from webapp.tournament_forward import AlfaTournamentCohort

STATE: dict[str, object] = {}
_LAST_CHECK: datetime | None = None
_LOCK = threading.Lock()
_logger = logging.getLogger(__name__)
REASONS = (
    "current_bar_missing",
    "history_missing",
    "feature_unavailable",
    "pattern_not_met",
    "threshold_not_met",
    "volume_unavailable",
    "volume_not_met",
    "market_filter",
    "eligible",
)


@dataclass(frozen=True)
class Family:
    family: str
    config_id: str
    reasons: dict[str, int]
    cohort_status: str
    cohort_members: int | None
    arm_counts: dict[str, int] | None


@dataclass(frozen=True)
class Snapshot:
    at: datetime
    status: str
    expected_day: date | None
    latest_day: date | None
    covered: int
    expected: int
    deadline: datetime | None
    deadline_open: bool
    families: tuple[Family, ...]


def aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("readiness timestamp must be timezone-aware")
    return now.astimezone(UTC)


def settled_day(now: datetime) -> date | None:
    cal = calendar()
    et = aware(now).astimezone(ZoneInfo(cal.timezone))
    known = is_session_day(et.date(), cal)
    if known is None:
        return None
    if known and et.time() >= close_time(et.date(), cal):
        return et.date()
    return previous_session_before(et.date(), cal)


def reasons(p: Prepared, c: Config, names: tuple[str, ...], latest: date) -> dict[str, int]:
    """Exclusive diagnostics, with original mask as the final source of truth."""
    s = settings()
    counts = dict.fromkeys(REASONS, 0)
    i, spy = p.days.index(latest), p.tickers.index("SPY")
    window = max(c.lookback, s.compression_slow + 1, s.atr_sessions + 1, s.volume_sessions)
    warmup = max(
        max(s.lookbacks), s.compression_slow + 1, s.atr_sessions + 1, s.volume_sessions, s.market_ma
    )
    eligible = mask(p, c)[i]
    for ticker in names:
        if ticker not in p.tickers or not p.valid[i, p.tickers.index(ticker)]:
            reason = "current_bar_missing"
        else:
            j = p.tickers.index(ticker)
            if i < warmup or not (
                np.all(p.valid[i - window : i + 1, j]) and np.all(p.valid[i - window : i + 1, spy])
            ):
                reason = "history_missing"
            elif not (
                math.isfinite(p.atr[i, j])
                and p.atr[i, j] > 0
                and math.isfinite(p.scores[c.family, c.lookback][i, j])
            ):
                reason = "feature_unavailable"
            elif not p.conditions[c.family, c.lookback][i, j]:
                reason = "pattern_not_met"
            elif p.scores[c.family, c.lookback][i, j] < (
                -c.threshold if c.family == "volatility_compression" else c.threshold
            ):
                reason = "threshold_not_met"
            elif c.volume > 0 and not math.isfinite(p.volume[i, j]):
                reason = "volume_unavailable"
            elif c.volume > 0 and p.volume[i, j] < c.volume:
                reason = "volume_not_met"
            elif not eligible[j]:
                reason = "market_filter"
            else:
                reason = "eligible"
        counts[reason] += 1
    assert sum(counts.values()) == len(names)
    assert counts["eligible"] == sum(
        bool(eligible[p.tickers.index(t)]) for t in names if t in p.tickers
    )
    return counts


def publication(
    engine: Engine, now: datetime, day: date | None, c: Config, report_hash: str
) -> tuple[str, int | None, dict[str, int] | None]:
    """Only actual, first-observed records; recomputed signals cannot add arms."""
    if day is None:
        return "unfrozen", None, None
    inspector = inspect(engine)
    status, members = "unfrozen", None
    counts = dict.fromkeys(settings().arms, 0)
    with session_factory(engine)() as session:
        if inspector.has_table(AlfaTournamentCohort.__tablename__):
            cohort = session.get(AlfaTournamentCohort, digest((report_hash, c.id, day)))
            if cohort is not None and utc(cohort.observed_at) <= now:
                data = json.loads(cohort.payload_json)
                status = "legacy_overcap" if data.get("blocked_legacy_overcap") else "frozen"
                members = len(data["members"])
        if inspector.has_table(storage.TournamentDecision.__tablename__):
            rows = list(
                session.scalars(
                    select(storage.TournamentDecision)
                    .where(
                        storage.TournamentDecision.day == day,
                        storage.TournamentDecision.family == c.family,
                        storage.TournamentDecision.observed_at <= now,
                    )
                    .limit(settings().api_record_limit + 1)
                )
            )
            if len(rows) > settings().api_record_limit:
                return "record_limit", members, None
            for row in rows:
                payload = json.loads(row.payload_json)
                if (
                    payload.get("report_hash") == report_hash
                    and payload.get("config_id") == c.id
                    and row.arm in counts
                ):
                    counts[row.arm] += 1
    return status, members, counts


def build(engine: Engine, now: datetime) -> Snapshot:
    now = aware(now)
    s, cal, frozen = settings(), calendar(), report.load()
    expected = settled_day(now)
    universe = tuple(frozen["tickers"])
    names = tuple(t for t in universe if t != "SPY")
    # The captured observation timestamp alone does not establish finality.
    bars = tuple(
        b
        for b in observed_bars(engine, now)
        if b.ticker in universe
        and expected is not None
        and b.day <= expected
        and is_session_day(b.day, cal) is True
    )
    latest = max((b.day for b in bars if b.ticker == "SPY" and b.valid), default=None)
    covered = len({b.ticker for b in bars if b.day == expected and b.valid})
    p = prepare(tuple(b for b in bars if b.day <= latest)) if latest is not None else None
    next_day = next_session_after(latest, cal) if latest else None
    deadline = (
        datetime.combine(next_day, cal.regular_open, ZoneInfo(cal.timezone)).astimezone(UTC)
        if next_day
        else None
    )
    deadline_open = deadline is not None and now < deadline
    families = []
    for w in frozen["winners"]:
        if not w["paper_enabled"]:
            continue
        c = Config(**w["config"])
        counts = (
            reasons(p, c, names, latest)
            if p is not None and latest
            else (dict.fromkeys(REASONS, 0) | {"current_bar_missing": len(names)})
        )
        cohort_status, members, arms = publication(engine, now, latest, c, frozen["report_hash"])
        families.append(Family(c.family, c.id, counts, cohort_status, members, arms))
    history = sum(
        sum(f.reasons[k] for k in REASONS if k not in ("current_bar_missing", "history_missing"))
        for f in families
    )
    eligible_count = sum(f.reasons["eligible"] for f in families)
    if expected is None:
        status = "calendar_unknown"
    elif latest is None:
        status = "no_bars"
    elif latest < expected:
        status = "stale_bars"
    elif latest < s.prospective_start:
        status = "waiting_start"
    elif not families:
        status = "no_experiment"
    elif any(f.cohort_status in ("legacy_overcap", "record_limit") for f in families):
        status = "publication_unavailable"
    elif any(f.cohort_status == "frozen" for f in families):
        status = "cohort_recorded"
    elif not history:
        status = "insufficient_history"
    elif not eligible_count:
        status = (
            "partial_coverage"
            if covered < len(universe)
            or any(
                f.reasons["history_missing"]
                or f.reasons["feature_unavailable"]
                or f.reasons["volume_unavailable"]
                for f in families
            )
            else "no_setup"
        )
    elif not deadline_open:
        status = "deadline_missed"
    else:
        status = "eligible_unpublished"
    return Snapshot(
        now,
        status,
        expected,
        latest,
        covered,
        len(universe),
        deadline,
        deadline_open,
        tuple(families),
    )


def aggregate(snapshot: Snapshot) -> dict[str, object]:
    return {
        "status": snapshot.status,
        "at": snapshot.at.isoformat(),
        "expected_day": str(snapshot.expected_day) if snapshot.expected_day else None,
        "latest_day": str(snapshot.latest_day) if snapshot.latest_day else None,
        "covered": snapshot.covered,
        "expected": snapshot.expected,
        "history_ready": sum(
            sum(
                f.reasons[k] for k in REASONS if k not in ("current_bar_missing", "history_missing")
            )
            for f in snapshot.families
        ),
        "eligible": sum(f.reasons["eligible"] for f in snapshot.families),
        "families": len(snapshot.families),
    }


def public_summary() -> dict[str, object]:
    # Deliberate allow-list: private reasons, tickers, payloads and errors stay out.
    keys = (
        "status",
        "at",
        "expected_day",
        "latest_day",
        "covered",
        "expected",
        "history_ready",
        "eligible",
        "families",
    )
    return {k: STATE[k] for k in keys if k in STATE}


def _single(engine: Engine, now: datetime) -> Snapshot | None:
    if not _LOCK.acquire(blocking=False):
        return None
    try:
        return build(engine, now)
    finally:
        _LOCK.release()


async def run(engine: Engine, now: datetime) -> None:
    global _LAST_CHECK
    now = aware(now)
    if _LAST_CHECK is not None and now - _LAST_CHECK < timedelta(
        seconds=settings().monitor_seconds
    ):
        return
    _LAST_CHECK = now
    try:
        snapshot = await asyncio.wait_for(
            asyncio.to_thread(_single, engine, now), timeout=settings().monitor_timeout_seconds
        )
        if snapshot is not None:
            STATE.clear()
            STATE.update(aggregate(snapshot))
    except Exception:
        # A timed-out thread is read-only and cannot later publish a stale success.
        STATE.clear()
        STATE.update(status="failed", at=now.isoformat())
        _logger.exception("research readiness unavailable; existing desk preserved")


def view(engine: Engine, now: datetime) -> dict[str, Any] | None:
    # Private template serialization adapter. No source prices or vendor access.
    try:
        result = build(engine, now)
        tr = ZoneInfo("Europe/Istanbul")
        return asdict(result) | {
            "at_tr": result.at.astimezone(tr).strftime("%d.%m.%Y %H:%M"),
            "deadline_tr": result.deadline.astimezone(tr).strftime("%d.%m.%Y %H:%M")
            if result.deadline
            else None,
        }
    except Exception:
        _logger.exception("private research readiness unavailable; existing report preserved")
        return None
