"""Single-use owner pairing grants for narrowly scoped Theta connector sessions.

Only hashes are persisted. A connector can access the immutable request roster
captured when the owner issued its grant; it never inherits the owner's password
or access to newly created jobs.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

from sqlalchemy import DateTime, String, Table, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.theta_validation import storage

_TOKEN = re.compile(r"[0-9a-f]{64}")
_REQUEST_ID = re.compile(r"theta_[0-9a-f]{32}")
_PAIRING_LIFETIME = timedelta(hours=2)
_CONNECTOR_LIFETIME = timedelta(days=7)
_MAX_REQUESTS = 32


class ThetaPairingGrant(AlfaBase):
    __tablename__ = "alfa_theta_pairing_grant"
    grant_id: Mapped[str] = mapped_column(String, primary_key=True)
    token_sha256: Mapped[str] = mapped_column(String, unique=True)
    request_ids_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ThetaConnectorSession(AlfaBase):
    __tablename__ = "alfa_theta_connector_session"
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    grant_id: Mapped[str] = mapped_column(String, unique=True)
    token_sha256: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(frozen=True)
class IssuedPairing:
    token: str
    expires_at: datetime
    request_count: int


@dataclass(frozen=True)
class IssuedConnector:
    token: str
    expires_at: datetime
    request_count: int


def ensure(engine: Engine) -> None:
    """Add only the two missing pairing tables, at application startup."""
    for model in (ThetaPairingGrant, ThetaConnectorSession):
        cast(Table, model.__table__).create(engine, checkfirst=True)


def _clock(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_pairing_clock_required")
    return now.astimezone(UTC)


def _hash_token(raw: str) -> str | None:
    if not isinstance(raw, str) or _TOKEN.fullmatch(raw) is None:
        return None
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _scope(body: str) -> tuple[str, ...] | None:
    try:
        values = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(values, list) or len(values) > _MAX_REQUESTS:
        return None
    if any(not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None for value in values):
        return None
    if len(set(values)) != len(values):
        return None
    return tuple(values)


def issue(engine: Engine, now: datetime) -> IssuedPairing:
    """Snapshot at most32 mature pending jobs; store no raw pairing token."""
    moment = _clock(now)
    ids = tuple(row["request_id"] for row in storage.pending(engine, moment, _MAX_REQUESTS))
    body = storage.canonical(ids)
    if _scope(body) is None:
        raise ValueError("invalid_pairing_request_scope")
    raw = secrets.token_hex(32)
    checksum = _hash_token(raw)
    assert checksum is not None
    expires = moment + _PAIRING_LIFETIME
    with session_factory(engine)() as session, session.begin():
        session.add(
            ThetaPairingGrant(
                grant_id=uuid.uuid4().hex,
                token_sha256=checksum,
                request_ids_json=body,
                created_at=moment,
                expires_at=expires,
            )
        )
    return IssuedPairing(raw, expires, len(ids))


def redeem(engine: Engine, raw: str, now: datetime) -> IssuedConnector:
    """Atomically consume one pairing grant and return a new scoped token once."""
    moment = _clock(now)
    checksum = _hash_token(raw)
    if checksum is None:
        raise ValueError("invalid_pairing_token")
    with session_factory(engine)() as session:
        grant = session.scalar(
            select(ThetaPairingGrant)
            .where(ThetaPairingGrant.token_sha256 == checksum)
            .with_for_update()
        )
        if grant is None:
            raise ValueError("invalid_pairing_token")
        if not storage.utc(grant.created_at) <= moment < storage.utc(grant.expires_at):
            raise ValueError("pairing_token_expired")
        scope = _scope(grant.request_ids_json)
        if scope is None:
            raise ValueError("invalid_pairing_request_scope")
        if (
            session.scalar(
                select(ThetaConnectorSession.session_id).where(
                    ThetaConnectorSession.grant_id == grant.grant_id
                )
            )
            is not None
        ):
            raise ValueError("pairing_token_consumed")
        connector = secrets.token_hex(32)
        token_hash = _hash_token(connector)
        assert token_hash is not None
        expires = moment + _CONNECTOR_LIFETIME
        session.add(
            ThetaConnectorSession(
                session_id=uuid.uuid4().hex,
                grant_id=grant.grant_id,
                token_sha256=token_hash,
                created_at=moment,
                expires_at=expires,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # The unique grant_id is the final single-use guard, including
            # SQLite where SELECT FOR UPDATE does not lock the parent row.
            session.rollback()
            raise ValueError("pairing_token_consumed") from None
    return IssuedConnector(connector, expires, len(scope))


def authenticate(engine: Engine, raw: str, now: datetime) -> tuple[str, ...] | None:
    """Return only this active session's frozen request IDs, otherwise None."""
    moment = _clock(now)
    checksum = _hash_token(raw)
    if checksum is None:
        return None
    with session_factory(engine)() as session:
        record = session.scalar(
            select(ThetaConnectorSession).where(ThetaConnectorSession.token_sha256 == checksum)
        )
        if record is None or not storage.utc(record.created_at) <= moment < storage.utc(
            record.expires_at
        ):
            return None
        grant = session.get(ThetaPairingGrant, record.grant_id)
        return _scope(grant.request_ids_json) if grant is not None else None


def personalize_source(source: str, connector_token: str, base_url: str) -> str:
    """Embed one scoped token and trusted origin at exact standalone markers."""
    if _hash_token(connector_token) is None:
        raise ValueError("invalid_connector_token")
    try:
        origin = urlsplit(base_url)
        port = origin.port
    except ValueError:
        raise ValueError("invalid_connector_origin") from None
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
        or any(character.isspace() for character in base_url)
        or port == 0
    ):
        raise ValueError("invalid_connector_origin")
    substitutions = {
        'EMBEDDED_TOKEN = ""': "EMBEDDED_TOKEN = " + repr(connector_token),
        'EMBEDDED_UOA = ""': "EMBEDDED_UOA = " + repr(base_url.rstrip("/")),
    }
    lines = source.splitlines(keepends=True)
    for marker, replacement in substitutions.items():
        matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == marker]
        if len(matches) != 1:
            raise ValueError("connector_source_marker_mismatch")
        index = matches[0]
        original = lines[index]
        suffix = original[len(original.rstrip("\r\n")) :]
        lines[index] = replacement + suffix
    return "".join(lines)
