"""Five fixed gate ablations on shared reference units; no trading or fitted winner."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from functools import cache
from typing import Literal
from zoneinfo import ZoneInfo

from webapp.news_research.config import implementation_hash, profile_hash, settings
from webapp.opening.model import settings as opening_settings

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings

Arm = Literal["A", "B", "C", "D", "E"]
ARMS: tuple[Arm, ...] = ("A", "B", "C", "D", "E")
_ET = ZoneInfo("America/New_York")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Opportunity:
    key: str
    ticker: str
    day: date
    at: datetime
    rs: bool | None
    news: bool | None
    base: bool | None = True
    missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _utc(self.at))
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        if not self.key or not self.ticker or self.at.astimezone(_ET).date() != self.day:
            raise ValueError("opportunity identity/session mismatch")
        if any(type(x) is not bool for x in (self.rs, self.news, self.base) if x is not None):
            raise ValueError("gate values must be true, false or unknown")


@dataclass(frozen=True)
class Price:
    ticker: str
    at: datetime
    known_at: datetime
    value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", _utc(self.at))
        object.__setattr__(self, "known_at", _utc(self.known_at))
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        if (
            not self.ticker
            or isinstance(self.value, bool)
            or not math.isfinite(self.value)
            or self.value <= 0
        ):
            raise ValueError("price must be finite and positive")
        if self.known_at < self.at:
            raise ValueError("price cannot be known before its source time")


@dataclass(frozen=True)
class Dataset:
    opportunities: tuple[Opportunity, ...]
    prices: tuple[Price, ...]
    sessions: tuple[date, ...]
    identity: str
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of))
        if not self.identity or len({x.key for x in self.opportunities}) != len(self.opportunities):
            raise ValueError("dataset identity missing or duplicate opportunity keys")
        if len(set(self.sessions)) != len(self.sessions):
            raise ValueError("duplicate dataset sessions")
        if any(day > self.as_of.astimezone(_ET).date() for day in self.sessions):
            raise ValueError("future session cannot count as observed")


def _and(*values: bool | None) -> bool | None:
    return False if False in values else None if None in values else True


def _or(a: bool | None, b: bool | None) -> bool | None:
    return True if a is True or b is True else None if a is None or b is None else False


def eligibility(opportunity: Opportunity, arm: Arm) -> bool | None:
    if arm == "A":
        return opportunity.base
    if arm == "B":
        return _and(opportunity.base, opportunity.rs)
    if arm == "C":
        return _and(opportunity.base, opportunity.news)
    if arm == "D":
        return _and(opportunity.base, opportunity.rs, opportunity.news)
    if arm == "E":
        return _and(opportunity.base, _or(opportunity.rs, opportunity.news))
    raise ValueError("unknown experiment arm")


@cache
def _bounds(day: date) -> tuple[datetime, datetime] | None:
    cal = load_live_alpha_settings().calendar
    if is_session_day(day, cal) is not True:
        return None
    return (
        datetime.combine(day, cal.regular_open, _ET).astimezone(UTC),
        datetime.combine(day, close_time(day, cal), _ET).astimezone(UTC),
    )


def roster(dataset: Dataset) -> tuple[Opportunity, ...]:
    """First non-false base per symbol, then fixed chronological slots; no backfill."""
    first: dict[tuple[date, str], Opportunity] = {}
    for item in sorted(dataset.opportunities, key=lambda p: (p.at, p.ticker, p.key)):
        bounds = _bounds(item.day)
        if item.base is False or item.at > dataset.as_of or item.day not in dataset.sessions:
            continue
        if (
            bounds is None
            or not bounds[0] + timedelta(minutes=opening_settings().entry_delay_minutes)
            <= item.at
            < bounds[1]
        ):
            continue
        first.setdefault((item.day, item.ticker), item)
    chosen: list[Opportunity] = []
    counts: dict[date, int] = {}
    for item in sorted(first.values(), key=lambda p: (p.at, p.ticker, p.key)):
        if counts.get(item.day, 0) < settings().max_units_per_day:
            chosen.append(item)
            counts[item.day] = counts.get(item.day, 0) + 1
    return tuple(chosen)


@dataclass(frozen=True)
class Unit:
    key: str
    ticker: str
    day: date
    entry_at: datetime | None
    exit_at: datetime | None
    entry_known_at: datetime | None
    exit_known_at: datetime | None
    quantity: int | None
    entry_debit: float | None
    gross: float | None
    cost: float | None
    net: float | None
    spy_net: float | None
    excess: float | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class Slot:
    arm: Arm
    key: str
    ticker: str
    day: date
    eligible: bool | None
    net: float | None
    excess: float | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class Day:
    arm: Arm
    day: date
    selected: int
    ineligible: int
    unknown: int
    unpriced: int
    net: float | None
    excess: float | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class ArmSummary:
    arm: Arm
    selected: int
    ineligible: int
    unknown: int
    priced: int
    complete_days: int
    incomplete_days: int
    net_complete_days: float | None
    excess_complete_days: float | None


@dataclass(frozen=True)
class Pair:
    day: date
    e_net: float
    b_net: float
    e_excess: float
    b_excess: float
    difference_net: float
    difference_excess: float


@dataclass(frozen=True)
class Stress:
    slippage_bps_per_side: float
    units: tuple[Unit, ...]
    slots: tuple[Slot, ...]
    days: tuple[Day, ...]
    arms: tuple[ArmSummary, ...]
    paired_days: tuple[Pair, ...]
    complete_common_sessions: int
    total_paired_excess: float | None
    mean_daily_paired_excess: float | None


@dataclass(frozen=True)
class Report:
    version: str
    profile_hash: str
    identity: str
    as_of: datetime
    status: str
    session_count: int
    roster_count: int
    news_only_count: int
    roster: tuple[Opportunity, ...]
    base: Stress
    stresses: tuple[Stress, ...]
    inference: str = "NOT_PERFORMED"
    winner: None = None
    implementation_hash: str = ""


def _mark(
    prices: tuple[Price, ...], ticker: str, target: datetime, day: date, asof: datetime
) -> tuple[Price | None, str]:
    tolerance = timedelta(minutes=settings().mark_tolerance_minutes)
    bounds = _bounds(day)
    if bounds is None:
        return None, "unknown_calendar"
    candidates = sorted(
        (
            p
            for p in prices
            if p.ticker == ticker
            and target < p.at <= target + tolerance
            and p.known_at <= min(asof, target + tolerance)
            and bounds[0] <= p.at <= bounds[1]
        ),
        key=lambda p: (p.known_at, p.at),
    )
    if not candidates:
        return None, "missing_observed_price"
    first = candidates[0]
    conflicting = {
        p.value for p in prices if p.ticker == ticker and p.at == first.at and p.known_at <= asof
    }
    return (None, "conflicting_price_versions") if len(conflicting) > 1 else (first, "")


def _spy(
    prices: tuple[Price, ...], at: datetime, day: date, asof: datetime
) -> tuple[Price | None, str]:
    tolerance = timedelta(minutes=settings().mark_tolerance_minutes)
    matches = [
        p
        for p in prices
        if p.ticker == "SPY" and p.at == at and p.known_at <= min(asof, at + tolerance)
    ]
    if not matches:
        return None, "missing_exact_time_spy"
    if len({p.value for p in matches}) > 1:
        return None, "conflicting_spy_versions"
    return min(matches, key=lambda p: p.known_at), ""


def _unit(item: Opportunity, dataset: Dataset, bps: float) -> Unit:
    def missing(reason: str) -> Unit:
        return Unit(
            item.key,
            item.ticker,
            item.day,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (reason,),
        )

    entry, error = _mark(
        dataset.prices,
        item.ticker,
        item.at + timedelta(minutes=settings().entry_delay_minutes),
        item.day,
        dataset.as_of,
    )
    if entry is None:
        return missing("entry_" + error)
    # Holding starts when the entry observation became known, never at a backdated source time.
    exit_price, error = _mark(
        dataset.prices,
        item.ticker,
        entry.known_at + timedelta(minutes=settings().holding_minutes),
        item.day,
        dataset.as_of,
    )
    if exit_price is None:
        return missing("exit_" + error)
    spy_entry, error = _spy(dataset.prices, entry.at, item.day, dataset.as_of)
    if spy_entry is None:
        return missing("entry_" + error)
    spy_exit, error = _spy(dataset.prices, exit_price.at, item.day, dataset.as_of)
    if spy_exit is None:
        return missing("exit_" + error)
    rate = Decimal(str(bps)) / Decimal(10_000)
    opening, closing = Decimal(str(entry.value)), Decimal(str(exit_price.value))
    debit_per_share = opening * (1 + rate)
    quantity = int(
        (Decimal(str(settings().notional_usd)) / debit_per_share).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if quantity <= 0:
        return missing("no_affordable_whole_share")
    debit = quantity * debit_per_share
    gross = quantity * (closing - opening)
    cost = quantity * (opening + closing) * rate
    net = gross - cost
    benchmark = debit * (
        Decimal(str(spy_exit.value)) * (1 - rate) / (Decimal(str(spy_entry.value)) * (1 + rate)) - 1
    )
    if not all(
        math.isfinite(float(value))
        for value in (debit, gross, cost, net, benchmark, net - benchmark)
    ):
        return missing("nonfinite_reference_arithmetic")
    return Unit(
        item.key,
        item.ticker,
        item.day,
        entry.at,
        exit_price.at,
        entry.known_at,
        exit_price.known_at,
        quantity,
        float(debit),
        float(gross),
        float(cost),
        float(net),
        float(benchmark),
        float(net - benchmark),
        (),
    )


def _run(dataset: Dataset, selected: tuple[Opportunity, ...], bps: float) -> Stress:
    units = tuple(_unit(item, dataset, bps) for item in selected)
    by_key = {u.key: u for u in units}
    slots: list[Slot] = []
    for arm in ARMS:
        for item in selected:
            state, unit = eligibility(item, arm), by_key[item.key]
            slot_errors = (
                ("unknown_gate", *item.missing) if state is None else unit.missing if state else ()
            )
            slots.append(
                Slot(
                    arm,
                    item.key,
                    item.ticker,
                    item.day,
                    state,
                    unit.net if state is True else 0.0 if state is False else None,
                    unit.excess if state is True else 0.0 if state is False else None,
                    slot_errors,
                )
            )
    days: list[Day] = []
    for day in sorted(dataset.sessions):
        bounds = _bounds(day)
        for arm in ARMS:
            group = [s for s in slots if s.day == day and s.arm == arm]
            errors = sorted({error for s in group for error in s.missing})
            if bounds is None:
                errors.append("unknown_calendar")
            elif dataset.as_of < bounds[1]:
                errors.append("session_not_finished")
            days.append(
                Day(
                    arm,
                    day,
                    sum(s.eligible is True for s in group),
                    sum(s.eligible is False for s in group),
                    sum(s.eligible is None for s in group),
                    sum(s.eligible is True and s.net is None for s in group),
                    None if errors else sum(s.net or 0.0 for s in group),
                    None if errors else sum(s.excess or 0.0 for s in group),
                    tuple(errors),
                )
            )
    summaries = []
    for arm in ARMS:
        arm_slots, arm_days = [s for s in slots if s.arm == arm], [d for d in days if d.arm == arm]
        complete = [d for d in arm_days if d.net is not None and d.excess is not None]
        summaries.append(
            ArmSummary(
                arm,
                sum(s.eligible is True for s in arm_slots),
                sum(s.eligible is False for s in arm_slots),
                sum(s.eligible is None for s in arm_slots),
                sum(s.eligible is True and s.net is not None for s in arm_slots),
                len(complete),
                len(arm_days) - len(complete),
                sum(d.net or 0.0 for d in complete) if complete else None,
                sum(d.excess or 0.0 for d in complete) if complete else None,
            )
        )
    pairs = []
    common = 0
    for day in sorted(dataset.sessions):
        day_map = {d.arm: d for d in days if d.day == day}
        common += all(d.net is not None and d.excess is not None for d in day_map.values())
        e, b = day_map["E"], day_map["B"]
        if (
            e.net is not None
            and b.net is not None
            and e.excess is not None
            and b.excess is not None
        ):
            pairs.append(
                Pair(day, e.net, b.net, e.excess, b.excess, e.net - b.net, e.excess - b.excess)
            )
    total = sum(p.difference_excess for p in pairs) if pairs else None
    return Stress(
        bps,
        units,
        tuple(slots),
        tuple(days),
        tuple(summaries),
        tuple(pairs),
        common,
        total,
        total / len(pairs) if total is not None else None,
    )


def evaluate(dataset: Dataset) -> Report:
    chosen = roster(dataset)
    cfg = settings()
    base = _run(dataset, chosen, cfg.slippage_bps_per_side)
    status = (
        "INSUFFICIENT_EVIDENCE"
        if base.complete_common_sessions < cfg.minimum_common_sessions
        else "DESCRIPTIVE_READY_NOT_VALIDATED"
    )
    return Report(
        cfg.version,
        profile_hash(),
        dataset.identity,
        dataset.as_of,
        status,
        len(dataset.sessions),
        len(chosen),
        sum(o.base is True and o.news is True and o.rs is False for o in chosen),
        chosen,
        base,
        tuple(_run(dataset, chosen, value) for value in cfg.stress_slippage_bps_per_side),
        implementation_hash=implementation_hash(),
    )
