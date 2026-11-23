"""Transaction boundary for the prospective PAPER calibration record."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from sqlalchemy import Boolean, Date, DateTime, Index, String, Table, Text, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.dml import Insert as SqliteInsert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.shadow_capture.model import aware, stored

DETAIL_EVENT_LIMIT = 2000


@dataclass(frozen=True)
class CaseRecord:
    opportunity_id: str
    ticker: str
    session: date
    published_at: datetime
    document: str
    active: bool


@dataclass(frozen=True)
class EventRecord:
    opportunity_id: str
    scan_id: str
    at: datetime
    document: str


@dataclass(frozen=True)
class ReportInputs:
    latest_at: datetime | None
    all_cases: tuple[CaseRecord, ...]
    cases: tuple[CaseRecord, ...]
    total: int
    ready: bool


class CalibrationClock(AlfaBase):
    __tablename__ = "alfa_paper_calibration_clock"

    study_key: Mapped[str] = mapped_column(String, primary_key=True)
    scan_id: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CalibrationCase(AlfaBase):
    __tablename__ = "alfa_paper_calibration_case"
    __table_args__ = (
        Index("ix_alfa_paper_calibration_case_active", "study_key", "active"),
        Index("ix_alfa_paper_calibration_case_session", "study_key", "session"),
    )

    study_key: Mapped[str] = mapped_column(String, primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String)
    session: Mapped[date] = mapped_column(Date)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    document: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean)


class CalibrationEvent(AlfaBase):
    __tablename__ = "alfa_paper_calibration_event"

    study_key: Mapped[str] = mapped_column(String, primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String, primary_key=True)
    scan_id: Mapped[str] = mapped_column(String, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    document: Mapped[str] = mapped_column(Text)


_TABLES = (CalibrationCase, CalibrationEvent, CalibrationClock)
_SCHEMA_LOCK = 0x554F415041504552


def ensure(engine: Engine) -> None:
    """Provision only the experiment tables, from the worker's startup path."""
    tables = [cast(Table, model.__table__) for model in _TABLES]
    with engine.begin() as connection:
        # Serialize the check/create sequence across concurrent worker starts.
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK})
        elif connection.dialect.name == "sqlite":
            connection.execute(text("BEGIN IMMEDIATE"))
        AlfaBase.metadata.create_all(connection, tables=tables)


def _available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(model.__tablename__ in names for model in _TABLES)


def _insert(connection: Connection, table: Table) -> PgInsert | SqliteInsert:
    if connection.dialect.name == "postgresql":
        return pg_insert(table)
    if connection.dialect.name == "sqlite":
        return sqlite_insert(table)
    raise ValueError("unsupported PAPER calibration database")


def _case(row: CalibrationCase) -> CaseRecord:
    return CaseRecord(
        row.opportunity_id,
        row.ticker,
        row.session,
        stored(row.published_at),
        row.document,
        row.active,
    )


def _event(row: CalibrationEvent) -> EventRecord:
    return EventRecord(row.opportunity_id, row.scan_id, stored(row.at), row.document)


def latest_observed_at(engine: Engine, study_key: str) -> datetime | None:
    stmt = select(CalibrationClock.observed_at).where(CalibrationClock.study_key == study_key)
    with engine.connect() as connection:
        value = connection.scalar(stmt)
    return stored(value) if value is not None else None


def active_tickers(engine: Engine, study_key: str) -> tuple[str, ...]:
    if not _available(engine):
        return ()
    stmt = (
        select(CalibrationCase.ticker)
        .where(CalibrationCase.study_key == study_key, CalibrationCase.active.is_(True))
        .distinct()
        .order_by(CalibrationCase.ticker)
    )
    with engine.connect() as connection:
        return tuple(connection.scalars(stmt))


