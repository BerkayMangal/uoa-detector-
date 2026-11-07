"""Append-only shadow records and physical post-commit confirmations.

``Any`` is confined to the immutable JSON persistence adapter. No record payload
is a trusted price until the collector validates its original wire evidence.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import (
    Date,
    DateTime,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.postgresql import Insert as PgInsert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, aliased, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.fundamental_context import settings as config
from webapp.fundamental_context.model import KINDS, Kind, Record, aware, digest, stored


class FundamentalEvent(AlfaBase):
    __tablename__ = "alfa_fundamental_event"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    day: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    parent_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    unique_owner: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    payload_json: Mapped[str] = mapped_column(Text)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    checksum: Mapped[str] = mapped_column(String)


class FundamentalReceipt(AlfaBase):
    __tablename__ = "alfa_fundamental_receipt"
    __table_args__ = (UniqueConstraint("parent_key", "stage"),)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    parent_key: Mapped[str] = mapped_column(String, index=True)
    stage: Mapped[str] = mapped_column(String)
    event_checksum: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str] = mapped_column(String)


TABLES = (FundamentalEvent, FundamentalReceipt)


def ensure(engine: Engine) -> None:
    for model in TABLES:
        cast(Table, model.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(model.__tablename__ in names for model in TABLES)


def _insert(engine: Engine, model: type[AlfaBase], values: dict[str, Any]) -> bool:
    table = cast(Table, model.__table__)
    statement: PgInsert | SqliteInsert
    if engine.dialect.name == "postgresql":
        statement = pg_insert(table)
    elif engine.dialect.name == "sqlite":
        statement = sqlite_insert(table)
    else:
        raise ValueError("unsupported_fundamental_dialect")
    with engine.begin() as connection:
        return (
            connection.execute(
                statement.values(values).on_conflict_do_nothing().returning(table.c.key)
            ).first()
            is not None
        )


def _event_checksum(
    key: str,
    kind: str,
    at: datetime,
    day: date | None,
    parent: str | None,
    owner: str | None,
    body: str,
    blob: bytes | None,
) -> str:
    return digest(
        (
            key,
            kind,
            at.isoformat(),
            day.isoformat() if day else None,
            parent,
            owner,
            body,
            hashlib.sha256(blob).hexdigest() if blob is not None else None,
        )
    )


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("fundamental_duplicate_json_key")
        result[key] = value
    return result


def _raw_blob(blob: bytes | None) -> bytes | None:
    if blob is None:
        return None
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as handle:
        raw = handle.read(config.settings().max_body_bytes + 1)
    if len(raw) > config.settings().max_body_bytes:
        raise ValueError("fundamental_blob_limit")
    return raw


def _receipt_key(parent_key: str, stage: str) -> str:
    return digest((parent_key, "receipt", stage))


def _first_valid(row: FundamentalEvent, receipt: FundamentalReceipt | None, now: datetime) -> bool:
    if receipt is None:
        return False
    at = stored(receipt.at)
    return (
        receipt.stage == "recorded"
        and receipt.key == _receipt_key(row.key, "recorded")
        and receipt.parent_key == row.key
        and receipt.event_checksum == row.checksum
        and receipt.checksum
        == digest(("receipt", "recorded", row.key, row.checksum, at.isoformat()))
        and stored(row.at) <= at <= now
    )


def _confirmed(
    row: FundamentalEvent,
    first: FundamentalReceipt | None,
    final: FundamentalReceipt | None,
    now: datetime,
) -> datetime | None:
    if not _first_valid(row, first, now) or first is None or final is None:
        return None
    at, first_at = stored(final.at), stored(first.at)
    expected = digest(
        (
            "receipt",
            "confirmed",
            row.key,
            row.checksum,
            first.checksum,
            first_at.isoformat(),
            at.isoformat(),
        )
    )
    if (
        final.stage != "confirmed"
        or final.key != _receipt_key(row.key, "confirmed")
        or final.parent_key != row.key
        or final.event_checksum != row.checksum
        or final.checksum != expected
        or not first_at <= at <= now
    ):
        return None
    return at


def _record(
    row: FundamentalEvent,
    first: FundamentalReceipt | None,
    final: FundamentalReceipt | None,
    now: datetime,
    require_receipt: bool,
) -> Record | None:
    at = stored(row.at)
    if row.kind not in KINDS or at > now:
        return None
    try:
        raw = _raw_blob(row.blob)
        expected = _event_checksum(
            row.key, row.kind, at, row.day, row.parent_key, row.unique_owner, row.payload_json, raw
        )
        payload = json.loads(row.payload_json, object_pairs_hook=_object)
        if not isinstance(payload, dict) or row.checksum != expected:
            return None
        # JSON may have been tampered to include non-finite values even when a
        # caller recomputes a digest; it still cannot enter the typed boundary.
        json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError, OSError, EOFError, RecursionError):
        return None
    confirmed = _confirmed(row, first, final, now)
    if require_receipt and confirmed is None:
        return None
    return Record(
        row.key,
        row.kind,
        at,
        row.day,
        row.parent_key,
        row.unique_owner,
        payload,
        row.checksum,
        confirmed,
        raw,
    )


def get(engine: Engine, key: str, now: datetime, require_receipt: bool = True) -> Record | None:
    at = aware(now)
    with session_factory(engine)() as session:
        row = session.get(FundamentalEvent, key)
        receipts = {
            r.stage: r
            for r in session.scalars(
                select(FundamentalReceipt).where(FundamentalReceipt.parent_key == key)
            )
        }
    return (
        _record(row, receipts.get("recorded"), receipts.get("confirmed"), at, require_receipt)
        if row is not None
        else None
    )


def _confirm(engine: Engine, row: Record, clock: Callable[[], datetime]) -> Record:
    # Called only after the parent's insert transaction committed. Existing
    # records are eligible here solely for activation recovery, never prices.
    at = aware(clock())
    if at < row.at:
        raise ValueError("fundamental_receipt_before_event")
    _insert(
        engine,
        FundamentalReceipt,
        dict(
            key=_receipt_key(row.key, "recorded"),
            stage="recorded",
            parent_key=row.key,
            event_checksum=row.checksum,
            at=at,
            checksum=digest(("receipt", "recorded", row.key, row.checksum, at.isoformat())),
        ),
    )
    with session_factory(engine)() as session:
        first = session.get(FundamentalReceipt, _receipt_key(row.key, "recorded"))
        parent = session.get(FundamentalEvent, row.key)
    # This clock is after the first receipt transaction committed. Never use
    # its pre-commit stamp to start candidate/entry horizons.
    confirmed_at = aware(clock())
    if parent is None or not _first_valid(parent, first, confirmed_at) or first is None:
        raise ValueError("fundamental_recorded_receipt_unverified")
    _insert(
        engine,
        FundamentalReceipt,
        dict(
            key=_receipt_key(row.key, "confirmed"),
            stage="confirmed",
            parent_key=row.key,
            event_checksum=row.checksum,
            at=confirmed_at,
            checksum=digest(
                (
                    "receipt",
                    "confirmed",
                    row.key,
                    row.checksum,
                    first.checksum,
                    stored(first.at).isoformat(),
                    confirmed_at.isoformat(),
                )
            ),
        ),
    )
    confirmed = get(engine, row.key, confirmed_at)
    if confirmed is None:
        raise ValueError("fundamental_confirmation_unverified")
    return confirmed


def put(
    engine: Engine,
    *,
    key: str,
    kind: Kind,
    at: datetime,
    payload: dict[str, Any],
    day: date | None = None,
    parent_key: str | None = None,
    unique_owner: str | None = None,
    blob: bytes | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Record | None:
    moment = aware(at)
    if (
        not isinstance(key, str)
        or not key
        or kind not in KINDS
        or not isinstance(payload, dict)
        or (day is not None and type(day) is not date)
        or any(
            value is not None and not isinstance(value, str) for value in (parent_key, unique_owner)
        )
    ):
        raise ValueError("fundamental_record_invalid")
    if blob is not None and (
        not isinstance(blob, bytes) or len(blob) > config.settings().max_body_bytes
    ):
        raise ValueError("fundamental_blob_limit")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    checksum = _event_checksum(key, kind, moment, day, parent_key, unique_owner, body, blob)
    inserted = _insert(
        engine,
        FundamentalEvent,
        dict(
            key=key,
            kind=kind,
            at=moment,
            day=day,
            parent_key=parent_key,
            unique_owner=unique_owner,
            payload_json=body,
            blob=gzip.compress(blob, mtime=0) if blob is not None else None,
            checksum=checksum,
        ),
    )
    if not inserted:
        return None
    record = Record(key, kind, moment, day, parent_key, unique_owner, payload, checksum, None, blob)
    return _confirm(engine, record, clock or (lambda: datetime.now(UTC)))


def list_events(
    engine: Engine,
    kind: Kind,
    now: datetime,
    *,
    day: date | None = None,
    parent_key: str | None = None,
    limit: int = 16,
    require_receipt: bool = True,
) -> tuple[Record, ...]:
    at = aware(now)
    if (
        kind not in KINDS
        or type(limit) is not int
        or not 1 <= limit <= config.settings().read_limit + 1
    ):
        raise ValueError("fundamental_read_limit")
    first, final = aliased(FundamentalReceipt), aliased(FundamentalReceipt)
    query = (
        select(FundamentalEvent, first, final)
        .outerjoin(first, and_(first.parent_key == FundamentalEvent.key, first.stage == "recorded"))
        .outerjoin(
            final, and_(final.parent_key == FundamentalEvent.key, final.stage == "confirmed")
        )
        .where(FundamentalEvent.kind == kind, FundamentalEvent.at <= at)
    )
    if day is not None:
        query = query.where(FundamentalEvent.day == day)
    if parent_key is not None:
        query = query.where(FundamentalEvent.parent_key == parent_key)
    with session_factory(engine)() as session:
        rows = session.execute(
            query.order_by(FundamentalEvent.at, FundamentalEvent.key).limit(limit)
        ).all()
    return tuple(
        record
        for row, first_row, final_row in rows
        if (record := _record(row, first_row, final_row, at, require_receipt)) is not None
    )


def count(
    engine: Engine,
    kind: Kind,
    now: datetime,
    *,
    day: date | None = None,
    include_future: bool = False,
) -> int:
    """Conservative raw ownership count, including unconfirmed records."""
    at = aware(now)
    if kind not in KINDS:
        raise ValueError("fundamental_record_kind")
    if type(include_future) is not bool or (include_future and day is None):
        raise ValueError("fundamental_future_scope_required")
    query = select(func.count()).select_from(FundamentalEvent).where(FundamentalEvent.kind == kind)
    if not include_future:
        query = query.where(FundamentalEvent.at <= at)
    if day is not None:
        query = query.where(FundamentalEvent.day == day)
    with session_factory(engine)() as session:
        return session.scalar(query) or 0


def _activation_key() -> str:
    return digest(("activation", config.identity()))


def activate(engine: Engine, clock: Callable[[], datetime]) -> Record:
    now = aware(clock())
    key, payload = _activation_key(), config.identity()
    existing = get(engine, key, now)
    if existing is not None:
        if existing.kind != "activation" or existing.payload != payload:
            raise ValueError("fundamental_activation_identity")
        return existing
    inserted = put(engine, key=key, kind="activation", at=now, payload=payload, clock=clock)
    if inserted is not None:
        return inserted
    raw = get(engine, key, aware(clock()), require_receipt=False)
    if raw is None or raw.kind != "activation" or raw.payload != payload:
        raise ValueError("fundamental_activation_unverified")
    return _confirm(engine, raw, clock)


def active(engine: Engine, now: datetime) -> Record | None:
    now = aware(now)
    if not available(engine):
        return None
    record = get(engine, _activation_key(), now)
    return (
        record
        if record is not None
        and record.kind == "activation"
        and record.payload == config.identity()
        else None
    )
