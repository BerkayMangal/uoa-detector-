"""Two dedicated append-only ownership/result tables; JSON stays at this boundary."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import Date, DateTime, String, Table, Text, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.alpha_lab.db import digest, dumps
from webapp.board.db import AlfaBase, session_factory
from webapp.news_pilot import report
from webapp.news_research import config


class PilotClaim(AlfaBase):
    __tablename__ = "alfa_news_pilot_claim"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    profile_hash: Mapped[str] = mapped_column(String)
    implementation_hash: Mapped[str] = mapped_column(String)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PilotResult(AlfaBase):
    __tablename__ = "alfa_news_pilot_result"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature: Mapped[str] = mapped_column(String)


TABLES = (PilotClaim, PilotResult)


def ensure(engine: Engine) -> None:
    for model in TABLES:
        cast(Table, model.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(model.__tablename__ in names for model in TABLES)


def _stored(at: datetime) -> datetime:
    # SQLite strips tzinfo from UTC DateTime columns; external clocks stay strict.
    return report.aware(at.replace(tzinfo=UTC) if at.tzinfo is None else at)


def identity(day: date) -> str:
    return digest((report.VERSION, day.isoformat(), config.profile_hash(), config.implementation_hash()))


def _put(engine: Engine, model: type[AlfaBase], values: dict[str, Any]) -> bool:
    constructor = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    table = cast(Table, model.__table__)
    with engine.begin() as connection:
        return connection.execute(constructor(table).values(values).on_conflict_do_nothing(
            index_elements=["key"]).returning(table.c.key)).first() is not None


def claim(engine: Engine, day: date, now: datetime) -> str | None:
    report.validate_day(day, now)
    key = identity(day)
    inserted = _put(engine, PilotClaim, dict(key=key, day=day, profile_hash=config.profile_hash(),
                    implementation_hash=config.implementation_hash(), claimed_at=report.aware(now)))
    return key if inserted else None


def _resolve(engine: Engine, key: str, now: datetime, summary: dict[str, Any] | None,
             reason: str | None) -> bool:
    at = report.aware(now)
    with session_factory(engine)() as session:
        owned = session.get(PilotClaim, key)
    if (owned is None or key != identity(owned.day) or _stored(owned.claimed_at) > at
            or owned.profile_hash != config.profile_hash() or owned.implementation_hash != config.implementation_hash()):
        raise ValueError("pilot_claim_unverified")
    if summary is not None:
        report.validate_summary(summary, at)
        if summary["day"] != owned.day.isoformat():
            raise ValueError("pilot_claim_day_mismatch")
    status = "completed" if summary is not None else "failed"
    body = dumps(summary) if summary is not None else None
    signature = digest((key, status, at.isoformat(), reason, body))
    return _put(engine, PilotResult, dict(key=key, status=status, recorded_at=at, reason=reason,
                                         summary_json=body, signature=signature))


def complete(engine: Engine, key: str, summary: dict[str, Any], now: datetime) -> bool:
    return _resolve(engine, key, now, summary, None)


def fail(engine: Engine, key: str, reason: str, now: datetime) -> bool:
    if reason not in {"source_export_refused", "source_limit_exceeded", "source_unavailable",
                      "evaluation_failed", "storage_failed", "export_busy"}:
        reason = "evaluation_failed"
    return _resolve(engine, key, now, None, reason)


def read(engine: Engine, now: datetime, limit: int = 10) -> list[dict[str, Any]]:
    at = report.aware(now)
    if not 1 <= limit <= 100:
        raise ValueError("pilot_read_limit_invalid")
    names = set(inspect(engine).get_table_names())
    if any(model.__tablename__ not in names for model in TABLES):
        return []
    with session_factory(engine)() as session:
        rows = session.execute(select(PilotClaim, PilotResult).outerjoin(PilotResult,
            PilotResult.key == PilotClaim.key).where(PilotClaim.claimed_at <= at,
            PilotClaim.profile_hash == config.profile_hash(),
            PilotClaim.implementation_hash == config.implementation_hash())
            .order_by(PilotClaim.day.desc(), PilotClaim.key).limit(limit)).all()
    output = []
    for owned, resolved in rows:
        row: dict[str, Any] = dict(day=owned.day.isoformat(), status="pending",
            recorded_at=_stored(owned.claimed_at).isoformat(), summary=None, reason=None,
            key=owned.key, profile_hash=owned.profile_hash, implementation_hash=owned.implementation_hash)
        if owned.key != identity(owned.day):
            row.update(status="failed", reason="claim_identity_unverified")
        elif resolved is not None and _stored(resolved.recorded_at) <= at:
            valid = _stored(resolved.recorded_at) >= _stored(owned.claimed_at) and resolved.signature == digest((resolved.key, resolved.status,
                _stored(resolved.recorded_at).isoformat(), resolved.reason, resolved.summary_json))
            try:
                summary = (report.validate_summary(json.loads(resolved.summary_json), at)
                           if resolved.summary_json is not None else None)
                valid = valid and ((resolved.status == "completed" and summary is not None)
                                   or (resolved.status == "failed" and summary is None))
                valid = valid and (summary is None or summary["day"] == owned.day.isoformat())
            except (ValueError, TypeError):
                valid, summary = False, None
            row.update(status=resolved.status if valid else "failed",
                       recorded_at=_stored(resolved.recorded_at).isoformat(),
                       summary=summary if valid else None,
                       reason=resolved.reason if valid else "result_identity_unverified")
        output.append(row)
    return output