class Scan:
    """A single accepted observation, using the transaction that advanced the clock."""

    def __init__(self, connection: Connection, study_key: str, scan_id: str) -> None:
        self._connection = connection
        self._study_key = study_key
        self._scan_id = scan_id

    def active_cases(self) -> tuple[CaseRecord, ...]:
        stmt = (
            select(CalibrationCase)
            .where(CalibrationCase.study_key == self._study_key, CalibrationCase.active.is_(True))
            .order_by(CalibrationCase.published_at, CalibrationCase.opportunity_id)
        )
        with Session(bind=self._connection) as session:
            return tuple(_case(row) for row in session.scalars(stmt))

    def existing(self, session: date) -> frozenset[str]:
        stmt = select(CalibrationCase.opportunity_id).where(
            CalibrationCase.study_key == self._study_key, CalibrationCase.session == session
        )
        return frozenset(self._connection.scalars(stmt))

    def put(self, record: CaseRecord) -> None:
        table = cast(Table, CalibrationCase.__table__)
        stmt = _insert(self._connection, table).values(
            study_key=self._study_key,
            opportunity_id=record.opportunity_id,
            ticker=record.ticker,
            session=record.session,
            published_at=aware(record.published_at),
            document=record.document,
            active=record.active,
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[table.c.study_key, table.c.opportunity_id],
            set_={"document": stmt.excluded.document, "active": stmt.excluded.active},
            where=(
                (table.c.ticker == stmt.excluded.ticker)
                & (table.c.session == stmt.excluded.session)
                & (table.c.published_at == stmt.excluded.published_at)
            ),
        ).returning(table.c.opportunity_id)
        if self._connection.execute(upsert).first() is None:
            raise ValueError("PAPER calibration case identity changed")

    def add_event(self, record: EventRecord) -> None:
        if record.scan_id != self._scan_id:
            raise ValueError("PAPER calibration event belongs to another scan")
        table = cast(Table, CalibrationEvent.__table__)
        self._connection.execute(
            table.insert().values(
                study_key=self._study_key,
                opportunity_id=record.opportunity_id,
                scan_id=record.scan_id,
                at=aware(record.at),
                document=record.document,
            )
        )


@contextmanager
def begin_scan(
    engine: Engine, study_key: str, scan_id: str, observed_at: datetime
) -> Iterator[Scan | None]:
    at = aware(observed_at)
    table = cast(Table, CalibrationClock.__table__)
    with engine.begin() as connection:
        stmt = _insert(connection, table).values(
            study_key=study_key, scan_id=scan_id, observed_at=at
        )
        claim = stmt.on_conflict_do_update(
            index_elements=[table.c.study_key],
            set_={"scan_id": stmt.excluded.scan_id, "observed_at": stmt.excluded.observed_at},
            where=(table.c.observed_at < stmt.excluded.observed_at)
            & (table.c.scan_id != stmt.excluded.scan_id),
        ).returning(table.c.study_key)
        claimed = connection.execute(claim).first() is not None
        yield Scan(connection, study_key, scan_id) if claimed else None


def report_inputs(
    engine: Engine, study_key: str, ticker: str | None, page: int, page_size: int
) -> ReportInputs:
    if page < 1 or page_size < 1:
        raise ValueError("PAPER calibration pagination must be positive")
    if not _available(engine):
        return ReportInputs(None, (), (), 0, False)
    # A single SELECT ties the clock to all case documents in the same database snapshot.
    stmt = (
        select(CalibrationClock.observed_at, CalibrationCase)
        .select_from(CalibrationClock)
        .outerjoin(CalibrationCase, CalibrationClock.study_key == CalibrationCase.study_key)
        .where(CalibrationClock.study_key == study_key)
        .order_by(CalibrationCase.published_at.desc(), CalibrationCase.opportunity_id)
    )
    with session_factory(engine)() as session:
        rows = session.execute(stmt).all()
        latest_at = stored(rows[0].observed_at) if rows else None
        all_cases = tuple(_case(record) for _, record in rows if record is not None)
    filtered = (
        all_cases if not ticker else tuple(case for case in all_cases if case.ticker == ticker)
    )
    offset = (page - 1) * page_size
    return ReportInputs(
        latest_at, all_cases, filtered[offset : offset + page_size], len(filtered), True
    )


def detail_inputs(
    engine: Engine, study_key: str, opportunity_id: str
) -> tuple[CaseRecord | None, tuple[EventRecord, ...]]:
    if not _available(engine):
        return None, ()
    stmt = (
        select(CalibrationCase, CalibrationEvent)
        .outerjoin(
            CalibrationEvent,
            (CalibrationCase.study_key == CalibrationEvent.study_key)
            & (CalibrationCase.opportunity_id == CalibrationEvent.opportunity_id),
        )
        .where(
            CalibrationCase.study_key == study_key, CalibrationCase.opportunity_id == opportunity_id
        )
        .order_by(CalibrationEvent.at.desc(), CalibrationEvent.scan_id.desc())
        .limit(DETAIL_EVENT_LIMIT)
    )
    with session_factory(engine)() as session:
        rows = session.execute(stmt).all()
        if not rows:
            return None, ()
        return _case(rows[0][0]), tuple(
            _event(event) for _, event in reversed(rows) if event is not None
        )
