"""Read-only supplement of an absent exact prior session, with physical source receipts."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import previous_session_before
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.db import digest
from webapp.board.daily_close import is_final_close
from webapp.board.db import session_factory
from webapp.calibration.model import Bar
from webapp.calibration.storage import DailyBar
from webapp.call_research.storage import DailyReceipt
from webapp.ohlc import RegularBar, _to_float
from webapp.opening.model import utc


def previous(day: date) -> date | None:
    return previous_session_before(day, load_live_alpha_settings().calendar)


def supplement(
    engine: Engine,
    legacy: dict[str, tuple[RegularBar, ...]],
    sessions: dict[str, date],
    asof: datetime,
) -> dict[str, tuple[RegularBar, ...]]:
    required = {ticker: previous(day) for ticker, day in sessions.items()}
    missing = {
        ticker: day
        for ticker, day in required.items()
        if day is not None and not any(b.day == day for b in legacy.get(ticker, ()))
    }
    if not missing:
        return legacy
    inspector = inspect(engine)
    if not all(inspector.has_table(t.__tablename__) for t in (DailyBar, DailyReceipt)):
        return legacy
    with session_factory(engine)() as session:
        rows = session.execute(
            select(DailyBar, DailyReceipt)
            .join(
                DailyReceipt,
                DailyReceipt.key == DailyBar.key,
            )
            .where(
                DailyBar.key.in_([digest((ticker, day)) for ticker, day in missing.items()]),
                DailyBar.observed_at <= asof,
                DailyReceipt.observed_at <= asof,
            )
        ).all()
    result = dict(legacy)
    for row, receipt in rows:
        if (
            row.day != missing.get(row.ticker)
            or receipt.payload_hash != digest(row.payload_json)
            or not is_final_close(row.day, utc(row.observed_at))
        ):
            continue
        try:
            raw: dict[str, Any] = json.loads(row.payload_json)  # immutable JSON adapter
            values = [_to_float(raw.get(k)) for k in ("open", "high", "low", "close")]
            if any(v is None for v in values):
                continue
            opening, high, low, close = (float(v) for v in values if v is not None)
            if not Bar(row.ticker, row.day, opening, high, low, close).valid:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        point = RegularBar(row.day, opening, high, low, close)
        result[row.ticker] = tuple(
            sorted((*legacy.get(row.ticker, ()), point), key=lambda p: p.day)
        )
    return result
