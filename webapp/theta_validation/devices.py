"""Revocable continuous Theta devices, separate from immutable v1 sessions.

Only hashes of bearer and one-use enrollment tokens are persisted. Wire JSON
uses Any at the existing storage/service presentation boundary only.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, String, Table, Text, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.theta_validation import service, storage

TOKEN = re.compile(r"[0-9a-f]{64}")
DEVICE_ID = re.compile(r"thetadev_[0-9a-f]{32}")
RESULT_PATH = re.compile(r"/api/theta/device/results/theta_[0-9a-f]{32}")
GRANT_LIFETIME = timedelta(hours=24)
HEARTBEAT_FRESHNESS_SECONDS = 180
MAX_DEVICES_IN_STATUS = 32


class ThetaDeviceGrant(AlfaBase):
    __tablename__ = "alfa_theta_device_grant"
    grant_id: Mapped[str] = mapped_column(String, primary_key=True)
    token_sha256: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ThetaDevice(AlfaBase):
    __tablename__ = "alfa_theta_device"
    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    grant_id: Mapped[str] = mapped_column(String, unique=True)
    token_sha256: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_upload_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_receipt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validated_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True)
class IssuedGrant:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedDevice:
    token: str
    device_id: str


class HeartbeatSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    connector_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$", max_length=32)
    terminal_state: Literal["reachable", "unreachable", "authentication_failed", "unknown"]
    last_error: Literal[
        "terminal_unreachable", "authentication_failed", "unsupported_payload",
        "request_failed", "response_too_large", "invalid_request",
        "upload_failed", "queue_unreachable",
    ] | None = None
    outbox_count: int = Field(default=0, ge=0, le=256, strict=True)


def ensure(engine: Engine) -> None:
    for model in (ThetaDeviceGrant, ThetaDevice):
        cast(Table, model.__table__).create(engine, checkfirst=True)


def _clock(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_device_clock_required")
    return now.astimezone(UTC)


def _hash(raw: str) -> str | None:
    if not isinstance(raw, str) or TOKEN.fullmatch(raw) is None:
        return None
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def authorized_path(method: str, path: str) -> bool:
    """Exact device authority only; no prefix, trailing slash or owner route."""
    return (method == "GET" and path == "/api/theta/device/jobs") or (
        method == "POST"
        and (path == "/api/theta/device/heartbeat" or RESULT_PATH.fullmatch(path) is not None)
    )


def issue(engine: Engine, now: datetime) -> IssuedGrant:
    moment = _clock(now)
    raw = secrets.token_hex(32)
    checksum = _hash(raw)
    assert checksum is not None
    expires = moment + GRANT_LIFETIME
    with session_factory(engine)() as session, session.begin():
        session.add(ThetaDeviceGrant(grant_id=uuid.uuid4().hex, token_sha256=checksum,
                                     created_at=moment, expires_at=expires))
    return IssuedGrant(raw, expires)


def redeem(engine: Engine, raw: str, now: datetime) -> IssuedDevice:
    moment = _clock(now)
    checksum = _hash(raw)
    if checksum is None:
        raise ValueError("invalid_device_pairing_token")
    with session_factory(engine)() as session:
        grant = session.scalar(select(ThetaDeviceGrant).where(
            ThetaDeviceGrant.token_sha256 == checksum).with_for_update())
        if grant is None:
            raise ValueError("invalid_device_pairing_token")
        if not storage.utc(grant.created_at) <= moment < storage.utc(grant.expires_at):
            raise ValueError("device_pairing_token_expired")
        if session.scalar(select(ThetaDevice.device_id).where(ThetaDevice.grant_id == grant.grant_id)):
            raise ValueError("device_pairing_token_consumed")
        token = secrets.token_hex(32)
        token_hash = _hash(token)
        assert token_hash is not None
        device_id = "thetadev_" + uuid.uuid4().hex
        session.add(ThetaDevice(device_id=device_id, grant_id=grant.grant_id,
                               token_sha256=token_hash, created_at=moment))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise ValueError("device_pairing_token_consumed") from None
    return IssuedDevice(token, device_id)


def _active(session: Session, device_id: str, now: datetime, *, lock: bool = False) -> ThetaDevice | None:
    if DEVICE_ID.fullmatch(device_id) is None:
        return None
    query = select(ThetaDevice).where(ThetaDevice.device_id == device_id)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None or row.revoked_at is not None or storage.utc(row.created_at) > now:
        return None
    return row


def authenticate(engine: Engine, raw: str, now: datetime) -> str | None:
    moment = _clock(now)
    checksum = _hash(raw)
    if checksum is None:
        return None
    with session_factory(engine)() as session:
        row = session.scalar(select(ThetaDevice).where(ThetaDevice.token_sha256 == checksum))
        if row is None or row.revoked_at is not None or storage.utc(row.created_at) > moment:
            return None
        return row.device_id


def require_active(engine: Engine, device_id: str, now: datetime) -> None:
    moment = _clock(now)
    with session_factory(engine)() as session:
        if _active(session, device_id, moment) is None:
            raise ValueError("device_unauthorized")


def revoke(engine: Engine, device_id: str, now: datetime) -> dict[str, Any]:
    moment = _clock(now)
    if DEVICE_ID.fullmatch(device_id) is None:
        raise ValueError("device_not_found")
    with session_factory(engine)() as session, session.begin():
        row = session.scalar(select(ThetaDevice).where(ThetaDevice.device_id == device_id).with_for_update())
        if row is None:
            raise ValueError("device_not_found")
        if row.revoked_at is None:
            row.revoked_at = moment
        return {"device_id": row.device_id, "revoked_at": storage.utc(row.revoked_at).isoformat(), "state": "revoked"}


def heartbeat(engine: Engine, device_id: str, spec: HeartbeatSpec, now: datetime) -> dict[str, Any]:
    moment = _clock(now)
    with session_factory(engine)() as session, session.begin():
        row = _active(session, device_id, moment, lock=True)
        if row is None:
            raise ValueError("device_unauthorized")
        if row.last_heartbeat_at is not None and storage.utc(row.last_heartbeat_at) > moment:
            raise ValueError("heartbeat_clock_before_previous")
        row.last_heartbeat_at = moment
        row.heartbeat_json = spec.model_dump_json()
    return {"device_id": device_id, "received_at": moment.isoformat(),
            "heartbeat_freshness_seconds": HEARTBEAT_FRESHNESS_SECONDS,
            "terminal_health_source": "device_self_report"}


def jobs(engine: Engine, device_id: str, now: datetime, limit: int = 4) -> dict[str, Any]:
    moment = _clock(now)
    if not 1 <= limit <= 4:
        raise ValueError("invalid_device_job_limit")
    require_active(engine, device_id, moment)
    return {"status": "device_authorized", "as_of": moment.isoformat(),
            "jobs": storage.pending(engine, moment, limit)}


def ingest(engine: Engine, device_id: str, request_id: str, upload: service.UploadSpec,
           now: datetime) -> dict[str, Any]:
    moment = _clock(now)
    with session_factory(engine)() as session, session.begin():
        row = _active(session, device_id, moment, lock=True)
        if row is None:
            raise ValueError("device_unauthorized")
        if upload.status in {"terminal_unreachable", "authentication_failed"}:
            raise ValueError("report_terminal_health_without_consuming_job")
        # Recheck after body parsing, and serialize with revocation on Postgres.
        # The existing service alone validates native quotes and computes PnL.
        receipt = service.ingest(engine, request_id, upload, moment)
        received = storage.utc(datetime.fromisoformat(receipt["received_at"]))
        row.last_upload_at = moment
        if row.last_receipt_at is None or received > storage.utc(row.last_receipt_at):
            row.last_receipt_at = received
        result = receipt["result"]
        if isinstance(result, dict) and isinstance(result.get("entry_quote"), dict) and (
            row.last_validated_data_at is None or received > storage.utc(row.last_validated_data_at)
        ):
            row.last_validated_data_at = received
        return receipt


def _iso(value: datetime | None) -> str | None:
    return storage.utc(value).isoformat() if value is not None else None


def _device_view(row: ThetaDevice, now: datetime, pending: bool) -> dict[str, Any]:
    health: dict[str, Any] = json.loads(row.heartbeat_json) if row.heartbeat_json else {}
    age = (now - storage.utc(row.last_heartbeat_at)).total_seconds() if row.last_heartbeat_at else None
    online = row.revoked_at is None and age is not None and 0 <= age <= HEARTBEAT_FRESHNESS_SECONDS
    state = "revoked" if row.revoked_at else "awaiting_connection" if age is None else (
        "idle" if online and not pending and health.get("outbox_count", 0) == 0 else "online" if online else "stale"
    )
    return {"device_id": row.device_id, "created_at": _iso(row.created_at),
            "revoked_at": _iso(row.revoked_at), "state": state, "bridge_online": online,
            "last_heartbeat_at": _iso(row.last_heartbeat_at), "heartbeat_age_seconds": age,
            "terminal_state": health.get("terminal_state", "unknown"),
            "terminal_health_source": "device_self_report",
            "connector_version": health.get("connector_version"), "last_error": health.get("last_error"),
            "outbox_count": health.get("outbox_count", 0),
            "last_upload_at": _iso(row.last_upload_at), "last_receipt_at": _iso(row.last_receipt_at),
            "last_validated_data_at": _iso(row.last_validated_data_at)}


def status(engine: Engine, now: datetime) -> dict[str, Any]:
    moment = _clock(now)
    if not {ThetaDeviceGrant.__tablename__, ThetaDevice.__tablename__}.issubset(inspect(engine).get_table_names()):
        return {"status": "storage_unavailable", "as_of": moment.isoformat(), "devices": []}
    pending = bool(storage.pending(engine, moment, 1))
    with session_factory(engine)() as session:
        total = session.scalar(select(func.count()).select_from(ThetaDevice)) or 0
        active = session.scalar(select(func.count()).select_from(ThetaDevice).where(ThetaDevice.revoked_at.is_(None))) or 0
        rows = session.scalars(select(ThetaDevice).order_by(ThetaDevice.created_at.desc(), ThetaDevice.device_id).limit(MAX_DEVICES_IN_STATUS))
        values = [_device_view(row, moment, pending) for row in rows]
    states = {row["state"] for row in values}
    state = next((s for s in ("online", "idle", "awaiting_connection", "stale", "revoked") if s in states), "awaiting_enrollment")
    return {"status": state, "as_of": moment.isoformat(),
            "heartbeat_freshness_seconds": HEARTBEAT_FRESHNESS_SECONDS,
            "device_count": total, "active_device_count": active,
            "truncated": total > MAX_DEVICES_IN_STATUS, "devices": values,
            "source_identity_independently_verified": False}
