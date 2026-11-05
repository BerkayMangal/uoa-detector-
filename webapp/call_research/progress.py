"""Bounded read-only call progress; recorded JSON is the sole untyped boundary."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab.db import digest
from webapp.board.db import session_factory
from webapp.call_research.scorecard import Brief, Scorecard
from webapp.live_alpha.store import LiveScan
from webapp.opening import measurement, storage
from webapp.opening.model import Advice, settings, utc
from webapp.opening.view import Result

LABELS = {
    "observed": "Ölçüldü",
    "pending": "Süre dolmadı",
    "awaiting": "Ölçüm bekleniyor",
    "partial": "SPY kaydı eksik",
    "missing": "Fiyat kaydı eksik",
    "invalid": "Kayıt geçersiz",
    "unknown_time": "Takvim bilinmiyor",
    "unverified": "Kaynak doğrulanamadı",
    "update": "Güncelleme",
}


@dataclass(frozen=True)
class Metric:
    label: str
    status: str
    value_pct: float | None = None
    excess_pp: float | None = None

    @property
    def text(self) -> str:
        return LABELS[self.status]


@dataclass(frozen=True)
class Row:
    brief: Brief
    canonical_key: str | None
    first: bool
    source_valid: bool
    metrics: tuple[Metric, ...]

    @property
    def intraday(self) -> tuple[Metric, ...]:
        return self.metrics[: len(settings().intraday_minutes)]


@dataclass(frozen=True)
class View:
    rows: tuple[Row, ...]
    first_entries: int
    sessions: int
    observed: int
    matched: int
    primary_label: str


def _normalize_marks(
    marks: tuple[tuple[str, dict[str, Any] | None], ...],
    intraday_minutes: tuple[int, ...],
    close_sessions: tuple[int, ...],
) -> tuple[tuple[str, dict[str, Any] | None], ...]:
    """Translate fixed writer IDs; aliases never select a last/better result."""
    aliases = {f"m{m}": f"+{m} dk" for m in intraday_minutes} | {
        f"c{d}": "Bugün kapanış" if d == 0 else f"+{d} seans kapanış"
        for d in close_sessions
    }
    grouped: dict[str, list[dict[str, Any] | None]] = defaultdict(list)
    for label, value in marks:
        grouped[aliases.get(label, label)].append(value)
    return tuple(
        (label, values[0] if len(values) == 1 else {"invalid": True})
        for label, values in grouped.items()
    )


def metrics(result: Result, now: datetime, blocked: str = "") -> tuple[Metric, ...]:
    cfg = settings()
    normalized = replace(
        result, marks=_normalize_marks(result.marks, cfg.intraday_minutes, cfg.close_sessions)
    )
    summary = measurement.build((normalized,), now)
    out = []
    for horizon in summary.horizons:
        if blocked:
            state = blocked
        elif horizon.invalid:
            state = "invalid"
        elif horizon.unknown_time:
            state = "unknown_time"
        elif horizon.pending:
            state = "pending"
        elif horizon.awaiting:
            state = "awaiting"
        elif horizon.missing:
            state = "missing"
        elif horizon.partial:
            state = "partial"
        elif horizon.matched:
            state = "observed"
        else:
            state = "unverified"
        out.append(
            Metric(
                horizon.label,
                state,
                horizon.mean_pct if state in ("observed", "partial") else None,
                horizon.mean_excess_pp if state == "observed" else None,
            )
        )
    return tuple(out)


def detail(card: Scorecard, now: datetime) -> tuple[Metric, ...]:
    result = Result(
        card.advice.ticker,
        card.at,
        card.advice.instrument,
        tuple(
            (label, mark.model_dump(mode="json") if mark else {"invalid": True})
            for label, mark in card.marks
        ),
        card.key,
        card.day,
        card.opening_profile,
    )
    blocked = (
        "unverified"
        if not card.source_valid or card.opening_profile != settings().profile_hash
        else "update"
        if not card.is_first_entry
        else ""
    )
    return metrics(result, now, blocked)


def listing(engine: Engine, briefs: tuple[Brief, ...], now: datetime) -> View:
    cfg = settings()
    primary_label = f"+{cfg.intraday_minutes[-1]} dk"
    if not briefs:
        return View((), 0, 0, 0, 0, primary_label)
    if len(briefs) > cfg.history_limit:
        raise ValueError("call progress display bound")
    unique: dict[str, Brief] = {}
    for brief in briefs:
        unique.setdefault(brief.key, brief)
    briefs = tuple(unique.values())
    keys = [row.key for row in briefs]
    with session_factory(engine)() as session:
        recorded = session.execute(
            select(storage.OpeningDecision, storage.OpeningCycle)
            .join(
                storage.OpeningCycle, storage.OpeningDecision.cycle_key == storage.OpeningCycle.key
            )
            .where(
                storage.OpeningDecision.key.in_(keys),
                storage.OpeningDecision.published_at <= now,
                storage.OpeningCycle.published_at <= now,
            )
        ).all()
        primaries = session.execute(
            select(storage.OpeningPrimary, storage.OpeningCycle)
            .join(
                storage.OpeningCycle, storage.OpeningPrimary.cycle_key == storage.OpeningCycle.key
            )
            .where(
                storage.OpeningPrimary.day.in_({b.day for b in briefs}),
                storage.OpeningPrimary.ticker.in_({b.ticker for b in briefs}),
                storage.OpeningPrimary.kind == "entry",
                storage.OpeningPrimary.published_at <= now,
                storage.OpeningCycle.published_at <= now,
            )
            .limit(cfg.query_limit + 1)
        ).all()
        if len(primaries) > cfg.query_limit:
            raise ValueError("call primary bound")
        canonical: dict[tuple[date, str], storage.OpeningPrimary] = {}
        for primary, cycle in sorted(primaries, key=lambda p: (utc(p[0].published_at), p[0].key)):
            try:
                if json.loads(cycle.payload_json).get("profile_hash") == cfg.profile_hash:
                    canonical.setdefault((primary.day, primary.ticker), primary)
            except (ValueError, AttributeError):
                continue
        outcomes = session.scalars(
            select(storage.OpeningOutcome)
            .where(
                storage.OpeningOutcome.primary_key.in_([p.key for p in canonical.values()]),
                storage.OpeningOutcome.observed_at <= now,
            )
            .limit(cfg.query_limit + 1)
        ).all()
        if len(outcomes) > cfg.query_limit:
            raise ValueError("call outcome bound")
        scans = (
            session.scalars(
                select(LiveScan).where(
                    LiveScan.scan_id.in_({cycle.scan_id for _, cycle in recorded}),
                    LiveScan.started_at <= now,
                    LiveScan.finished_at <= now,
                )
            ).all()
            if inspect(engine).has_table(LiveScan.__tablename__)
            else []
        )
    scan_map = {s.scan_id: s for s in scans}
    sources: dict[str, dict[str, Any]] = {}
    for scan in scans:
        try:
            parsed = json.loads(scan.snapshot_json)
            if isinstance(parsed, dict):
                sources[scan.scan_id] = parsed
        except ValueError:
            continue
    marks: dict[tuple[str, str], list[dict[str, Any] | None]] = defaultdict(list)
    for outcome in outcomes:
        try:
            value = json.loads(outcome.payload_json)
        except ValueError:
            value = None
        marks[outcome.primary_key, outcome.horizon].append(value)
    by_key = {r.key: (r, c) for r, c in recorded}
    rows = []
    for brief in briefs:
        record = by_key.get(brief.key)
        primary = canonical.get((brief.day, brief.ticker))
        original_key = primary.decision_key if primary else None
        first = bool(primary and original_key == brief.key)
        valid = False
        raw_marks: tuple[tuple[str, dict[str, Any] | None], ...] = ()
        if record:
            row, cycle = record
            current_scan = scan_map.get(cycle.scan_id)
            source = sources.get(cycle.scan_id)
            try:
                advice = Advice.model_validate_json(row.payload_json)
                metadata = json.loads(cycle.payload_json)
                valid = bool(
                    metadata.get("profile_hash") == cfg.profile_hash
                    and source
                    and current_scan
                    and utc(current_scan.started_at) <= brief.at
                    and utc(current_scan.finished_at) <= brief.at
                    and metadata.get("source_snapshot_hash") == digest(source)
                    and advice.ticker == brief.ticker
                    and row.day == cycle.day
                    and any(
                        isinstance(raw, dict)
                        and raw.get("ticker") == brief.ticker
                        and digest(raw) == advice.source_card_hash
                        and raw.get("profile_sha256") == advice.source_profile_hash
                        for raw in source.get("cards", [])
                    )
                )
            except (ValueError, AttributeError, ValidationError, TypeError):
                valid = False
        if first and primary:
            raw_marks = tuple(
                (label, values[0] if len(values) == 1 else {"invalid": True})
                for (pk, label), values in marks.items()
                if pk == primary.key
            )
        result = Result(
            brief.ticker, brief.at, "", raw_marks, brief.key, brief.day, cfg.profile_hash
        )
        blocked = "unverified" if not valid else "update" if not first else ""
        rows.append(Row(brief, original_key, first, valid, metrics(result, now, blocked)))
    paired = [
        next(m for m in row.metrics if m.label == primary_label)
        for row in rows
        if row.first and row.source_valid
    ]
    return View(
        tuple(rows),
        sum(row.first for row in rows),
        len({row.brief.day for row in rows if row.first}),
        sum(m.value_pct is not None for m in paired),
        sum(m.excess_pp is not None for m in paired),
        primary_label,
    )
