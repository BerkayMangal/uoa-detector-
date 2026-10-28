"""First-entry UNDERLYING reference returns, never old PAPER fills or option P&L.

JSON/Any is confined to this evidence persistence adapter. Missing observations
are not silently replaced by a later day or by the last cached price.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Table, inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import close_time, next_session_after
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import session_factory
from webapp.calibration.storage import DailyBar
from webapp.opening.model import Advice, settings, utc
from webapp.opening.policy import number
from webapp.opening.storage import (
    OpeningDecision,
    OpeningObservation,
    OpeningOutcome,
    OpeningPrimary,
)


def measure(engine: Engine, now: datetime) -> int:
    cfg, base = settings(), load_live_alpha_settings()
    saved: list[dict[str, Any]] = []
    with session_factory(engine)() as s:
        primaries = list(
            s.scalars(
                select(OpeningPrimary)
                .where(OpeningPrimary.kind == "entry", OpeningPrimary.published_at <= now)
                .order_by(OpeningPrimary.published_at.desc())
                .limit(cfg.query_limit + 1)
            )
        )
        if len(primaries) > cfg.query_limit:
            raise ValueError("opening measurement bound exceeded")
        known = set(
            s.scalars(
                select(OpeningOutcome.key).where(
                    OpeningOutcome.primary_key.in_([p.key for p in primaries])
                )
            )
        )
        for primary in primaries:
            row = s.get(OpeningDecision, primary.decision_key)
            if row is None:
                continue
            advice = Advice.model_validate_json(row.payload_json)
            entry_at = utc(primary.published_at)
            if advice.source_at is None or advice.plan is None:
                continue
            entry = advice.plan.entry
            # Benchmark must have been captured by this decision's publication.
            spy_entry = s.scalar(
                select(OpeningObservation)
                .where(
                    OpeningObservation.ticker == "SPY",
                    OpeningObservation.available_at <= entry_at,
                    OpeningObservation.source_at
                    >= entry_at - timedelta(seconds=cfg.source_max_age_seconds),
                    OpeningObservation.source_at <= entry_at,
                )
                .order_by(
                    OpeningObservation.available_at.desc(), OpeningObservation.source_at.desc()
                )
                .limit(1)
            )
            horizons: list[tuple[str, datetime, int | None]] = [
                (f"m{m}", entry_at + timedelta(minutes=m), None) for m in cfg.intraday_minutes
            ]
            for count in cfg.close_sessions:
                day = primary.day
                for _ in range(count):
                    next_day = next_session_after(day, base.calendar)
                    if next_day is None:
                        break
                    day = next_day
                else:
                    horizons.append(
                        (
                            f"c{count}",
                            datetime.combine(
                                day,
                                close_time(day, base.calendar),
                                ZoneInfo(base.calendar.timezone),
                            ),
                            count,
                        )
                    )
            for name, target, daily in horizons:
                key = digest((primary.key, name))
                if key in known or now < target:
                    continue
                own_value: float | None = None
                spy_value: float | None = None
                own_at: datetime | None = None
                spy_at: datetime | None = None
                availability: datetime | None = None
                if daily is None:
                    end = target + timedelta(seconds=cfg.reference_window_seconds)
                    own = s.scalar(
                        select(OpeningObservation)
                        .where(
                            OpeningObservation.ticker == primary.ticker,
                            OpeningObservation.source_at >= target,
                            OpeningObservation.source_at <= end,
                            OpeningObservation.available_at >= target,
                            OpeningObservation.available_at <= min(now, end),
                        )
                        .order_by(OpeningObservation.available_at, OpeningObservation.source_at)
                        .limit(1)
                    )
                    if own:
                        own_value, own_at, availability = (
                            own.value,
                            utc(own.source_at),
                            utc(own.available_at),
                        )
                        spy = s.scalar(
                            select(OpeningObservation)
                            .where(
                                OpeningObservation.ticker == "SPY",
                                OpeningObservation.source_at >= target,
                                OpeningObservation.source_at <= end,
                                OpeningObservation.available_at >= target,
                                OpeningObservation.available_at <= min(now, end),
                                OpeningObservation.source_at
                                >= own_at - timedelta(seconds=cfg.reference_window_seconds),
                                OpeningObservation.source_at
                                <= own_at + timedelta(seconds=cfg.reference_window_seconds),
                            )
                            .order_by(OpeningObservation.available_at, OpeningObservation.source_at)
                            .limit(1)
                        )
                        if spy:
                            spy_value, spy_at = spy.value, utc(spy.source_at)
                    # Allow the remainder of the declared window to arrive before
                    # freezing a missing own or benchmark observation.
                    if (own_value is None or spy_value is None) and now <= end:
                        continue
                elif inspect(engine).has_table(DailyBar.__tablename__):
                    for ticker in (primary.ticker, "SPY"):
                        bar = s.scalar(
                            select(DailyBar)
                            .where(
                                DailyBar.ticker == ticker,
                                DailyBar.day == target.date(),
                                DailyBar.observed_at >= target,
                                DailyBar.observed_at <= now,
                            )
                            .limit(1)
                        )
                        value = number(json.loads(bar.payload_json).get("close")) if bar else None
                        if value is not None and value > 0 and bar:
                            if ticker == primary.ticker:
                                own_value, own_at, availability = (
                                    value,
                                    target,
                                    utc(bar.observed_at),
                                )
                            else:
                                spy_value, spy_at = value, target
                    if own_value is None or spy_value is None:
                        continue  # Pending exact settled date; never use another close.
                else:
                    continue
                sign = 1 if advice.direction == "up" else -1
                underlying_return = (
                    sign * (own_value / entry - 1) * 100 if own_value is not None else None
                )
                spy_return = (
                    (spy_value / spy_entry.value - 1) * 100
                    if spy_value is not None and spy_entry is not None
                    else None
                )
                payload = {
                    "status": "complete"
                    if underlying_return is not None and spy_return is not None
                    else "missing_reference",
                    "target_at": target,
                    "observed_at": now,
                    "price_available_at": availability,
                    "entry_reference": entry,
                    "entry_published_at": entry_at,
                    "entry_source_at": advice.source_at,
                    "spy_entry": spy_entry.value if spy_entry else None,
                    "spy_entry_source_at": utc(spy_entry.source_at) if spy_entry else None,
                    "own_price": own_value,
                    "own_source_at": own_at,
                    "spy_price": spy_value,
                    "spy_source_at": spy_at,
                    "directional_underlying_pct": underlying_return,
                    "spy_pct": spy_return,
                    "directional_excess_pp": underlying_return - sign * spy_return
                    if underlying_return is not None and spy_return is not None
                    else None,
                    "basis": "Dayanak referans getirisi; emir/dolum veya opsiyon P&L değil. Ücret, spread ve kayma hariç.",
                    "missing": [
                        n
                        for n, v in (
                            ("dayanak", own_value),
                            ("SPY giriş", spy_entry),
                            ("SPY çıkış", spy_value),
                        )
                        if v is None
                    ],
                }
                saved.append(
                    dict(
                        key=key,
                        primary_key=primary.key,
                        horizon=name,
                        observed_at=now,
                        payload_json=dumps(payload),
                    )
                )
    return append(engine, cast(Table, OpeningOutcome.__table__), saved)
