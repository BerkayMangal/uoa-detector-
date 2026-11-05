"""Append-only timed measurement ownership and physically confirmed evidence."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    case,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.board.quota_ledger import AlfaUwQuota
from webapp.opening.model import settings as opening_settings

from .config import profile_hash, settings
from .model import (
    Activation,
    Assessment,
    Attempt,
    Completion,
    Halt,
    Leg,
    LegEvidence,
    LegInput,
    OpenAttempts,
    Reservation,
    Target,
    aware,
    canonical,
    digest,
    stored,
)


class TimedActivation(AlfaBase):
    __tablename__ = "alfa_timed_activation"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String, index=True)
    opening_profile_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TimedTarget(AlfaBase):
    __tablename__ = "alfa_timed_target"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    activation_key: Mapped[str] = mapped_column(String, index=True)
    profile_hash: Mapped[str] = mapped_column(String, index=True)
    decision_key: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    eligible: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String)


class TimedAttempt(AlfaBase):
    __tablename__ = "alfa_timed_attempt"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    target_key: Mapped[str] = mapped_column(String, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("target_key", "ordinal"),)


class TimedRequestSlot(AlfaBase):
    __tablename__ = "alfa_timed_request_slot"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    profile_hash: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    attempt_key: Mapped[str] = mapped_column(String, index=True)
    leg: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("attempt_key", "leg"),)


class TimedPacket(AlfaBase):
    __tablename__ = "alfa_timed_packet"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_key: Mapped[str] = mapped_column(String, index=True)
    leg: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String)
    packet_hash: Mapped[str] = mapped_column(String)
    body_kind: Mapped[str] = mapped_column(String)
    compressed_body: Mapped[bytes] = mapped_column(LargeBinary)
    payload_json: Mapped[str] = mapped_column(Text)


class TimedReceipt(AlfaBase):
    __tablename__ = "alfa_timed_receipt"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    parent_key: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    parent_hash: Mapped[str] = mapped_column(String)
    receipt_hash: Mapped[str] = mapped_column(String)


class TimedCompletion(AlfaBase):
    __tablename__ = "alfa_timed_completion"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_key: Mapped[str] = mapped_column(String, index=True)
    target_key: Mapped[str] = mapped_column(String, index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)


class TimedAssessment(AlfaBase):
    __tablename__ = "alfa_timed_assessment"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    target_key: Mapped[str] = mapped_column(String, index=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String)


class TimedDayHalt(AlfaBase):
    __tablename__ = "alfa_timed_day_halt"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)


TABLES = (TimedActivation, TimedTarget, TimedAttempt, TimedRequestSlot, TimedPacket, TimedReceipt, TimedCompletion, TimedAssessment, TimedDayHalt)


def _insert(connection: Connection, model: type[AlfaBase], values: dict[str, Any]) -> bool:
    # Any is restricted to the SQL/JSON serialization boundary.
    constructor = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    table = cast(Table, model.__table__)
    return connection.execute(constructor(table).values(values).on_conflict_do_nothing().returning(table.c.key)).first() is not None


def _put(engine: Engine, model: type[AlfaBase], values: dict[str, Any]) -> bool:
    with engine.begin() as connection:
        return _insert(connection, model, values)


def ensure(engine: Engine) -> None:
    for model in TABLES:
        cast(Table, model.__table__).create(engine, checkfirst=True)
    cast(Table, AlfaUwQuota.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(t.__tablename__ in names for t in TABLES)


def _receipt_key(kind: str, parent: str, stage: str) -> str:
    return digest((kind, parent, stage))


def _confirm(engine: Engine, kind: str, parent: str, checksum: str, lower: datetime,
             clock: Callable[[], datetime]) -> tuple[datetime, datetime]:
    prior = aware(lower)
    for stage in ("post_commit", "confirmed"):
        at = aware(clock())  # AFTER preceding transaction context has returned.
        if at < prior:
            raise ValueError("physical confirmation precedes source/receipt")
        key = _receipt_key(kind, parent, stage)
        _put(engine, TimedReceipt, dict(key=key, parent_key=parent, kind=kind, stage=stage, at=at, parent_hash=checksum,
                                       receipt_hash=digest((key, parent, kind, stage, at.isoformat(), checksum))))
        with session_factory(engine)() as session:
            row = session.get(TimedReceipt, key)
            if row is None or row.parent_hash != checksum:
                raise ValueError("confirmation integrity failure")
            prior = stored(row.at)
    confirmed = confirmation(engine, kind, parent, checksum, datetime.max.replace(tzinfo=UTC))
    if confirmed is None:
        raise ValueError("confirmation missing")
    return confirmed


def confirmation(engine: Engine, kind: str, parent: str, checksum: str, asof: datetime) -> tuple[datetime, datetime] | None:
    with session_factory(engine)() as session:
        rows = [session.get(TimedReceipt, _receipt_key(kind, parent, stage)) for stage in ("post_commit", "confirmed")]
        if any(r is None or r.parent_key != parent or r.kind != kind or r.parent_hash != checksum
               or r.receipt_hash != digest((r.key, parent, kind, r.stage, stored(r.at).isoformat(), checksum)) for r in rows):
            return None
        first, second = rows
        assert first is not None and second is not None
        a, b = stored(first.at), stored(second.at)
        return (a, b) if a <= b <= aware(asof) else None


def activate(engine: Engine, clock: Callable[[], datetime] | None = None) -> Activation:
    clock = clock or (lambda: datetime.now(UTC))
    capture, opening = profile_hash(), opening_settings().profile_hash
    key = digest((capture, opening))
    at = aware(clock())
    _put(engine, TimedActivation, dict(key=key, profile_hash=capture, opening_profile_hash=opening, created_at=at))
    with session_factory(engine)() as session:
        row = session.get(TimedActivation, key)
        assert row is not None
        created = stored(row.created_at)
    checksum = digest((key, capture, opening, created.isoformat()))
    _, confirmed = _confirm(engine, "activation", key, checksum, created, clock)
    return Activation(key=key, profile_hash=capture, opening_profile_hash=opening, activated_at=confirmed)


def activation(engine: Engine, asof: datetime) -> Activation | None:
    if not available(engine):
        return None
    capture, opening = profile_hash(), opening_settings().profile_hash
    key = digest((capture, opening))
    with session_factory(engine)() as session:
        row = session.get(TimedActivation, key)
        if row is None or row.profile_hash != capture or row.opening_profile_hash != opening:
            return None
        created = stored(row.created_at)
        checksum = digest((key, capture, opening, created.isoformat()))
    confirmed = confirmation(engine, "activation", key, checksum, asof)
    return Activation(key=key, profile_hash=capture, opening_profile_hash=opening, activated_at=confirmed[1]) if confirmed and confirmed[0] >= created else None


def save_target(engine: Engine, target: Target) -> bool:
    body = target.model_dump_json()
    return _put(engine, TimedTarget, dict(key=target.key, activation_key=target.activation_key, profile_hash=target.profile_hash,
        decision_key=target.decision_key, day=target.day,
        discovered_at=target.discovered_at, target_at=target.target_at, eligible=target.eligible, payload_json=body, payload_hash=digest(body)))


def _target_value(row: TimedTarget) -> Target:
    if row.payload_hash != digest(row.payload_json):
        raise ValueError("target payload integrity failure")
    value = Target.model_validate_json(row.payload_json)
    if (value.key != row.key or value.activation_key != row.activation_key or value.profile_hash != row.profile_hash
            or value.decision_key != row.decision_key or value.day != row.day or value.eligible != row.eligible
            or value.discovered_at != stored(row.discovered_at) or value.target_at != stored(row.target_at)):
        raise ValueError("target metadata integrity failure")
    return value


def targets(engine: Engine, now: datetime, day: date | None = None, *, decision_key: str | None = None,
            target_keys: Sequence[str] | None = None, activation_key: str | None = None) -> tuple[Target, ...]:
    with session_factory(engine)() as session:
        key = activation_key or digest((profile_hash(), opening_settings().profile_hash))
        query = select(TimedTarget).where(TimedTarget.discovered_at <= aware(now), TimedTarget.activation_key == key,
                                         TimedTarget.profile_hash == profile_hash())
        if day is not None:
            query = query.where(TimedTarget.day == day)
        if decision_key is not None:
            query = query.where(TimedTarget.decision_key == decision_key)
        if target_keys is not None:
            query = query.where(TimedTarget.key.in_(target_keys))
        rows = session.scalars(query.order_by(TimedTarget.target_at.desc(), TimedTarget.key).limit(settings().read_limit)).all()
        return tuple(_target_value(r) for r in rows)


def target_count(engine: Engine, now: datetime, day: date | None = None, *, decision_key: str | None = None) -> int:
    with session_factory(engine)() as session:
        key = digest((profile_hash(), opening_settings().profile_hash))
        query = select(func.count()).select_from(TimedTarget).where(TimedTarget.discovered_at <= aware(now),
            TimedTarget.activation_key == key, TimedTarget.profile_hash == profile_hash())
        if day is not None:
            query = query.where(TimedTarget.day == day)
        if decision_key is not None:
            query = query.where(TimedTarget.decision_key == decision_key)
        return int(session.scalar(query) or 0)


def attempts(engine: Engine, target_key: str) -> tuple[Attempt, ...]:
    with session_factory(engine)() as session:
        rows = session.scalars(select(TimedAttempt).where(TimedAttempt.target_key == target_key).order_by(TimedAttempt.ordinal).limit(settings().max_attempts)).all()
        return tuple(Attempt(key=r.key, target_key=r.target_key, ordinal=r.ordinal, claimed_at=stored(r.claimed_at)) for r in rows)


def read_open_attempts(engine: Engine, active: Activation, day: date, asof: datetime) -> OpenAttempts:
    asof = aware(asof)
    if activation(engine, asof) != active:
        raise ValueError("open-attempt activation unverified")
    with session_factory(engine)() as session:
        conditions = (TimedTarget.activation_key == active.key, TimedTarget.profile_hash == active.profile_hash,
                      TimedTarget.day == day, TimedTarget.discovered_at <= asof, TimedAttempt.claimed_at <= asof)
        pairs = session.execute(select(TimedAttempt, TimedTarget).join(TimedTarget, TimedTarget.key == TimedAttempt.target_key)
            .where(*conditions)
            .order_by(TimedAttempt.claimed_at.desc(), TimedAttempt.key).limit(settings().read_limit)).all()
        total = session.scalar(select(func.count()).select_from(TimedAttempt)
            .join(TimedTarget, TimedTarget.key == TimedAttempt.target_key).where(*conditions)) or 0
        result = []
        for claimed, row in pairs[:settings().read_limit]:
            target = _target_value(row)
            attempt = Attempt(key=claimed.key, target_key=claimed.target_key, ordinal=claimed.ordinal, claimed_at=stored(claimed.claimed_at))
            if (target.key != row.key or target.activation_key != active.key or target.profile_hash != active.profile_hash
                    or target.opening_profile_hash != active.opening_profile_hash or target.day != day
                    or target.published_at < active.activated_at or target.discovered_at != stored(row.discovered_at)
                    or target.discovered_at > attempt.claimed_at or not target.eligible
                    or attempt.key != digest((target.key, attempt.ordinal))):
                raise ValueError("open-attempt target binding unverified")
            done = completions(engine, target.key, asof)
            if not any(c.attempt_key == attempt.key for c in done):
                result.append((attempt, target))
        return OpenAttempts(rows=tuple(result), limited=total > len(pairs))


def open_attempts(engine: Engine, now: datetime) -> OpenAttempts:
    now = aware(now)
    active = activation(engine, now)
    if active is None:
        return OpenAttempts(rows=(), limited=False)
    return read_open_attempts(engine, active, now.astimezone(ZoneInfo("America/New_York")).date(), now)


def claim(engine: Engine, target: Target, ordinal: int, at: datetime) -> Attempt | None:
    at = aware(at)
    active = activation(engine, at)
    if (active is None or target.activation_key != active.key or target.profile_hash != active.profile_hash
            or target.opening_profile_hash != active.opening_profile_hash or target.published_at < active.activated_at
            or target.discovered_at > at):
        return None
    if (not target.eligible or ordinal not in range(1, settings().max_attempts+1)
            or at < target.target_at+timedelta(seconds=settings().attempt_offsets_seconds[ordinal-1]) or at > target.deadline):
        return None
    with session_factory(engine)() as session:
        row = session.get(TimedTarget, target.key)
        if row is None or _target_value(row) != target or session.get(TimedAssessment, target.key) is not None:
            return None
    existing = attempts(engine, target.key)
    if ordinal > 1:
        if not any(a.ordinal == ordinal-1 for a in existing):
            return None
        previous = next((c for c in completions(engine, target.key, at) if c.ordinal == ordinal-1), None)
        if previous is None or at < previous.confirmed_at+timedelta(seconds=settings().attempt_spacing_seconds):
            return None
    key = digest((target.key, ordinal))
    if not _put(engine, TimedAttempt, dict(key=key, target_key=target.key, ordinal=ordinal, claimed_at=at)):
        return None
    return Attempt(key=key, target_key=target.key, ordinal=ordinal, claimed_at=at)


class _DeniedError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def reserve_leg(engine: Engine, attempt: Attempt, leg: Leg, ticker: str, at: datetime,
                observed: int | None = None, *, shared_cap: int) -> Reservation:
    at = aware(at)
    day = at.astimezone(ZoneInfo("America/New_York")).date()
    cfg = settings()
    observed = observed if isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0 else None
    if observed is not None:
        # Native usage is monotone knowledge, not a request reservation. Retain
        # it even if this module's later slot/request transaction is refused.
        with engine.begin() as floor_connection:
            constructor = pg_insert if floor_connection.dialect.name == "postgresql" else sqlite_insert
            floor_connection.execute(constructor(cast(Table, AlfaUwQuota.__table__)).values(
                et_date=day, reserved=observed, updated_at=at).on_conflict_do_nothing())
            floor_connection.execute(update(AlfaUwQuota).where(AlfaUwQuota.et_date == day).values(
                reserved=case((AlfaUwQuota.reserved < observed, observed), else_=AlfaUwQuota.reserved), updated_at=at))
    try:
        with engine.begin() as connection:
            original = connection.execute(select(TimedAttempt.target_key, TimedAttempt.claimed_at, TimedAttempt.ordinal).where(TimedAttempt.key == attempt.key)).first()
            target_record = connection.execute(select(TimedTarget.payload_json, TimedTarget.payload_hash).where(TimedTarget.key == attempt.target_key)).first()
            raw_target = target_record.payload_json if target_record is not None else None
            if (original is None or original.target_key != attempt.target_key or stored(original.claimed_at) != attempt.claimed_at
                    or original.ordinal != attempt.ordinal or stored(original.claimed_at) > at or raw_target is None):
                raise _DeniedError("claim_unverified")
            if target_record is None or target_record.payload_hash != digest(raw_target):
                raise _DeniedError("target_unverified")
            target = Target.model_validate_json(raw_target)
            if (day != target.day or at > target.deadline or ticker != (target.ticker if leg == "own" else "SPY")):
                raise _DeniedError("leg_binding_invalid")
            if connection.execute(select(TimedDayHalt.key).where(TimedDayHalt.day == day, TimedDayHalt.at <= at,
                    TimedDayHalt.until.is_(None)).limit(1)).first() is not None:
                raise _DeniedError("day_halted")
            if connection.execute(select(TimedDayHalt.key).where(TimedDayHalt.day == day, TimedDayHalt.at <= at,
                    TimedDayHalt.until > at).limit(1)).first() is not None:
                raise _DeniedError("cooldown")
            prior = connection.execute(select(TimedRequestSlot.key).where(TimedRequestSlot.attempt_key == attempt.key, TimedRequestSlot.leg == leg)).first()
            if prior is not None:
                raise _DeniedError("already_reserved")
            slot = connection.execute(select(func.max(TimedRequestSlot.ordinal)).where(TimedRequestSlot.profile_hash == profile_hash(), TimedRequestSlot.day == day)).scalar_one()
            index = int(slot or 0)+1
            slot_key: str | None = None
            for offset in range(cfg.slot_conflict_retries):
                chosen = index+offset
                if chosen > cfg.module_daily_cap:
                    raise _DeniedError("module_cap")
                key = digest((profile_hash(), day.isoformat(), chosen))
                if _insert(connection, TimedRequestSlot, dict(key=key, profile_hash=profile_hash(), day=day, ordinal=chosen,
                    attempt_key=attempt.key, leg=leg, ticker=ticker, reserved_at=at)):
                    slot_key = key
                    break
            if slot_key is None:
                raise _DeniedError("slot_conflict")
            constructor = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
            prior_quota = connection.execute(select(AlfaUwQuota.reserved).where(AlfaUwQuota.et_date == day)).scalar_one_or_none()
            if prior_quota is None and observed is None:
                raise _DeniedError("quota_unknown")
            connection.execute(constructor(cast(Table, AlfaUwQuota.__table__)).values(et_date=day, reserved=0, updated_at=at).on_conflict_do_nothing())
            if observed is not None and isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0:
                connection.execute(update(AlfaUwQuota).where(AlfaUwQuota.et_date == day).values(
                    reserved=case((AlfaUwQuota.reserved < observed, observed), else_=AlfaUwQuota.reserved), updated_at=at))
            changed = connection.execute(update(AlfaUwQuota).where(AlfaUwQuota.et_date == day, AlfaUwQuota.reserved+1 <= shared_cap)
                                         .values(reserved=AlfaUwQuota.reserved+1, updated_at=at))
            if changed.rowcount != 1:
                raise _DeniedError("shared_cap")
            shared = connection.execute(select(AlfaUwQuota.reserved).where(AlfaUwQuota.et_date == day)).scalar_one()
            count = connection.execute(select(func.count()).select_from(TimedRequestSlot).where(TimedRequestSlot.profile_hash == profile_hash(), TimedRequestSlot.day == day)).scalar_one()
        return Reservation(granted=True, reason="reserved", slot_key=slot_key, module_reserved=count, shared_reserved=shared)
    except _DeniedError as denied:
        return Reservation(granted=False, reason=denied.reason)


def record_leg(engine: Engine, attempt: Attempt, value: LegInput,
               clock: Callable[[], datetime] | None = None) -> LegEvidence:
    clock = clock or (lambda: datetime.now(UTC))
    if value.requested_at is not None and value.received_at is not None and value.requested_at > value.received_at:
        raise ValueError("response precedes request")
    if value.wire_attempted and value.requested_at is None:
        raise ValueError("wire attempt lacks request clock")
    if value.status == "ok" and (not value.wire_attempted or value.received_at is None or value.price is None or value.price <= 0 or value.trade_at is None):
        raise ValueError("successful quote lacks source/receipt evidence")
    with session_factory(engine)() as session:
        claimed = session.get(TimedAttempt, attempt.key)
        target_row = session.get(TimedTarget, attempt.target_key)
        if (claimed is None or target_row is None or claimed.target_key != attempt.target_key
                or claimed.ordinal != attempt.ordinal or stored(claimed.claimed_at) != attempt.claimed_at):
            raise ValueError("packet claim unverified")
        bound = _target_value(target_row)
        if value.ticker != (bound.ticker if value.leg == "own" else "SPY"):
            raise ValueError("packet ticker binding mismatch")
        if value.wire_attempted:
            slot = session.scalar(select(TimedRequestSlot).where(TimedRequestSlot.attempt_key == attempt.key,
                                  TimedRequestSlot.leg == value.leg).limit(1))
            if slot is None or value.requested_at is None or stored(slot.reserved_at) > value.requested_at:
                raise ValueError("wire packet lacks prior reservation")
    raw = value.raw_body if value.raw_body is not None else canonical(value.payload)
    if len(raw) > settings().max_body_bytes:
        raise ValueError("packet body bound")
    payload_hash = hashlib.sha256(raw).hexdigest()
    if value.raw_body is not None and value.wire_body_hash is not None and value.wire_body_hash != payload_hash:
        raise ValueError("wire body hash mismatch")
    meta = value.model_dump(mode="json", exclude={"payload", "raw_body"})
    if value.status != "ok":
        meta.update(price=None, trade_at=None)
    key = digest((attempt.key, value.leg))
    checksum = digest((key, attempt.key, meta, payload_hash))
    _put(engine, TimedPacket, dict(key=key, attempt_key=attempt.key, leg=value.leg, ticker=value.ticker,
        received_at=value.received_at, payload_hash=payload_hash, packet_hash=checksum,
        body_kind="wire_raw" if value.raw_body is not None else "canonical_json", compressed_body=gzip.compress(raw, mtime=0), payload_json=canonical(meta).decode()))
    lower = value.received_at or aware(clock())
    _confirm(engine, "packet", key, checksum, lower, clock)
    recorded = evidence(engine, key, datetime.max.replace(tzinfo=UTC))
    if recorded is None:
        raise ValueError("recorded packet cannot be verified")
    return recorded


def _evidence(engine: Engine, key: str, asof: datetime) -> LegEvidence | None:
    with session_factory(engine)() as session:
        row = session.get(TimedPacket, key)
        if row is None:
            return None
        metadata: dict[str, Any] = json.loads(row.payload_json)
        checksum = digest((key, row.attempt_key, metadata, row.payload_hash))
        if checksum != row.packet_hash:
            return None
        with gzip.GzipFile(fileobj=io.BytesIO(row.compressed_body)) as stream:
            raw = stream.read(settings().max_body_bytes+1)
        if len(raw) > settings().max_body_bytes or hashlib.sha256(raw).hexdigest() != row.payload_hash:
            return None
        confirmed = confirmation(engine, "packet", key, checksum, asof)
        if confirmed is None or (row.received_at is not None and stored(row.received_at) > confirmed[0]):
            return None
        allowed = {k: v for k, v in metadata.items() if k not in {"body_bytes"}}
        return LegEvidence(key=key, attempt_key=row.attempt_key, payload_hash=row.payload_hash,
                           receipt_at=confirmed[0], confirmed_at=confirmed[1], **allowed)


def evidence(engine: Engine, key: str, asof: datetime) -> LegEvidence | None:
    try:
        return _evidence(engine, key, asof)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, EOFError, RecursionError, OverflowError):
        # Bounded malformed stored evidence is unknown. Native DB exceptions
        # still propagate to the caller's storage-unavailable boundary.
        return None


def legs(engine: Engine, attempt_key: str, asof: datetime) -> tuple[LegEvidence, ...]:
    return tuple(e for leg in ("own", "spy") if (e := evidence(engine, digest((attempt_key, leg)), asof)) is not None)


def complete(engine: Engine, attempt: Attempt, own: LegEvidence | None, spy: LegEvidence | None, at: datetime,
             reason: str = "", *, own_confirmed_at: datetime | None = None, spy_confirmed_at: datetime | None = None,
             clock: Callable[[], datetime] | None = None) -> Completion:
    at = aware(at)
    known = next((a for a in attempts(engine, attempt.target_key) if a.key == attempt.key), None)
    if known != attempt:
        raise ValueError("completion claim unverified")
    if (own is not None and own.leg != "own") or (spy is not None and spy.leg != "spy"):
        raise ValueError("completion leg mismatch")
    for item in (own, spy):
        if item is not None and (item.attempt_key != attempt.key or evidence(engine, item.key, at) is None):
            raise ValueError("completion packet unverified")
    for item, physical in ((own, own_confirmed_at), (spy, spy_confirmed_at)):
        if physical is not None and (item is None or not item.confirmed_at <= aware(physical) <= at):
            raise ValueError("physical leg confirmation invalid")
    value = Completion(key=attempt.key, attempt_key=attempt.key, target_key=attempt.target_key, ordinal=attempt.ordinal,
        completed_at=at, confirmed_at=at, own_key=own.key if own else None, spy_key=spy.key if spy else None, reason=reason,
        own_confirmed_at=own_confirmed_at, spy_confirmed_at=spy_confirmed_at)
    body = value.model_dump_json()
    _put(engine, TimedCompletion, dict(key=attempt.key, attempt_key=attempt.key, target_key=attempt.target_key,
        completed_at=at, payload_json=body))
    _confirm(engine, "completion", attempt.key, digest(body), at, clock or (lambda: datetime.now(UTC)))
    result = next((c for c in completions(engine, attempt.target_key, datetime.max.replace(tzinfo=UTC)) if c.key == attempt.key), None)
    if result is None:
        raise ValueError("completion unavailable")
    return result


def completions(engine: Engine, target_key: str, now: datetime) -> tuple[Completion, ...]:
    with session_factory(engine)() as session:
        rows = session.scalars(select(TimedCompletion).where(TimedCompletion.target_key == target_key,
            TimedCompletion.completed_at <= now).order_by(TimedCompletion.completed_at).limit(settings().max_attempts)).all()
        result = []
        for row in rows:
            confirmed = confirmation(engine, "completion", row.key, digest(row.payload_json), now)
            if confirmed:
                result.append(Completion.model_validate_json(row.payload_json).model_copy(update={"confirmed_at": confirmed[1]}))
        return tuple(result)


def assess(engine: Engine, value: Assessment) -> bool:
    if value.key != value.target_key:
        raise ValueError("assessment key must identify target")
    with session_factory(engine)() as session:
        target = session.get(TimedTarget, value.target_key)
        if target is None or stored(target.discovered_at) > value.assessed_at:
            raise ValueError("assessment target unverified")
    if value.return_pct is not None and value.own_key is None:
        raise ValueError("return lacks own evidence")
    if value.excess_pp is not None and (value.own_key is None or value.spy_key is None):
        raise ValueError("excess lacks paired evidence")
    body = value.model_dump_json()
    return _put(engine, TimedAssessment, dict(key=value.key, target_key=value.target_key, assessed_at=value.assessed_at,
        payload_json=body, payload_hash=digest(body)))


def assessment(engine: Engine, target_key: str, now: datetime) -> Assessment | None:
    return next(iter(assessments(engine, now, target_keys=(target_key,))), None)


def assessments(engine: Engine, now: datetime, *, target_keys: Sequence[str] | None = None) -> tuple[Assessment, ...]:
    with session_factory(engine)() as session:
        query = select(TimedAssessment).where(TimedAssessment.assessed_at <= now)
        if target_keys is not None:
            query = query.where(TimedAssessment.target_key.in_(target_keys))
        rows = session.scalars(query.order_by(TimedAssessment.assessed_at.desc()).limit(settings().read_limit)).all()
        output = []
        for row in rows:
            if row.payload_hash != digest(row.payload_json):
                raise ValueError("assessment payload integrity failure")
            value = Assessment.model_validate_json(row.payload_json)
            if value.key != row.key or value.target_key != row.target_key or value.assessed_at != stored(row.assessed_at):
                raise ValueError("assessment metadata integrity failure")
            output.append(value)
        return tuple(output)


def halt(engine: Engine, day: date, reason: str, at: datetime) -> None:
    value = Halt(day=day, reason=reason, at=at)
    _put(engine, TimedDayHalt, dict(key=digest((profile_hash(), day.isoformat(), reason)), day=day, at=at,
        until=None, payload_json=value.model_dump_json()))


def halted(engine: Engine, day: date, asof: datetime) -> Halt | None:
    with session_factory(engine)() as session:
        row = session.scalar(select(TimedDayHalt).where(TimedDayHalt.day == day, TimedDayHalt.at <= asof,
                             TimedDayHalt.until.is_(None)).order_by(TimedDayHalt.at).limit(1))
        return Halt.model_validate_json(row.payload_json) if row else None


def cooldown(engine: Engine, day: date, at: datetime, until: datetime) -> None:
    if aware(until) < aware(at)+timedelta(seconds=settings().rate_limit_cooldown_seconds):
        raise ValueError("cooldown too short")
    value = Halt(day=day, reason="rate_limited", at=at, until=until)
    _put(engine, TimedDayHalt, dict(key=digest((profile_hash(), day.isoformat(), "rate_limited", at.isoformat())), day=day,
        at=at, until=until, payload_json=value.model_dump_json()))


def cooling_until(engine: Engine, day: date, asof: datetime) -> datetime | None:
    with session_factory(engine)() as session:
        value = session.scalar(select(func.max(TimedDayHalt.until)).where(TimedDayHalt.day == day,
            TimedDayHalt.at <= asof, TimedDayHalt.until > asof))
        return stored(value) if value else None
