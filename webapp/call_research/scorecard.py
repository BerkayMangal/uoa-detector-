"""Read original statements and their exact provenance; never create advice."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from uoa_detector.live_alpha.calendar import close_time
from webapp.alpha_lab.db import digest
from webapp.board.db import session_factory
from webapp.calibration.settings import calendar
from webapp.call_research.settings import settings
from webapp.opening import storage
from webapp.opening.measurement import Mark
from webapp.opening.model import Advice, utc


@dataclass(frozen=True)
class Brief:
    key: str
    ticker: str
    day: date
    at: datetime
    action: str
    headline: str


@dataclass(frozen=True)
class Cost:
    kind: str
    units: int | None
    entry_per_unit: float | None
    planned_risk_per_unit: float | None
    roundtrip_per_unit: float | None
    roundtrip_pct: float | None
    breakeven: float | None
    readiness: str
    note: str


@dataclass(frozen=True)
class Excursion:
    count: int
    first_at: datetime | None
    last_at: datetime | None
    favorable_pct: float | None
    adverse_pct: float | None
    through: datetime
    limited: bool


@dataclass(frozen=True)
class Scorecard:
    key: str
    day: date
    at: datetime
    advice: Advice
    opening_profile: str
    scan_id: str
    source_valid: bool
    source_at: datetime | None
    canonical_key: str | None
    is_first_entry: bool
    costs: tuple[Cost, ...]
    marks: tuple[tuple[str, Mark | None], ...]
    excursion: Excursion | None


def number(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def costs(card: dict[str, Any]) -> tuple[Cost, ...]:
    # The only untyped input is an immutable hash-verified source-card JSON boundary.
    result = []
    stock = card.get("stock_plan")
    if isinstance(stock, dict):
        shares = number(stock.get("shares"))
        risk = number(stock.get("planned_risk_usd"))
        result.append(
            Cost(
                "stock",
                int(shares) if shares is not None and shares > 0 and shares.is_integer() else None,
                number(stock.get("entry_ref")),
                risk / shares if risk is not None and shares and shares > 0 else None,
                None,
                None,
                None,
                str(card.get("readiness", "unknown")),
                "Stop planı riski; azami zarar garantisi değildir.",
            )
        )
    for raw in card.get("options", []):
        if not isinstance(raw, dict):
            continue
        lots = number(raw.get("lots"))
        debit = number(raw.get("entry_debit"))
        credit = number(raw.get("exit_credit"))
        multiplier = number(raw.get("multiplier"))
        commission = number(raw.get("commission_usd"))
        roundtrip = (
            (debit - credit) * multiplier + commission
            if all(v is not None for v in (debit, credit, multiplier, commission))
            and debit is not None
            and credit is not None
            and multiplier is not None
            and commission is not None
            else None
        )
        result.append(
            Cost(
                str(raw.get("kind", "unknown")),
                int(lots) if lots is not None and lots >= 0 and lots.is_integer() else None,
                number(raw.get("entry_cost_usd")),
                number(raw.get("max_loss_usd")),
                roundtrip,
                number(raw.get("roundtrip_cost_pct")),
                number(raw.get("breakeven")),
                str(raw.get("readiness", "unknown")),
                str(raw.get("blocker") or "Prim, spread ve model komisyonu çağrı anına aittir."),
            )
        )
    return tuple(result)


def listing(
    engine: Engine, now: datetime, before: str = "", ticker: str = "", first_only: bool = True
) -> tuple[Brief, ...]:
    if not storage.available(engine):
        return ()
    with session_factory(engine)() as session:
        query = select(storage.OpeningDecision).where(storage.OpeningDecision.published_at <= now)
        if ticker:
            query = query.where(storage.OpeningDecision.ticker == ticker.strip().upper())
        if first_only:
            query = query.join(
                storage.OpeningPrimary,
                storage.OpeningPrimary.decision_key == storage.OpeningDecision.key,
            ).where(
                storage.OpeningPrimary.kind == "entry", storage.OpeningPrimary.published_at <= now
            )
        if before:
            cursor = session.get(storage.OpeningDecision, before)
            if cursor and utc(cursor.published_at) <= now:
                from sqlalchemy import and_, or_

                query = query.where(
                    or_(
                        storage.OpeningDecision.published_at < cursor.published_at,
                        and_(
                            storage.OpeningDecision.published_at == cursor.published_at,
                            storage.OpeningDecision.key < cursor.key,
                        ),
                    )
                )
        rows = session.scalars(
            query.order_by(
                storage.OpeningDecision.published_at.desc(), storage.OpeningDecision.key.desc()
            ).limit(settings().display_limit)
        ).all()
        result = []
        for row in rows:
            try:
                advice = Advice.model_validate_json(row.payload_json)
            except ValidationError:
                continue
            result.append(
                Brief(
                    row.key, row.ticker, row.day, utc(row.published_at), row.action, advice.headline
                )
            )
    return tuple(result)


def _excursion(
    session: Session, advice: Advice, at: datetime, day: date, now: datetime
) -> Excursion | None:
    cal = calendar()
    if advice.plan is None or advice.plan.entry <= 0 or day.year not in cal.years:
        return None
    closing = datetime.combine(day, close_time(day, cal), ZoneInfo(cal.timezone)).astimezone(UTC)
    through = min(now, closing)
    rows = session.scalars(
        select(storage.OpeningObservation)
        .where(
            storage.OpeningObservation.ticker == advice.ticker,
            storage.OpeningObservation.source_at > at,
            storage.OpeningObservation.source_at <= through,
            storage.OpeningObservation.available_at >= at,
            storage.OpeningObservation.available_at <= now,
        )
        .order_by(storage.OpeningObservation.source_at)
        .limit(settings().query_limit + 1)
    ).all()
    if len(rows) > settings().query_limit:
        return Excursion(0, None, None, None, None, through, True)
    prices = [r for r in rows if number(r.value) is not None and r.value > 0]
    sign = 1 if advice.direction == "up" else -1
    changes = [sign * (r.value / advice.plan.entry - 1) * 100 for r in prices]
    return Excursion(
        len(prices),
        utc(prices[0].source_at) if prices else None,
        utc(prices[-1].source_at) if prices else None,
        max(0.0, max(changes)) if changes else None,
        min(0.0, min(changes)) if changes else None,
        through,
        False,
    )


def get(engine: Engine, key: str, now: datetime) -> Scorecard | None:
    if not storage.available(engine):
        return None
    with session_factory(engine)() as session:
        row = session.get(storage.OpeningDecision, key)
        if row is None or utc(row.published_at) > now:
            return None
        cycle = session.get(storage.OpeningCycle, row.cycle_key)
        if cycle is None or utc(cycle.published_at) > now:
            return None
        try:
            advice = Advice.model_validate_json(row.payload_json)
            metadata = json.loads(cycle.payload_json)
        except (ValueError, TypeError):
            return None
        candidates = session.scalars(
            select(storage.OpeningPrimary)
            .where(
                storage.OpeningPrimary.kind == "entry",
                storage.OpeningPrimary.day == row.day,
                storage.OpeningPrimary.ticker == row.ticker,
                storage.OpeningPrimary.published_at <= now,
            )
            .order_by(storage.OpeningPrimary.published_at)
            .limit(settings().display_limit)
        ).all()
        canonical = None
        for primary in candidates:
            original = session.get(storage.OpeningCycle, primary.cycle_key)
            if original and json.loads(original.payload_json).get("profile_hash") == metadata.get(
                "profile_hash"
            ):
                canonical = primary
                break
        marks = []
        if canonical and canonical.decision_key == row.key:
            outcomes = session.scalars(
                select(storage.OpeningOutcome).where(
                    storage.OpeningOutcome.primary_key == canonical.key,
                    storage.OpeningOutcome.observed_at <= now,
                )
            ).all()
            for outcome in outcomes:
                try:
                    mark = Mark.model_validate_json(outcome.payload_json)
                except ValidationError:
                    mark = None
                if mark and (
                    mark.observed_at.tzinfo is None
                    or mark.target_at.tzinfo is None
                    or mark.observed_at > now
                    or mark.target_at > now
                    or mark.observed_at < mark.target_at
                ):
                    mark = None
                marks.append((outcome.horizon, mark))
        original_key = canonical.decision_key if canonical else None
        source = storage.source(engine, now, cycle.scan_id)
        source_card = None
        valid = False
        if (
            source
            and source.started_at <= utc(row.published_at)
            and source.finished_at <= utc(row.published_at)
            and metadata.get("source_snapshot_hash") == digest(source.snapshot)
        ):
            for raw in source.snapshot.get("cards", []):
                if (
                    isinstance(raw, dict)
                    and raw.get("ticker") == row.ticker
                    and digest(raw) == advice.source_card_hash
                    and raw.get("profile_sha256") == advice.source_profile_hash
                ):
                    source_card = raw
                    valid = True
                    break
        return Scorecard(
            row.key,
            row.day,
            utc(row.published_at),
            advice,
            str(metadata.get("profile_hash", "unknown")),
            cycle.scan_id,
            valid,
            source.started_at if source else None,
            original_key,
            bool(canonical and canonical.decision_key == row.key),
            costs(source_card) if source_card else (),
            tuple(
                sorted(
                    marks,
                    key=lambda item: (
                        item[1].target_at if item[1] else datetime.max.replace(tzinfo=UTC),
                        item[0],
                    ),
                )
            ),
            _excursion(session, advice, utc(row.published_at), row.day, now)
            if valid and canonical and canonical.decision_key == row.key
            else None,
        )
