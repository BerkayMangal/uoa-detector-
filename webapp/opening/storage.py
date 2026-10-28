"""Append-only persistence adapter: publish a complete cycle in one transaction."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import Date, DateTime, Float, String, Table, Text, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.alpha_lab.db import digest, dumps
from webapp.board.db import AlfaBase, session_factory
from webapp.live_alpha.store import LiveScan, ScanRow
from webapp.opening.model import Advice, Clock, code_hash, settings, utc
from webapp.opening.policy import fresh, point


class OpeningCycle(AlfaBase):
    __tablename__ = "alfa_opening_cycle"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    scan_id: Mapped[str] = mapped_column(String, index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class OpeningDecision(AlfaBase):
    __tablename__ = "alfa_opening_decision"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    cycle_key: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    action: Mapped[str] = mapped_column(String)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class OpeningPrimary(AlfaBase):
    __tablename__ = "alfa_opening_primary"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    cycle_key: Mapped[str] = mapped_column(String)
    decision_key: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OpeningObservation(AlfaBase):
    __tablename__ = "alfa_opening_observation"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[float] = mapped_column(Float)
    scan_id: Mapped[str] = mapped_column(String)


class OpeningOutcome(AlfaBase):
    __tablename__ = "alfa_opening_outcome"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    primary_key: Mapped[str] = mapped_column(String, index=True)
    horizon: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


TABLES = (OpeningCycle, OpeningDecision, OpeningPrimary, OpeningObservation, OpeningOutcome)


def ensure(engine: Engine) -> None:
    for model in TABLES:
        cast(Table, model.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    return all(inspect(engine).has_table(model.__tablename__) for model in TABLES)


def source(engine: Engine, now: datetime, scan_id: str | None = None) -> ScanRow | None:
    if not inspect(engine).has_table(LiveScan.__tablename__):
        return None
    query = select(LiveScan)
    if scan_id is not None:
        query = query.where(LiveScan.scan_id == scan_id)
    with session_factory(engine)() as s:
        row = s.scalar(
            query.where(LiveScan.started_at <= now, LiveScan.finished_at <= now)
            .order_by(LiveScan.started_at.desc(), LiveScan.scan_id)
            .limit(1)
        )
        return (
            ScanRow(
                row.scan_id,
                utc(row.started_at),
                utc(row.finished_at),
                row.market_mode,
                row.status,
                row.run_id,
                json.loads(row.snapshot_json),
            )
            if row
            else None
        )


def publish(
    engine: Engine, scan: ScanRow, now: datetime, c: Clock, advice: tuple[Advice, ...]
) -> bool:
    cfg = settings()
    key = digest((cfg.profile_hash, c.day, scan.scan_id, c.phase))
    constructor = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    # All inserts use the SAME connection/transaction, including the winning key.
    # A race loser publishes nothing; an exception rolls back the complete cycle.
    with engine.begin() as connection:

        def insert(model: type[AlfaBase], row: dict[str, Any]) -> bool:
            table = cast(Table, model.__table__)
            return (
                connection.execute(
                    constructor(table)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=["key"])
                    .returning(table.c.key)
                ).first()
                is not None
            )

        payload = {
            "version": cfg.version,
            "profile_hash": cfg.profile_hash,
            "code_hash": code_hash(),
            "phase": c.phase,
            "reason": c.reason,
            "scan_id": scan.scan_id,
            "source_started_at": scan.started_at,
            "source_finished_at": scan.finished_at,
            "source_snapshot_hash": digest(scan.snapshot),
            "published_at": now,
            "opening": c.opening,
            "assessment": c.assessment,
            "advice_count": len(advice),
        }
        if not insert(
            OpeningCycle,
            dict(
                key=key,
                day=c.day,
                scan_id=scan.scan_id,
                published_at=now,
                payload_json=dumps(payload),
            ),
        ):
            return False
        for item in advice:
            decision_key = digest((key, item.scope, item.position_id, item.ticker))
            insert(
                OpeningDecision,
                dict(
                    key=decision_key,
                    cycle_key=key,
                    ticker=item.ticker,
                    day=c.day,
                    action=item.action,
                    published_at=now,
                    payload_json=item.model_dump_json(),
                ),
            )
            if item.action == "BUY":
                insert(
                    OpeningPrimary,
                    dict(
                        key=digest((cfg.profile_hash, c.day, "entry", item.ticker)),
                        day=c.day,
                        ticker=item.ticker,
                        kind="entry",
                        cycle_key=key,
                        decision_key=decision_key,
                        published_at=now,
                    ),
                )
        checkpoint_start = c.preparation if c.checkpoint == "preparation" else c.assessment
        if (
            c.checkpoint
            and checkpoint_start
            and checkpoint_start <= utc(scan.started_at)
            and fresh(utc(scan.started_at), now, cfg.source_max_age_seconds)
            and utc(scan.finished_at) <= now
        ):
            insert(
                OpeningPrimary,
                dict(
                    key=digest((cfg.profile_hash, c.day, c.checkpoint)),
                    day=c.day,
                    ticker="*",
                    kind=c.checkpoint,
                    cycle_key=key,
                    decision_key=None,
                    published_at=now,
                ),
            )
        for ticker, raw in scan.snapshot.get("price_observations", {}).items():
            p = point(raw, now, c)
            if p:
                value, at = p
                insert(
                    OpeningObservation,
                    dict(
                        key=digest((cfg.profile_hash, ticker, at)),
                        ticker=ticker,
                        source_at=at,
                        available_at=now,
                        value=value,
                        scan_id=scan.scan_id,
                    ),
                )
    return True
