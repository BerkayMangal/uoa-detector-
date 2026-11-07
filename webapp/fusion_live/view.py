"""Bounded read-only forward reporting; JSON decoding is the stored-result adapter.

No collection, model call, fitting, schema creation or result finalization runs
from this projection. A completed session counts only in its original cohort.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from webapp.board.db import session_factory
from webapp.fusion_live import config, scoring, storage
from webapp.shadow_capture import config as source_config
from webapp.shadow_capture.model import aware, digest


@dataclass(frozen=True)
class HistoryArm:
    arm: str
    label: str
    sessions: int
    net_usd: float | None
    average_usd: float | None
    difference_from_base_usd: float | None


@dataclass(frozen=True)
class Progress:
    horizon: str
    label: str
    complete_sessions: int
    required_sessions: int
    arms: tuple[HistoryArm, ...]


@dataclass(frozen=True)
class Report:
    as_of: datetime
    day: date
    status: str
    reason: str
    method: str
    identity: scoring.Identity
    source_identity: scoring.SourceIdentity
    activation_key: str | None
    activation_at: datetime | None
    candidate_count: int | None
    source_candidates: int | None
    ignored_candidates: int
    invalid_candidates: int
    universe_verified: bool
    session_closed: bool
    rows: tuple[scoring.Candidate, ...]
    ignored: tuple[scoring.IgnoredCandidate, ...]
    horizons: tuple[scoring.Horizon, ...]
    stored_days: int
    invalid_days: int
    ignored_days: int
    complete_common_sessions: int
    required_complete_sessions: int
    readiness: str
    progress: tuple[Progress, ...]
    limited: bool = False
    configured_enabled: bool = True
    inference: str = "NOT_PERFORMED"
    winner: None = None


@dataclass(frozen=True)
class _StoredHorizon:
    horizon: str
    nets: tuple[float, ...]


@dataclass(frozen=True)
class _StoredDay:
    day: date
    horizons: tuple[_StoredHorizon, ...]


def _mapping(value: object) -> dict[str, Any]:
    # Any is restricted to persisted immutable JSON, before explicit shape checks.
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("result_mapping_invalid")
    return value


def _items(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("result_sequence_invalid")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("result_clock_invalid")
    return aware(datetime.fromisoformat(value))


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("result_number_invalid")
    return float(value)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("result_count_invalid")
    return value


def _near(left: float, right: object) -> None:
    if not math.isclose(left, _number(right), rel_tol=1e-10, abs_tol=1e-7):
        raise ValueError("result_accounting_invalid")


def _references(row: dict[str, Any], horizon: str, as_of: datetime) -> float:
    phases = _items(row.get("phases"))
    if len(phases) != 3:
        raise ValueError("result_reference_roster_invalid")
    by_phase = {_mapping(phase).get("phase"): _mapping(phase) for phase in phases}
    if set(by_phase) != {"entry", "m15", "m60"}:
        raise ValueError("result_reference_phases_invalid")
    entry, end = by_phase["entry"], by_phase[horizon]
    if entry.get("state") != "observed" or end.get("state") != "observed":
        raise ValueError("result_reference_missing")
    candidate_at = _time(row.get("available_at"))
    close = scoring.session_close(candidate_at.astimezone(scoring.ET).date())
    if close is None:
        raise ValueError("result_reference_calendar_invalid")
    expected_entry = candidate_at + timedelta(seconds=source_config.settings().entry_delay_seconds)
    entry_known = _time(entry.get("confirmed_at"))
    expected_end = entry_known + timedelta(minutes=int(horizon[1:]))
    for phase, due in ((entry, expected_entry), (end, expected_end)):
        known = _time(phase.get("confirmed_at"))
        deadline = min(due + timedelta(seconds=source_config.settings().window_seconds), close)
        own_at, spy_at = _time(phase.get("own_trade_at")), _time(phase.get("spy_trade_at"))
        if (
            _time(phase.get("target_at")) != due
            or _time(phase.get("deadline")) != deadline
            or not due <= known <= deadline
            or known > as_of
            or not due <= own_at <= known
            or not due <= spy_at <= known
            or abs((own_at - spy_at).total_seconds()) > source_config.settings().pair_skew_seconds
        ):
            raise ValueError("result_reference_clock_invalid")
    opening, closing = _number(entry.get("own_price")), _number(end.get("own_price"))
    if min(opening, closing, _number(entry.get("spy_price")), _number(end.get("spy_price"))) <= 0:
        raise ValueError("result_reference_price_invalid")
    rate = Decimal(str(source_config.settings().slippage_bps_per_side)) / Decimal(10000)
    first, last = Decimal(str(opening)), Decimal(str(closing))
    quantity = int(
        (
            Decimal(str(source_config.settings().reference_usd)) / (first * (1 + rate))
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    if quantity < 1 or _count(end.get("reference_quantity")) != quantity:
        raise ValueError("result_reference_size_invalid")
    net = float(quantity * (last * (1 - rate) - first * (1 + rate)))
    _near(net, end.get("model_net_usd"))
    return net


def _common_panel(
    body: dict[str, Any], horizon: dict[str, Any], active: storage.Record
) -> _StoredHorizon:
    name = horizon.get("horizon")
    if name not in scoring.HORIZONS or not isinstance(name, str):
        raise ValueError("result_horizon_invalid")
    size = _count(body.get("candidates"))
    if (
        not 1 <= size <= config.settings().candidate_limit
        or body.get("universe_verified") is not True
        or body.get("limited") is not False
        or _count(body.get("invalid_candidates")) != 0
        or horizon.get("reference_complete") is not True
        or _count(horizon.get("candidates")) != size
        or _count(horizon.get("reference_observed")) != size
    ):
        raise ValueError("result_universe_incomplete")
    rows = _items(body.get("rows"))
    if len(rows) != size or active.available_at is None:
        raise ValueError("result_roster_incomplete")
    expected: dict[str, list[float]] = {arm: [] for arm in config.settings().arms}
    accepts = {arm: 0 for arm in config.settings().arms}
    skips = {arm: 0 for arm in config.settings().arms}
    keys: set[str] = set()
    slots: set[int] = set()
    day = date.fromisoformat(str(body.get("day")))
    closing = scoring.session_close(day)
    if closing is None:
        raise ValueError("result_calendar_invalid")
    for raw_row in rows:
        row = _mapping(raw_row)
        key, checksum, slot = row.get("key"), row.get("checksum"), row.get("slot")
        if (
            not isinstance(key, str)
            or not re.fullmatch("[a-f0-9]{64}", key)
            or key in keys
            or not isinstance(checksum, str)
            or not re.fullmatch("[a-f0-9]{64}", checksum)
            or type(slot) is not int
            or not 1 <= slot <= config.settings().candidate_limit
            or slot in slots
        ):
            raise ValueError("result_candidate_identity_invalid")
        keys.add(key)
        slots.add(slot)
        started, source, available, cutoff = (
            _time(row.get(field))
            for field in ("source_started_at", "source_at", "available_at", "cutoff")
        )
        if (
            not active.available_at <= started <= source <= available < cutoff < closing
            or source.astimezone(scoring.ET).date() != day
            or cutoff
            != available
            + timedelta(
                seconds=source_config.settings().entry_delay_seconds
                - config.settings().entry_lead_seconds
            )
        ):
            raise ValueError("result_source_clock_invalid")
        reference = _references(row, name, _time(body.get("as_of")))
        decisions = _items(row.get("decisions"))
        if len(decisions) != len(config.settings().arms):
            raise ValueError("result_arm_roster_invalid")
        seen = set()
        for raw_decision in decisions:
            decision = _mapping(raw_decision)
            arm = decision.get("arm")
            if arm not in expected or arm in seen:
                raise ValueError("result_arm_identity_invalid")
            seen.add(arm)
            state = decision.get("state")
            at, known = _time(decision.get("at")), _time(decision.get("available_at"))
            used = tuple(_time(value) for value in _items(decision.get("used_at")))
            if (
                state not in ("PAPER_ACCEPT", "PAPER_SKIP")
                or decision.get("verified") is not True
                or decision.get("timely") is not True
                or _time(decision.get("cutoff")) != cutoff
                or not available <= at <= known <= cutoff
                or not used
                or any(t > at for t in used)
            ):
                raise ValueError("result_decision_clock_or_state_invalid")
            net = reference if state == "PAPER_ACCEPT" else 0.0
            _near(net, decision.get(name + "_net_usd"))
            expected[arm].append(net)
            accepts[arm] += state == "PAPER_ACCEPT"
            skips[arm] += state == "PAPER_SKIP"
    metrics = _items(horizon.get("arms"))
    if len(metrics) != len(config.settings().arms):
        raise ValueError("result_metric_roster_invalid")
    by_arm = {_mapping(value).get("arm"): _mapping(value) for value in metrics}
    if set(by_arm) != set(expected):
        raise ValueError("result_metric_identity_invalid")
    totals = []
    for arm in config.settings().arms:
        metric = by_arm[arm]
        if (
            metric.get("complete") is not True
            or _count(metric.get("candidates")) != size
            or _count(metric.get("accepts")) != accepts[arm]
            or _count(metric.get("skips")) != skips[arm]
            or _count(metric.get("unknown")) != 0
        ):
            raise ValueError("result_metric_counts_invalid")
        net = math.fsum(expected[arm])
        _near(net, metric.get("net_usd"))
        totals.append(net)
    return _StoredHorizon(name, tuple(totals))


def _stored_day(
    record: storage.Record, active: storage.Record, current: scoring.Daily
) -> tuple[_StoredDay | None, str]:
    try:
        payload = record.payload
        if (
            record.kind != "day_result"
            or record.day is None
            or record.candidate_key != active.key
            or record.arm is not None
            or record.key
            != digest((config.settings().version, active.key, record.day.isoformat(), "day_result"))
            or payload.get("identity") != asdict(current.identity)
            or payload.get("activation_key") != active.key
            or record.available_at is None
            or active.available_at is None
        ):
            raise ValueError("result_record_identity_invalid")
        body = _mapping(payload.get("result"))
        if body.get("source_identity") != asdict(current.source_identity):
            return None, "outside_source_cohort"
        closing = scoring.session_close(record.day)
        if (
            body.get("identity") != asdict(current.identity)
            or body.get("activation_key") != active.key
            or body.get("method") != current.method
            or body.get("source_method") != current.source_method
            or body.get("day") != record.day.isoformat()
            or closing is None
            or body.get("session_closed") is not True
            or record.available_at > current.as_of
            or not closing <= _time(body.get("as_of")) <= record.at <= record.available_at
            or _time(body.get("activation_at")) != active.available_at
        ):
            raise ValueError("result_record_clock_invalid")
        values = _items(body.get("horizons"))
        by_name = {_mapping(value).get("horizon"): _mapping(value) for value in values}
        if len(values) != 2 or set(by_name) != set(scoring.HORIZONS):
            raise ValueError("result_horizon_roster_invalid")
        complete = tuple(
            _common_panel(body, by_name[name], active)
            for name in scoring.HORIZONS
            if by_name[name].get("common_complete") is True
        )
        return _StoredDay(record.day, complete), ""
    except (ValueError, TypeError, KeyError):
        return None, "result_unverified"


def _history(
    engine: Engine, daily: scoring.Daily
) -> tuple[tuple[Progress, ...], int, int, int, int, bool]:
    panels: dict[str, dict[date, tuple[float, ...]]] = {name: {} for name in scoring.HORIZONS}
    active = storage.active(engine, daily.as_of)
    invalid = ignored = stored_days = 0
    limited = False
    if active is not None:
        limit = config.settings().history_read_limit
        with session_factory(engine)() as session:
            keys = session.scalars(
                select(storage.FusionEvent.key)
                .where(
                    storage.FusionEvent.kind == "day_result",
                    storage.FusionEvent.candidate_key == active.key,
                    storage.FusionEvent.at <= daily.as_of,
                )
                .order_by(
                    storage.FusionEvent.day.desc(),
                    storage.FusionEvent.at.desc(),
                    storage.FusionEvent.key.desc(),
                )
                .limit(limit + 1)
            ).all()
        limited = len(keys) > limit
        days: set[date] = set()
        for key in keys[:limit]:
            record = storage.get(engine, key, daily.as_of)
            if record is None:
                invalid += 1
                continue
            result, reason = _stored_day(record, active, daily)
            if result is None:
                if reason == "outside_source_cohort":
                    ignored += 1
                else:
                    invalid += 1
                continue
            if result.day in days:
                invalid += 1
                continue
            days.add(result.day)
            stored_days += 1
            for value in result.horizons:
                panels[value.horizon][result.day] = value.nets
    progress = []
    for name in scoring.HORIZONS:
        values = tuple(panels[name].values())
        totals = tuple(
            math.fsum(value[n] for value in values) for n in range(len(config.settings().arms))
        )
        arms = tuple(
            HistoryArm(
                arm,
                scoring.ARM_LABELS[arm],
                len(values),
                total if values else None,
                total / len(values) if values else None,
                total - totals[0] if values else None,
            )
            for arm, total in zip(config.settings().arms, totals, strict=True)
        )
        progress.append(
            Progress(
                name,
                "+15 dakika" if name == "m15" else "+60 dakika",
                len(values),
                config.settings().minimum_complete_sessions,
                arms,
            )
        )
    common = len(set(panels["m15"]) & set(panels["m60"]))
    return tuple(progress), stored_days, invalid, ignored, common, limited


def build(engine: Engine, now: datetime, day: date | None = None) -> Report:
    at = aware(now)
    selected = day or at.astimezone(scoring.ET).date()
    daily = scoring.build_day(engine, selected, at)
    progress: tuple[Progress, ...] = ()
    stored_days = invalid = ignored = common = 0
    history_limited = False
    try:
        if storage.available(engine):
            progress, stored_days, invalid, ignored, common, history_limited = _history(
                engine, daily
            )
    except Exception:
        invalid += 1
    limited = daily.limited or history_limited
    ready = common >= config.settings().minimum_complete_sessions and not limited and invalid == 0
    enabled = config.enabled()
    return Report(
        at,
        selected,
        daily.status if enabled else "DISABLED",
        daily.reason if enabled else "Yeni motor kapalı; önceki kayıtlar ayrı okunuyor.",
        daily.method,
        daily.identity,
        daily.source_identity,
        daily.activation_key,
        daily.activation_at,
        None if daily.status in ("STORAGE_UNAVAILABLE", "NOT_ACTIVATED") else daily.candidates,
        daily.source_candidates,
        daily.ignored_candidates,
        daily.invalid_candidates,
        daily.universe_verified,
        daily.session_closed,
        daily.rows,
        daily.ignored,
        daily.horizons,
        stored_days,
        invalid,
        ignored,
        common,
        config.settings().minimum_complete_sessions,
        "READY_FOR_REVIEW" if ready else "INSUFFICIENT_COMPLETE_SESSIONS",
        progress,
        limited,
        enabled,
    )
