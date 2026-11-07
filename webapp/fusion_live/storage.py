"""Separate append-only decisions with physical post-commit receipts.

Any is confined to the immutable JSON adapter, never a trusted market feature.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, cast

from sqlalchemy import Date, DateTime, String, Table, Text, UniqueConstraint, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.fusion_live import config
from webapp.shadow_capture.model import aware, digest, stored

Kind = Literal["activation", "claim", "decision", "day_result"]


class FusionEvent(AlfaBase):
    __tablename__ = "alfa_fusion_event"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    day: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    candidate_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    arm: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String)


class FusionReceipt(AlfaBase):
    __tablename__ = "alfa_fusion_receipt"
    __table_args__ = (UniqueConstraint("parent_key", "stage"),)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    parent_key: Mapped[str] = mapped_column(String, index=True)
    stage: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str] = mapped_column(String)


@dataclass(frozen=True)
class Record:
    key: str
    kind: str
    at: datetime
    day: date | None
    candidate_key: str | None
    arm: str | None
    payload: dict[str, Any]
    checksum: str
    available_at: datetime | None


def ensure(engine: Engine) -> None:
    for model in (FusionEvent, FusionReceipt):
        cast(Table, model.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(t.__tablename__ in names for t in (FusionEvent, FusionReceipt))


def _insert(engine: Engine, model: type[AlfaBase], values: dict[str, Any]) -> bool:
    table = cast(Table, model.__table__)
    statement = pg_insert(table) if engine.dialect.name == "postgresql" else sqlite_insert(table)
    if engine.dialect.name not in ("postgresql", "sqlite"):
        raise ValueError("unsupported_fusion_storage")
    with engine.begin() as c:
        return (
            c.execute(
                statement.values(values).on_conflict_do_nothing().returning(table.c.key)
            ).first()
            is not None
        )


def _sum(row: FusionEvent) -> str:
    return digest(
        (
            row.key,
            row.kind,
            stored(row.at).isoformat(),
            row.day.isoformat() if row.day else None,
            row.candidate_key,
            row.arm,
            row.payload_json,
        )
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate_fusion_json_key")
        out[k] = v
    return out


def get(engine: Engine, key: str, now: datetime, *, confirmed: bool = True) -> Record | None:
    at = aware(now)
    with session_factory(engine)() as s:
        row = s.get(FusionEvent, key)
        receipts = {
            r.stage: r
            for r in s.scalars(select(FusionReceipt).where(FusionReceipt.parent_key == key))
        }
    if (
        row is None
        or row.kind not in ("activation", "claim", "decision", "day_result")
        or stored(row.at) > at
    ):
        return None
    try:
        body = json.loads(row.payload_json, object_pairs_hook=_pairs)
        if not isinstance(body, dict) or _sum(row) != row.checksum:
            return None
        json.dumps(body, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    first, final = receipts.get("recorded"), receipts.get("confirmed")
    available_at = None
    if first is not None and final is not None:
        t1, t2 = stored(first.at), stored(final.at)
        if (
            first.key == digest((key, "recorded"))
            and final.key == digest((key, "confirmed"))
            and stored(row.at) <= t1 <= t2 <= at
            and first.checksum == digest((key, row.checksum, "recorded", t1.isoformat()))
            and final.checksum
            == digest(
                (key, row.checksum, "confirmed", first.checksum, t1.isoformat(), t2.isoformat())
            )
        ):
            available_at = t2
    if confirmed and available_at is None:
        return None
    return Record(
        row.key,
        row.kind,
        stored(row.at),
        row.day,
        row.candidate_key,
        row.arm,
        body,
        row.checksum,
        available_at,
    )


def _confirm(engine: Engine, row: Record, clock: Callable[[], datetime]) -> Record:
    at = aware(clock())
    if at < row.at:
        raise ValueError("fusion_clock_reversed")
    _insert(
        engine,
        FusionReceipt,
        dict(
            key=digest((row.key, "recorded")),
            parent_key=row.key,
            stage="recorded",
            at=at,
            checksum=digest((row.key, row.checksum, "recorded", at.isoformat())),
        ),
    )
    with session_factory(engine)() as s:
        first = s.get(FusionReceipt, digest((row.key, "recorded")))
    after = aware(clock())
    if first is None or after < stored(first.at) or stored(first.at) < row.at:
        raise ValueError("fusion_receipt_unverified")
    expected = digest((row.key, row.checksum, "recorded", stored(first.at).isoformat()))
    if first.checksum != expected:
        raise ValueError("fusion_receipt_unverified")
    _insert(
        engine,
        FusionReceipt,
        dict(
            key=digest((row.key, "confirmed")),
            parent_key=row.key,
            stage="confirmed",
            at=after,
            checksum=digest(
                (
                    row.key,
                    row.checksum,
                    "confirmed",
                    first.checksum,
                    stored(first.at).isoformat(),
                    after.isoformat(),
                )
            ),
        ),
    )
    result = get(engine, row.key, after)
    if result is None:
        raise ValueError("fusion_confirmation_unverified")
    return result


def put(
    engine: Engine,
    *,
    key: str,
    kind: Kind,
    at: datetime,
    payload: dict[str, Any],
    clock: Callable[[], datetime],
    day: date | None = None,
    candidate_key: str | None = None,
    arm: str | None = None,
) -> Record | None:
    at = aware(at)
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    checksum = digest(
        (key, kind, at.isoformat(), day.isoformat() if day else None, candidate_key, arm, body)
    )
    if not _insert(
        engine,
        FusionEvent,
        dict(
            key=key,
            kind=kind,
            at=at,
            day=day,
            candidate_key=candidate_key,
            arm=arm,
            payload_json=body,
            checksum=checksum,
        ),
    ):
        return None
    return _confirm(
        engine, Record(key, kind, at, day, candidate_key, arm, payload, checksum, None), clock
    )


def list_events(
    engine: Engine,
    kind: Kind,
    now: datetime,
    *,
    day: date | None = None,
    limit: int = 32,
    confirmed: bool = True,
) -> tuple[Record, ...]:
    at = aware(now)
    if not 1 <= limit <= 512:
        raise ValueError("fusion_read_limit")
    with session_factory(engine)() as s:
        query = select(FusionEvent.key).where(FusionEvent.kind == kind, FusionEvent.at <= at)
        if day is not None:
            query = query.where(FusionEvent.day == day)
        keys = list(s.scalars(query.order_by(FusionEvent.at, FusionEvent.key).limit(limit + 1)))
    return tuple(r for key in keys if (r := get(engine, key, at, confirmed=confirmed)) is not None)


def activation_key() -> str:
    return digest(("fusion_activation", config.identity()))


def active(engine: Engine, now: datetime) -> Record | None:
    row = get(engine, activation_key(), now) if available(engine) else None
    return (
        row
        if row is not None and row.kind == "activation" and row.payload == config.identity()
        else None
    )


def activate(engine: Engine, clock: Callable[[], datetime]) -> Record:
    at = aware(clock())
    row = put(
        engine,
        key=activation_key(),
        kind="activation",
        at=at,
        payload=config.identity(),
        clock=clock,
    )
    if row is not None:
        return row
    raw = get(engine, activation_key(), aware(clock()), confirmed=False)
    if raw is None or raw.kind != "activation" or raw.payload != config.identity():
        raise ValueError("fusion_activation_unavailable")
    return raw if raw.available_at is not None else _confirm(engine, raw, clock)
