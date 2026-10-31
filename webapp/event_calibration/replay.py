"""Select first, label later. Independent observed stock-reference units only."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import product

from webapp.event_calibration.features import Sample
from webapp.event_calibration.model import Dataset, digest, settings
from webapp.event_calibration.series import Series


@dataclass(frozen=True)
class Candidate:
    arm: str
    ratio: float
    dominance: float
    percentile: float
    delay: int
    hold: int

    @property
    def key(self) -> str:
        return digest(
            (self.arm, self.ratio, self.dominance, self.percentile, self.delay, self.hold)
        )[:16]


def candidates() -> tuple[Candidate, ...]:
    c = settings()
    return tuple(
        Candidate("baseline", 0, 0, 0, delay, hold)
        for delay, hold in product(c.entry_delay_minutes, c.holding_minutes)
    ) + tuple(
        Candidate(arm, ratio, dominance, percentile, delay, hold)
        for arm, ratio, dominance, percentile, delay, hold in product(
            c.arms, c.ratios, c.dominance, c.percentiles, c.entry_delay_minutes, c.holding_minutes
        )
    )


def qualifies(s: Sample, c: Candidate) -> bool:
    if not s.decision.usable:
        return False
    if c.arm == "baseline":
        return s.decision.baseline
    if (
        s.ratio is None
        or s.percentile is None
        or s.dominance is None
        or not (s.ratio >= c.ratio and s.percentile >= c.percentile and s.dominance >= c.dominance)
    ):
        return False
    if c.arm == "normalized":
        return True
    if s.acceptance is None or s.acceptance <= 0:
        return False
    if c.arm == "normalized_price":
        return True
    if c.arm == "normalized_news":
        return s.decision.news is True
    if c.arm == "normalized_market":
        return s.market is not None and s.market > 0
    context = {
        "normalized_market": s.decision.market,
        "normalized_gamma": s.decision.gamma,
        "normalized_sector": s.decision.sector,
    }.get(c.arm)
    return context is not None and context.known_at <= s.decision.at and context.value > 0


@dataclass(frozen=True)
class Unit:
    key: str
    ticker: str
    day: date
    decision_at: datetime
    entry_at: datetime
    exit_at: datetime
    known_at: datetime
    entry: float
    exit: float
    net_pct: float
    spy_pct: float
    excess_pp: float
    risk_r: float | None


@dataclass(frozen=True)
class Replay:
    candidate: Candidate
    selected: int
    unresolved: int
    boundary_excluded: int
    units: tuple[Unit, ...]
    selected_days: tuple[date, ...] = ()


def replay(
    data: Dataset,
    samples: tuple[Sample, ...],
    candidate: Candidate,
    days: tuple[date, ...],
    bps: int,
    *,
    label_asof: datetime | None = None,
    index: Series | None = None,
) -> Replay:
    c = settings()
    series = index or Series.make(data)
    allowed = set(days)
    occupied: set[tuple[str, date]] = set()
    counts: Counter[date] = Counter()
    chosen: list[Sample] = []
    boundary = 0
    for s in sorted(samples, key=lambda p: (p.decision.at, p.decision.ticker, p.decision.key)):
        if s.day not in allowed or not qualifies(s, candidate):
            continue
        if (
            s.decision.at
            + timedelta(minutes=candidate.delay + candidate.hold + 2 * c.mark_tolerance_minutes)
            >= s.close
        ):
            boundary += 1
            continue
        identity = s.decision.ticker, s.day
        if identity in occupied or counts[s.day] >= c.max_units_per_day:
            continue
        occupied.add(identity)
        counts[s.day] += 1
        chosen.append(s)
    units = []
    slip = bps / 10000
    for s in chosen:
        decision = s.decision
        entry = series.after(
            decision.ticker, decision.at + timedelta(minutes=candidate.delay), strict=True
        )
        exit_ = (
            series.after(decision.ticker, entry.at + timedelta(minutes=candidate.hold))
            if entry
            else None
        )
        if not entry or not exit_ or exit_.at >= s.close:
            continue
        references = [
            p
            for t in (entry.at, exit_.at)
            if (p := series.exact.get(("SPY", t))) is not None
            and p.known_at <= p.at + timedelta(minutes=c.mark_tolerance_minutes)
        ]
        spy = {p.at: p for p in references}
        if entry.at not in spy or exit_.at not in spy:
            continue
        known_at = max(
            entry.known_at, exit_.known_at, spy[entry.at].known_at, spy[exit_.at].known_at
        )
        if label_asof is not None and known_at >= label_asof:
            continue
        own = (exit_.value * (1 - slip) / (entry.value * (1 + slip)) - 1) * 100
        ref = (spy[exit_.at].value * (1 - slip) / (spy[entry.at].value * (1 + slip)) - 1) * 100
        units.append(
            Unit(
                decision.key,
                decision.ticker,
                s.day,
                decision.at,
                entry.at,
                exit_.at,
                known_at,
                entry.value,
                exit_.value,
                own,
                ref,
                own - ref,
                own / (decision.risk_fraction * 100) if decision.risk_fraction else None,
            )
        )
    return Replay(
        candidate,
        len(chosen),
        len(chosen) - len(units),
        boundary,
        tuple(units),
        tuple(s.day for s in chosen),
    )
