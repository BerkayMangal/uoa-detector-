"""Live Alpha tables (contract §8). New ``alfa_live_*`` tables only; created with
``checkfirst``; nothing here alters or drops an existing table.

Append-only: ``alfa_live_scan``, ``alfa_live_rec``, ``alfa_live_news``,
``alfa_live_event``, ``alfa_live_manual``, ``alfa_live_heartbeat``.
``alfa_live_paper`` holds one row per opportunity whose state columns move
forward, and every such move is also written to ``alfa_live_event`` with who,
when, why and the policy version — so the history is never lost to an update.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Table,
    Text,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine


class LiveScan(AlfaBase):
    """One engine cycle: the snapshot the page renders, plus its funnel and timing."""

    __tablename__ = "alfa_live_scan"

    scan_id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market_mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)          # ok / degraded / failed
    policy_version: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text)


class LiveRec(AlfaBase):
    """An immutable recommendation record. A new row only when (rec, readiness) changes."""

    __tablename__ = "alfa_live_rec"

    rec_id: Mapped[str] = mapped_column(String, primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recommendation: Mapped[str] = mapped_column(String)
    readiness: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    policy_version: Mapped[str] = mapped_column(String)
    profile_sha256: Mapped[str] = mapped_column(String)
    inputs_hash: Mapped[str] = mapped_column(String)
    supersedes: Mapped[str | None] = mapped_column(String, nullable=True)
    scan_id: Mapped[str] = mapped_column(String)
    card_json: Mapped[str] = mapped_column(Text)


class LiveNews(AlfaBase):
    """A headline as the provider returned it. ``provider_created_at`` keeps UW's name."""

    __tablename__ = "alfa_live_news"

    ref: Mapped[str] = mapped_column(String, primary_key=True)   # sha256(source|headline|created_at)
    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String)
    provider_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sentiment: Mapped[str | None] = mapped_column(String, nullable=True)
    is_major: Mapped[bool] = mapped_column(Boolean)
    tags_json: Mapped[str] = mapped_column(Text)
    tickers_json: Mapped[str] = mapped_column(Text)


class LiveNewsCheck(AlfaBase):
    """One headlines call per ticker: when, and what it produced (a failure is a state)."""

    __tablename__ = "alfa_live_news_check"

    check_id: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text)
    refs_json: Mapped[str] = mapped_column(Text)


class LivePaper(AlfaBase):
    """One PAPER position per opportunity, in the preferred instrument only."""

    __tablename__ = "alfa_live_paper"

    paper_id: Mapped[str] = mapped_column(String, primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String, unique=True)
    rec_id: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    instrument: Mapped[str] = mapped_column(String)          # stock / long_call / bull_call_debit / ...
    plan_json: Mapped[str] = mapped_column(Text)              # frozen plan at creation
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_mark: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_mark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LiveEvent(AlfaBase):
    """Every state change of a recommendation or PAPER position."""

    __tablename__ = "alfa_live_event"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)                # job / owner
    policy_version: Mapped[str] = mapped_column(String)
    detail_json: Mapped[str] = mapped_column(Text)


class LiveManual(AlfaBase):
    """The owner's own fill, kept apart from PAPER (MANUAL_FILL)."""

    __tablename__ = "alfa_live_manual"

    manual_id: Mapped[str] = mapped_column(String, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opportunity_id: Mapped[str] = mapped_column(String, index=True)
    rec_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ticker: Mapped[str] = mapped_column(String)
    instrument: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text)


class LiveHeartbeat(AlfaBase):
    __tablename__ = "alfa_live_heartbeat"

    beat_id: Mapped[str] = mapped_column(String, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String)
    detail_json: Mapped[str] = mapped_column(Text)


_TABLES = (LiveScan, LiveRec, LiveNews, LiveNewsCheck, LivePaper, LiveEvent, LiveManual, LiveHeartbeat)


def ensure_live_tables(engine: Engine) -> None:
    for model in _TABLES:
        cast("Table", model.__table__).create(engine, checkfirst=True)


def new_id() -> str:
    return uuid.uuid4().hex


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanRow:
    scan_id: str
    started_at: datetime
    finished_at: datetime
    market_mode: str
    status: str
    run_id: str | None
    snapshot: dict[str, Any]


def write_scan(engine: Engine, row: ScanRow, policy_version: str) -> None:
    with session_factory(engine)() as s, s.begin():
        s.add(LiveScan(
            scan_id=row.scan_id, started_at=row.started_at, finished_at=row.finished_at,
            market_mode=row.market_mode, status=row.status, policy_version=policy_version,
            run_id=row.run_id, snapshot_json=dumps(row.snapshot),
        ))


def latest_scan(engine: Engine) -> ScanRow | None:
    stmt = select(LiveScan).order_by(LiveScan.started_at.desc()).limit(1)
    with session_factory(engine)() as s:
        got = s.execute(stmt).scalars().first()
        if got is None:
            return None
        return ScanRow(
            scan_id=got.scan_id, started_at=got.started_at, finished_at=got.finished_at,
            market_mode=got.market_mode, status=got.status, run_id=got.run_id,
            snapshot=json.loads(got.snapshot_json),
        )


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecHead:
    rec_id: str
    recommendation: str
    readiness: str
    created_at: datetime


