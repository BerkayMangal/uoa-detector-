"""Additive append-only research storage; JSON is confined to this adapter boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import DateTime, Float, Integer, String, Table, Text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase


class Alert(AlfaBase):
    __tablename__ = "alfa_lab_alert"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    alert_id: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Poll(AlfaBase):
    __tablename__ = "alfa_lab_poll"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Audit(AlfaBase):
    __tablename__ = "alfa_lab_audit"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Price(AlfaBase):
    __tablename__ = "alfa_lab_price"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float] = mapped_column(Float)
    session_day: Mapped[str] = mapped_column(String)


class Cohort(AlfaBase):
    __tablename__ = "alfa_lab_cohort"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[str] = mapped_column(String, index=True)
    arm: Mapped[str] = mapped_column(String)
    horizon_count: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Outcome(AlfaBase):
    __tablename__ = "alfa_lab_outcome"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    cohort_key: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[str] = mapped_column(String, index=True)
    horizon: Mapped[int] = mapped_column(Integer)
    arm: Mapped[str] = mapped_column(String)
    stock_net_pct: Mapped[float] = mapped_column(Float)
    peer_excess_pp: Mapped[float] = mapped_column(Float)
    spy_excess_pp: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Cycle(AlfaBase):
    __tablename__ = "alfa_lab_cycle"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def ensure(engine: Engine) -> None:
    for model in (Alert, Poll, Audit, Price, Cohort, Outcome, Cycle):
        cast(Table, model.__table__).create(engine, checkfirst=True)


def append(engine: Engine, table: Table, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    # DB-side conflict handling preserves the original first_seen across racing
    # workers/restarts; no SELECT-then-INSERT window and no UPDATE path.
    constructor = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    inserted = 0
    with engine.begin() as connection:
        # Bounded batches stay under SQLite's parameter limit on older versions.
        for offset in range(0, len(rows), 100):
            result = connection.execute(
                constructor(table)
                .values(list(rows[offset : offset + 100]))
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(table.c.key)
            )
            inserted += len(result.all())
    return inserted
