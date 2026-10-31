"""Daily capture membership and as-of coverage, independent of signal success."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import previous_session_before
from webapp.alpha_lab.db import digest
from webapp.board.baskets import basket_close_tickers
from webapp.board.db import session_factory
from webapp.calibration.model import Bar
from webapp.calibration.settings import calendar
from webapp.calibration.storage import DailyBar
from webapp.call_research.data import Dataset, prepare
from webapp.call_research.settings import settings
from webapp.call_research.storage import DailyReceipt
from webapp.opening.model import utc
from webapp.research_readiness import settled_day
from webapp.tournament.report import load as original_report


@dataclass(frozen=True)
class Observed:
    bar: Bar
    available_at: datetime
    verified: bool = False


@dataclass(frozen=True)
class CoverageRow:
    ticker: str
    status: str
    latest: date | None
    history: int
    is_context: bool


@dataclass(frozen=True)
class Coverage:
    expected_day: date | None
    ready: int
    expected: int
    limited: bool
    rows: tuple[CoverageRow, ...]


def targets() -> tuple[str, ...]:
    result = tuple(sorted(set(original_report()["tickers"]) | set(settings().context_tickers)))
    if len(result) > settings().capture_max_symbols:
        raise ValueError("research capture membership exceeds frozen bound")
    return result


def daily_close_targets() -> tuple[str, ...]:
    return tuple(dict.fromkeys((*basket_close_tickers(), *targets())))


def prospective_stocks() -> tuple[str, ...]:
    return tuple(
        sorted(
            (set(original_report()["tickers"]) - set(settings().reference_only))
            | set(settings().prospective_extra_symbols)
        )
    )


def observed(
    engine: Engine, now: datetime, verified: bool = False
) -> tuple[tuple[Observed, ...], bool]:
    if not inspect(engine).has_table(DailyBar.__tablename__):
        return (), False
    finality: dict[datetime, date | None] = {}
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(DailyBar)
            .where(DailyBar.observed_at <= now, DailyBar.ticker.in_(targets()))
            .order_by(DailyBar.observed_at.desc(), DailyBar.key)
            .limit(settings().query_limit + 1)
        ).all()
        if len(rows) > settings().query_limit:
            return (), True
        receipts = (
            {
                r.key: r
                for r in session.scalars(
                    select(DailyReceipt)
                    .where(DailyReceipt.observed_at <= now)
                    .limit(settings().query_limit + 1)
                )
            }
            if verified and inspect(engine).has_table(DailyReceipt.__tablename__)
            else {}
        )
        if len(receipts) > settings().query_limit:
            return (), True
        result = []
        for row in rows:
            receipt = receipts.get(row.key)
            has_receipt = bool(receipt and receipt.payload_hash == digest(row.payload_json))
            if verified and not has_receipt:
                continue
            available_at = (
                max(utc(row.observed_at), utc(receipt.observed_at))
                if has_receipt and receipt
                else utc(row.observed_at)
            )
            try:
                raw = json.loads(row.payload_json)
                values = [raw.get(k) for k in ("open", "high", "low", "close")]
                good = all(
                    isinstance(v, (float, int))
                    and not isinstance(v, bool)
                    and math.isfinite(v)
                    and v > 0
                    for v in values
                )
                available = utc(row.observed_at)
                if available not in finality:
                    finality[available] = settled_day(available)
                final_day = finality[available]
                good = good and final_day is not None and row.day <= final_day
                bar_open, high, low, close = (
                    (float(v) for v in values) if good else (float("nan"),) * 4
                )
                volume = raw.get("volume")
                result.append(
                    Observed(
                        Bar(
                            row.ticker,
                            row.day,
                            bar_open,
                            high,
                            low,
                            close,
                            float(volume)
                            if isinstance(volume, (float, int)) and math.isfinite(volume)
                            else None,
                        ),
                        available_at,
                        has_receipt,
                    )
                )
            except (ValueError, TypeError, AttributeError):
                result.append(
                    Observed(
                        Bar(row.ticker, row.day, *(float("nan"),) * 4, None),
                        available_at,
                        has_receipt,
                    )
                )
    return tuple(result), False


def view(engine: Engine, now: datetime) -> Coverage:
    points, limited = observed(engine, now)
    expected = settled_day(now)
    panel = {
        (p.bar.ticker, p.bar.day): p.bar
        for p in points
        if expected is not None and p.bar.day <= expected
    }
    required = []
    day = expected
    for _ in range(settings().persistence_sessions + 1):
        if day is None:
            break
        required.append(day)
        day = previous_session_before(day, calendar())
    result = []
    for name in targets():
        known = [b for (ticker, _), b in panel.items() if ticker == name and b.valid]
        latest = max((b.day for b in known), default=None)
        current = panel.get((name, expected)) if expected else None
        status = (
            "calendar_unknown"
            if expected is None
            else "limited"
            if limited
            else "not_collected"
            if not known and current is None
            else "current_missing"
            if current is None
            else "invalid"
            if not current.valid
            else "history_missing"
            if len(required) != settings().persistence_sessions + 1
            or any((name, d) not in panel or not panel[name, d].valid for d in required)
            else "ready"
        )
        result.append(
            CoverageRow(name, status, latest, len(known), name in settings().context_tickers)
        )
    return Coverage(
        expected, sum(r.status == "ready" for r in result), len(result), limited, tuple(result)
    )


def research_data(engine: Engine, now: datetime) -> tuple[Dataset, bool]:
    points, limited = observed(engine, now, verified=True)
    expected = settled_day(now)
    allowed = set(prospective_stocks()) | set(settings().benchmarks)
    bars = tuple(
        p.bar
        for p in points
        if p.bar.ticker in allowed
        and p.bar.valid
        and expected is not None
        and p.bar.day <= expected
    )
    return prepare(bars), limited