def latest_recs(engine: Engine, opportunity_ids: Sequence[str]) -> dict[str, RecHead]:
    if not opportunity_ids:
        return {}
    stmt = (
        select(LiveRec.opportunity_id, LiveRec.rec_id, LiveRec.recommendation, LiveRec.readiness,
               LiveRec.created_at)
        .where(LiveRec.opportunity_id.in_(list(opportunity_ids)))
        .order_by(LiveRec.created_at)
    )
    out: dict[str, RecHead] = {}
    with session_factory(engine)() as s:
        for opp, rec_id, rec, ready, created in s.execute(stmt).all():
            out[opp] = RecHead(rec_id=rec_id, recommendation=rec, readiness=ready, created_at=created)
    return out


def append_rec(engine: Engine, rec: LiveRec) -> None:
    with session_factory(engine)() as s, s.begin():
        s.add(rec)


def recs_for(engine: Engine, opportunity_id: str) -> list[dict[str, Any]]:
    stmt = select(LiveRec).where(LiveRec.opportunity_id == opportunity_id).order_by(LiveRec.created_at)
    with session_factory(engine)() as s:
        return [
            {
                "rec_id": r.rec_id, "created_at": r.created_at, "recommendation": r.recommendation,
                "readiness": r.readiness, "path": r.path, "supersedes": r.supersedes,
                "card": json.loads(r.card_json),
            }
            for r in s.execute(stmt).scalars()
        ]


# ---------------------------------------------------------------------------
# Events, paper, manual, heartbeat
# ---------------------------------------------------------------------------


def append_event(
    engine: Engine, *, at: datetime, opportunity_id: str, kind: str, actor: str,
    policy_version: str, detail: dict[str, Any],
) -> None:
    with session_factory(engine)() as s, s.begin():
        s.add(LiveEvent(
            event_id=new_id(), at=at, opportunity_id=opportunity_id, kind=kind, actor=actor,
            policy_version=policy_version, detail_json=dumps(detail),
        ))


def events_for(engine: Engine, opportunity_id: str) -> list[dict[str, Any]]:
    stmt = select(LiveEvent).where(LiveEvent.opportunity_id == opportunity_id).order_by(LiveEvent.at)
    with session_factory(engine)() as s:
        return [
            {"at": e.at, "kind": e.kind, "actor": e.actor, "detail": json.loads(e.detail_json)}
            for e in s.execute(stmt).scalars()
        ]


def create_paper(engine: Engine, paper: LivePaper) -> bool:
    """Insert one PAPER position; False when the opportunity already has one."""
    try:
        with session_factory(engine)() as s, s.begin():
            s.add(paper)
    except IntegrityError:
        return False
    return True


def papers(engine: Engine, states: Sequence[str] | None = None) -> list[LivePaper]:
    stmt = select(LivePaper).order_by(LivePaper.created_at.desc())
    if states is not None:
        stmt = stmt.where(LivePaper.state.in_(list(states)))
    with session_factory(engine)() as s:
        rows = list(s.execute(stmt).scalars())
        s.expunge_all()
        return rows


def paper_has(engine: Engine, opportunity_id: str) -> bool:
    stmt = select(LivePaper.paper_id).where(LivePaper.opportunity_id == opportunity_id)
    with session_factory(engine)() as s:
        return s.execute(stmt).first() is not None


def move_paper(engine: Engine, paper_id: str, expected_state: str, **values: Any) -> bool:
    """Compare-and-set on state, so two cycles cannot both fill or both close."""
    stmt = (
        update(LivePaper)
        .where(LivePaper.paper_id == paper_id, LivePaper.state == expected_state)
        .values(**values)
    )
    with engine.begin() as conn:
        return conn.execute(stmt).rowcount == 1


def add_manual(engine: Engine, row: LiveManual) -> None:
    with session_factory(engine)() as s, s.begin():
        s.add(row)


def manuals(engine: Engine, limit: int = 50) -> list[LiveManual]:
    stmt = select(LiveManual).order_by(LiveManual.at.desc()).limit(limit)
    with session_factory(engine)() as s:
        rows = list(s.execute(stmt).scalars())
        s.expunge_all()
        return rows


def beat(engine: Engine, at: datetime, status: str, detail: dict[str, Any]) -> None:
    with session_factory(engine)() as s, s.begin():
        s.add(LiveHeartbeat(beat_id=new_id(), at=at, status=status, detail_json=dumps(detail)))


def last_beats(engine: Engine, n: int = 3) -> list[tuple[datetime, str, dict[str, Any]]]:
    stmt = select(LiveHeartbeat).order_by(LiveHeartbeat.at.desc()).limit(n)
    with session_factory(engine)() as s:
        return [(b.at, b.status, json.loads(b.detail_json)) for b in s.execute(stmt).scalars()]
