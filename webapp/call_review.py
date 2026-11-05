"""Descriptive first-call attribution. Never fits, selects or publishes a rule."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine

from webapp import signal_explanation
from webapp.board.db import session_factory
from webapp.call_research import progress, scorecard
from webapp.call_research.settings import settings
from webapp.opening import storage
from webapp.opening.model import Advice

ROLE_LABELS = {
    "uw_news_selected": "UW haber desteği",
    "relative_strength_selected": "Göreli güç",
    "pullback": "Geri çekilme",
    "bearish": "Düşüş planı",
    "unknown": "Destek yolu doğrulanamadı",
}


@dataclass(frozen=True)
class Case:
    key: str
    ticker: str
    day: date
    at: datetime
    role: str
    instrument: str
    verified: bool
    reasons: tuple[str, ...]
    metrics: tuple[progress.Metric, ...]
    price_move_atr: float | None = None
    relative_pp: float | None = None
    dominant_flow_pct: float | None = None
    news_vote: bool | None = None


@dataclass(frozen=True)
class Stats:
    horizon: str
    calls: int
    observed: int
    positive: int
    negative: int
    flat: int
    paired: int
    measured_sessions: int
    mean_pct: float | None
    matched_own_pct: float | None
    matched_spy_pct: float | None
    matched_excess_pp: float | None
    states: dict[str, int]


@dataclass(frozen=True)
class Group:
    axis: str
    key: str
    calls: int
    sessions: int
    metrics: tuple[Stats, ...]


@dataclass(frozen=True)
class Report:
    as_of: datetime
    status: str
    total: int | None
    limit: int
    limited: bool
    verified: int
    sessions: int
    cases: tuple[Case, ...]
    metrics: tuple[Stats, ...]
    groups: tuple[Group, ...]
    method: str = "legacy_first_call_review_v1"
    inference: str = "NOT_PERFORMED"
    winner: str | None = None


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _mean(values: list[float]) -> float | None:
    return math.fsum(v / len(values) for v in values) if values else None


def summarize(cases: tuple[Case, ...]) -> tuple[Stats, ...]:
    """Matched own and benchmark use identical records; missing never becomes zero."""
    labels = tuple(dict.fromkeys(m.label for c in cases for m in c.metrics))
    output = []
    for label in labels:
        found = [(c, m) for c in cases for m in c.metrics if m.label == label]
        own = [(c, m.value_pct) for c, m in found if c.verified and _finite(m.value_pct)]
        paired = [
            (m.value_pct, m.excess_pp)
            for c, m in found
            if c.verified and _finite(m.value_pct) and _finite(m.excess_pp)
        ]
        values = [v for _, v in own if v is not None]
        matched = [(a, e) for a, e in paired if a is not None and e is not None]
        output.append(
            Stats(
                label,
                len(cases),
                len(values),
                sum(v > 0 for v in values),
                sum(v < 0 for v in values),
                sum(v == 0 for v in values),
                len(matched),
                len({c.day for c, _ in own}),
                _mean(values),
                _mean([a for a, _ in matched]),
                _mean([a - e for a, e in matched]),
                _mean([e for _, e in matched]),
                dict(Counter(m.status if c.verified else "unverified" for c, m in found)),
            )
        )
    return tuple(output)


def build(engine: Engine, now: datetime) -> Report:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware review clock required")
    limit = settings().display_limit
    required = (*storage.TABLES,)
    names = set(inspect(engine).get_table_names())
    if any(t.__tablename__ not in names for t in required):
        return Report(now, "STORAGE_UNAVAILABLE", None, limit, False, 0, 0, (), (), ())
    briefs = scorecard.listing(engine, now)
    listed = progress.listing(engine, briefs, now)
    keys = tuple(r.brief.key for r in listed.rows)
    explanations = signal_explanation.build(engine, now, keys=keys)
    with session_factory(engine)() as session:
        total = (
            session.scalar(
                select(func.count(func.distinct(storage.OpeningDecision.key)))
                .join(
                    storage.OpeningPrimary,
                    storage.OpeningPrimary.decision_key == storage.OpeningDecision.key,
                )
                .where(
                    storage.OpeningPrimary.kind == "entry",
                    storage.OpeningPrimary.published_at <= now,
                    storage.OpeningDecision.published_at <= now,
                )
            )
            or 0
        )
        saved = (
            session.scalars(
                select(storage.OpeningDecision).where(storage.OpeningDecision.key.in_(keys))
            ).all()
            if keys
            else []
        )
    advice = {}
    for record in saved:
        try:
            advice[record.key] = Advice.model_validate_json(record.payload_json)
        except (ValueError, TypeError):
            continue
    cases = []
    for row in listed.rows:
        x, a = explanations.get(row.brief.key), advice.get(row.brief.key)
        valid = bool(
            row.first
            and row.source_valid
            and x
            and x.source_valid
            and a
            and x.first_issued_at == row.brief.at
            and x.statement_at == row.brief.at
        )
        role = x.support.role if valid and x and x.support.role in ROLE_LABELS else "unknown"
        metrics = (
            row.metrics
            if valid
            else tuple(progress.Metric(m.label, "unverified") for m in row.metrics)
        )
        cases.append(
            Case(
                row.brief.key,
                row.brief.ticker,
                row.brief.day,
                row.brief.at,
                role,
                a.instrument if valid and a else "unknown",
                valid,
                x.reasons if valid and x else (),
                metrics,
                x.price.move_atr if valid and x else None,
                x.price.relative_pp if valid and x else None,
                x.flow.dominant_share_pct if valid and x else None,
                x.news.qualifying_vote if valid and x else None,
            )
        )
    selected = tuple(cases)
    groups = []
    # Fixed axes, sorted identifiers, never ranked by performance.
    for axis in ("role", "instrument", "day"):
        for value in sorted({str(getattr(c, axis)) for c in selected}):
            members = tuple(c for c in selected if str(getattr(c, axis)) == value)
            groups.append(
                Group(axis, value, len(members), len({c.day for c in members}), summarize(members))
            )
    limited = total > len(selected)
    return Report(
        now,
        "LIMITED" if limited else "DESCRIPTIVE" if selected else "EMPTY",
        total,
        limit,
        limited,
        sum(c.verified for c in selected),
        len({c.day for c in selected}),
        selected,
        summarize(selected),
        tuple(groups),
    )
