"""Bounded as-of first-entry reference coverage, with no advice or alpha estimate.

Stored JSON is parsed at this adapter boundary; the summary leaves as typed,
immutable records. Different horizons and policies are never pooled as trades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from uoa_detector.live_alpha.calendar import close_time, next_session_after
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.db import dumps
from webapp.opening.model import settings

if TYPE_CHECKING:
    from webapp.opening.view import Result


class Mark(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)
    status: Literal["complete", "missing_reference"]
    observed_at: datetime
    target_at: datetime
    directional_underlying_pct: float | None = Field(allow_inf_nan=False)
    directional_excess_pp: float | None = Field(allow_inf_nan=False)
    spy_pct: float | None = Field(allow_inf_nan=False)


@dataclass(frozen=True)
class Horizon:
    label: str
    observed: int
    matched: int
    positive: int
    sessions: int
    mean_pct: float | None
    mean_excess_pp: float | None
    pending: int
    awaiting: int
    partial: int
    missing: int
    invalid: int
    unknown_time: int


@dataclass(frozen=True)
class Summary:
    version: str
    entries: int
    sessions: int
    tickers: int
    excluded: int
    duplicates: int
    first_day: date | None
    last_day: date | None
    scope_day: date | None
    limit: int
    horizons: tuple[Horizon, ...]


def _target(day: date, entry: datetime, kind: str, count: int) -> datetime | None:
    if kind == "minutes":
        return entry + timedelta(minutes=count)
    cal = load_live_alpha_settings().calendar
    for _ in range(count):
        next_day = next_session_after(day, cal)
        if next_day is None:
            return None
        day = next_day
    if day.year not in cal.years:
        return None
    return datetime.combine(day, close_time(day, cal), ZoneInfo(cal.timezone))


def _mean(values: list[float]) -> float | None:
    # Dividing before summing also avoids a sum overflow on finite inputs.
    return math.fsum(v / len(values) for v in values) if values else None


def build(results: tuple[Result, ...], now: datetime, scope_day: date | None = None) -> Summary:
    if now.tzinfo is None:
        raise ValueError("measurement time must be timezone-aware")
    cfg = settings()
    eligible = {}
    excluded = duplicates = 0
    for row in results:
        if (
            not row.key
            or row.profile_hash != cfg.profile_hash
            or row.at.tzinfo is None
            or row.at > now
            or row.day is None
            or (scope_day is not None and row.day != scope_day)
        ):
            excluded += 1
            continue
        if row.key in eligible:
            duplicates += 1
            continue
        eligible[row.key] = row
    selected = sorted(eligible.values(), key=lambda r: (r.at, r.key), reverse=True)[
        : cfg.history_limit
    ]
    days = {row.day for row in selected if row.day is not None}
    horizons = []
    targets = [("minutes", m, f"+{m} dk") for m in cfg.intraday_minutes] + [
        ("sessions", d, "Bugün kapanış" if d == 0 else f"+{d} seans kapanış")
        for d in cfg.close_sessions
    ]
    for kind, count, label in targets:
        pending = awaiting = partial = missing = invalid = unknown = 0
        returns: list[float] = []
        excess: list[float] = []
        observed_days: set[date] = set()
        for row in selected:
            assert row.day is not None
            target = _target(row.day, row.at, kind, count)
            if target is None:
                unknown += 1
                continue
            if now < target:
                pending += 1
                continue
            raw = dict(row.marks).get(label)
            if raw is None:
                awaiting += 1
                continue
            try:
                mark = Mark.model_validate_json(dumps(raw))
            except (ValidationError, ValueError, TypeError):
                invalid += 1
                continue
            if mark.observed_at.tzinfo is None or mark.target_at.tzinfo is None:
                invalid += 1
                continue
            if mark.observed_at > now:
                awaiting += 1
                continue
            if mark.target_at != target or mark.observed_at < target:
                invalid += 1
                continue
            own, paired = mark.directional_underlying_pct, mark.directional_excess_pp
            if mark.status == "complete" and (
                own is None or paired is None or mark.spy_pct is None
            ):
                invalid += 1
                continue
            if own is None:
                missing += 1
                continue
            returns.append(own)
            observed_days.add(row.day)
            if mark.status == "complete" and paired is not None:
                excess.append(paired)
            else:
                partial += 1
        horizons.append(
            Horizon(
                label,
                len(returns),
                len(excess),
                sum(v > 0 for v in returns),
                len(observed_days),
                _mean(returns),
                _mean(excess),
                pending,
                awaiting,
                partial,
                missing,
                invalid,
                unknown,
            )
        )
    return Summary(
        cfg.version,
        len(selected),
        len(days),
        len({r.ticker for r in selected}),
        excluded,
        duplicates,
        min(days) if days else None,
        max(days) if days else None,
        scope_day,
        cfg.history_limit,
        tuple(horizons),
    )
