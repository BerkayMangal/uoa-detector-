"""Append-only requests and connector receipts; no broker or strategy mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    exists,
    func,
    inspect,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory


class ThetaRequest(AlfaBase):
    __tablename__ = "alfa_theta_validation_request"
    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String, unique=True)
    option_symbol: Mapped[str] = mapped_column(String, index=True)
    planned_entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str] = mapped_column(String)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_label: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text)


class ThetaReceipt(AlfaBase):
    __tablename__ = "alfa_theta_validation_receipt"
    __table_args__ = (UniqueConstraint("request_id", "attempt"),)
    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    upload_sha256: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    finalized: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)


TABLES = (ThetaRequest, ThetaReceipt)
MAX_ATTEMPTS = 3


def ensure(engine: Engine) -> None:
    for model in TABLES:
        cast(Table, model.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(model.__tablename__ in names for model in TABLES)


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def request_view(row: ThetaRequest) -> dict[str, Any]:
    # Any is confined to the persisted/wire JSON presentation boundary.
    payload = json.loads(row.payload_json)
    return {
        "request_id": row.request_id,
        "request_hash": row.request_hash,
        "option_symbol": row.option_symbol,
        "planned_entry_at": utc(row.planned_entry_at).isoformat(),
        "created_at": utc(row.created_at).isoformat(),
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "source_label": row.source_label,
        "session_close_at": payload.get("session_close_at"),
        "retry_of": payload.get("retry_of"),
        "data_use": "retrospective_one_contract_quote_markout",
    }


def retry_request(engine: Engine, request_id: str, now: datetime) -> dict[str, Any]:
    with session_factory(engine)() as session:
        parent = session.get(ThetaRequest, request_id)
        if parent is None:
            raise ValueError("request_not_found")
        receipts = list(
            session.scalars(select(ThetaReceipt).where(ThetaReceipt.request_id == request_id))
        )
        if any(row.finalized for row in receipts):
            raise ValueError("completed_result_is_immutable")
        if len(receipts) < MAX_ATTEMPTS:
            raise ValueError("retry_not_needed")
        payload = json.loads(parent.payload_json)
        payload["retry_of"] = request_id
    return add_request(engine, payload, now)


def receipt_view(row: ThetaReceipt) -> dict[str, Any]:
    return {
        "receipt_id": row.receipt_id,
        "request_id": row.request_id,
        "attempt": row.attempt,
        "received_at": utc(row.received_at).isoformat(),
        "upload_sha256": row.upload_sha256,
        "status": row.status,
        "finalized": bool(row.finalized),
        "result": json.loads(row.result_json),
    }


def add_request(engine: Engine, payload: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    body = canonical(payload)
    checksum = digest(body)
    key = "theta_" + checksum[:32]
    with session_factory(engine)() as session:
        existing = session.get(ThetaRequest, key)
        if existing:
            if existing.payload_json != body:
                raise ValueError("request_hash_conflict")
            return request_view(existing)
        row = ThetaRequest(
            request_id=key,
            request_hash=checksum,
            option_symbol=payload["option_symbol"],
            planned_entry_at=datetime.fromisoformat(payload["planned_entry_at"]),
            created_at=now,
            source_kind=payload["source_kind"],
            source_id=payload.get("source_id"),
            source_label=payload["source_label"],
            payload_json=body,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            found = session.get(ThetaRequest, key)
            if found is None or found.payload_json != body:
                raise ValueError("request_hash_conflict") from None
            return request_view(found)
        return request_view(row)


def get_request(engine: Engine, key: str) -> dict[str, Any] | None:
    with session_factory(engine)() as session:
        row = session.get(ThetaRequest, key)
        return request_view(row) if row else None


def pending(engine: Engine, now: datetime, limit: int) -> list[dict[str, Any]]:
    if not available(engine):
        return []
    attempts = (
        select(func.count())
        .where(ThetaReceipt.request_id == ThetaRequest.request_id)
        .scalar_subquery()
    )
    finished = exists().where(
        ThetaReceipt.request_id == ThetaRequest.request_id, ThetaReceipt.finalized == 1
    )
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(ThetaRequest)
            .where(
                ThetaRequest.planned_entry_at <= now - timedelta(minutes=64),
                ~finished,
                attempts < MAX_ATTEMPTS,
            )
            .order_by(ThetaRequest.created_at, ThetaRequest.request_id)
            .limit(limit)
        )
        return [request_view(row) for row in rows]


def append_receipt(
    engine: Engine,
    *,
    request_id: str,
    upload: Mapping[str, Any],
    result: Mapping[str, Any],
    status: str,
    finalized: bool,
    now: datetime,
) -> dict[str, Any]:
    body = canonical(upload)
    checksum = digest(body)
    key = digest(request_id + ":" + checksum)
    with session_factory(engine)() as session:
        # Postgres serializes admission on the immutable parent row; SQLite's
        # unique attempt slot also prevents concurrent writers exceeding the cap.
        parent = session.scalar(
            select(ThetaRequest).where(ThetaRequest.request_id == request_id).with_for_update()
        )
        if parent is None:
            raise ValueError("request_not_found")
        old = session.get(ThetaReceipt, key)
        if old:
            return receipt_view(old)
        prior = list(
            session.scalars(
                select(ThetaReceipt)
                .where(ThetaReceipt.request_id == request_id)
                .order_by(ThetaReceipt.attempt)
            )
        )
        if any(row.finalized for row in prior):
            raise ValueError("completed_result_is_immutable")
        attempt = len(prior) + 1
        if attempt > MAX_ATTEMPTS:
            raise ValueError("attempt_limit_reached")
        row = ThetaReceipt(
            receipt_id=key,
            request_id=request_id,
            attempt=attempt,
            received_at=now,
            upload_sha256=checksum,
            status=status,
            finalized=int(finalized),
            payload_json=body,
            result_json=canonical(result),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.get(ThetaReceipt, key)
            if existing:
                return receipt_view(existing)
            raise ValueError("concurrent_receipt_retry_required") from None
        return receipt_view(row)


def view(engine: Engine, *, now: datetime, limit: int = 32) -> dict[str, Any]:
    if not available(engine):
        return {
            "status": "storage_unavailable",
            "jobs": [],
            "recent_results": [],
            "last_connector_contact": None,
            "as_of": now.isoformat(),
        }
    with session_factory(engine)() as session:
        requests = list(
            session.scalars(
                select(ThetaRequest).order_by(ThetaRequest.created_at.desc()).limit(limit)
            )
        )
        keys = [row.request_id for row in requests]
        receipts = (
            list(
                session.scalars(
                    select(ThetaReceipt)
                    .where(ThetaReceipt.request_id.in_(keys))
                    .order_by(ThetaReceipt.received_at.desc())
                )
            )
            if keys
            else []
        )
        last = session.scalar(
            select(ThetaReceipt).order_by(ThetaReceipt.received_at.desc()).limit(1)
        )
        jobs = []
        for row in requests:
            attempts = [receipt for receipt in receipts if receipt.request_id == row.request_id]
            completed = any(receipt.finalized for receipt in attempts)
            value = request_view(row)
            value.update(
                {
                    "attempts": len(attempts),
                    "finalized": completed,
                    "state": "completed"
                    if completed
                    else "retry_limit"
                    if len(attempts) >= MAX_ATTEMPTS
                    else "pending",
                }
            )
            jobs.append(value)
        status = (
            "awaiting_connection"
            if last is None
            else ("data_received" if last.finalized else "connection_or_data_error")
        )
        return {
            "status": status,
            "as_of": now.isoformat(),
            "jobs": jobs,
            "recent_results": [receipt_view(row) for row in receipts[:12]],
            "last_connector_contact": utc(last.received_at).isoformat() if last else None,
            "execution_enabled": False,
            "source": "owner_connected_thetadata_terminal",
            "source_identity_independently_verified": False,
        }
