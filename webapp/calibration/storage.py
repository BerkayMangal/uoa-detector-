"""Append-only daily payload capture and prospective instructions, no order API."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import Date, DateTime, String, Table, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.alpha_lab.db import append, digest, dumps
from webapp.alpha_lab.flow import utc
from webapp.board.daily_close import is_final_close
from webapp.board.db import AlfaBase, session_factory
from webapp.calibration.model import Bar
from webapp.ohlc import _regular_session_rows, _to_float

_logger = logging.getLogger(__name__)


class DailyBar(AlfaBase):
    __tablename__ = "alfa_calibration_bar"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Decision(AlfaBase):
    __tablename__ = "alfa_calibration_decision"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Result(AlfaBase):
    __tablename__ = "alfa_calibration_result"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    decision_key: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


def ensure(engine: Engine) -> None:
    for table in (DailyBar, Decision, Result):
        cast(Table, table.__table__).create(engine, checkfirst=True)


def capture_payload(engine: Engine, ticker: str, data: object, now: datetime) -> None:
    """Best-effort side capture, using exactly the legacy job's settled payload."""
    try:
        ensure(engine)
        grouped: dict[date, list[dict[str, float | None]]] = defaultdict(list)
        for day, row in _regular_session_rows(data):
            if is_final_close(day, now):
                grouped[day].append({k: _to_float(row.get(k)) for k in ("open", "high", "low", "close", "volume")})
        saved: list[dict[str, Any]] = []  # JSON/SQL serialization adapter.
        for day, values in grouped.items():
            distinct = {dumps(v) for v in values}
            payload: object = values[0] if len(distinct) == 1 else {"conflicting_rows": True}
            saved.append(dict(key=digest((ticker, day)), ticker=ticker, day=day, observed_at=utc(now), payload_json=dumps(payload)))
        append(engine, cast(Table, DailyBar.__table__), saved)
    except Exception:
        _logger.exception("calibration side capture failed; legacy daily bars preserved")


def bars_at(engine: Engine, now: datetime) -> tuple[Bar, ...]:
    with session_factory(engine)() as session:
        rows = session.scalars(select(DailyBar).where(DailyBar.observed_at <= now))
        result = []
        for row in rows:
            data = json.loads(row.payload_json)
            if any(data.get(k) is None for k in ("open", "high", "low", "close")):
                continue
            result.append(Bar(row.ticker, row.day, data["open"], data["high"], data["low"], data["close"], data.get("volume")))
        return tuple(result)
