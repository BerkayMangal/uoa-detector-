"""Append-only private response and item provenance, with post-commit receipts."""
from __future__ import annotations

import gzip
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import DateTime, Integer, LargeBinary, String, Table, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, defer, mapped_column

from webapp.alpha_lab.db import append as insert
from webapp.board.db import AlfaBase, session_factory

from .config import Provider, profile_hash, settings
from .model import Parsed, aware, canonical, digest, etag_value, stored


class SharedNewsPacket(AlfaBase):
    __tablename__ = "alfa_shared_news_packet"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    requested_url: Mapped[str] = mapped_column(String)
    profile_hash: Mapped[str] = mapped_column(String)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String)
    body_hash: Mapped[str] = mapped_column(String)
    compressed_body: Mapped[bytes] = mapped_column(LargeBinary)
    etag: Mapped[str | None] = mapped_column(String, nullable=True)
    body_packet_key: Mapped[str | None] = mapped_column(String, nullable=True)


class SharedNewsReceipt(AlfaBase):
    __tablename__ = "alfa_shared_news_receipt"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    packet_hash: Mapped[str] = mapped_column(String)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SharedNewsObservation(AlfaBase):
    __tablename__ = "alfa_shared_news_item_observation"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    packet_key: Mapped[str] = mapped_column(String, index=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    item_id: Mapped[str] = mapped_column(String, index=True)
    version_hash: Mapped[str] = mapped_column(String, index=True)
    item_json: Mapped[str] = mapped_column(Text)


# Unique mapped class names avoid collisions in the global AlfaBase registry;
# short aliases keep this package's existing adapter API unchanged.
Packet = SharedNewsPacket
Receipt = SharedNewsReceipt
Observation = SharedNewsObservation


_FAILURES: dict[tuple[str, str], datetime] = {}
_FAILURE_LOCK = threading.Lock()


def _engine_key(engine: Engine, provider: str) -> tuple[str, str]:
    return digest(str(engine.url).encode()), provider


def mark_failure(engine: Engine, provider: str, at: datetime) -> None:
    with _FAILURE_LOCK:
        _FAILURES[_engine_key(engine, provider)] = aware(at)


def failure_at(engine: Engine, provider: str) -> datetime | None:
    with _FAILURE_LOCK:
        return _FAILURES.get(_engine_key(engine, provider))


def ensure(engine: Engine) -> None:
    for model in (Packet, Receipt, Observation):
        cast(Table, model.__table__).create(engine, checkfirst=True)


def signature(packet: Packet) -> str:
    return digest(canonical((packet.key, packet.provider, packet.requested_url, packet.profile_hash, stored(packet.requested_at).isoformat(),
                             stored(packet.received_at).isoformat(), packet.status_code, packet.category,
                             packet.body_hash, packet.etag, packet.body_packet_key)))


def append(engine: Engine, provider: Provider, requested_at: datetime, received_at: datetime,
           status_code: int | None, category: str, raw: bytes = b"", *, etag: str | None = None,
           body_packet_key: str | None = None, parsed: Parsed | None = None,
           clock: Callable[[], datetime] | None = None) -> str:
    """Only INSERT paths; a crash after packet commit leaves an unusable packet."""
    requested, received = aware(requested_at), aware(received_at)
    if requested > received or len(raw) > settings().max_body_bytes:
        raise ValueError("invalid physical packet")
    if etag is not None:
        etag_value(etag)
    if category == "ok" and (status_code != 200 or parsed is None):
        raise ValueError("successful body must be validated")
    if category == "not_modified" and (status_code != 304 or not body_packet_key or raw):
        raise ValueError("304 needs a previous body")
    checksum = digest(raw)
    target = next(p.url for p in settings().providers if p.provider == provider)
    profile = profile_hash()
    key = digest(canonical((provider, target, profile, requested.isoformat(), received.isoformat(), status_code, category, checksum, etag, body_packet_key)))
    packet = Packet(key=key, provider=provider, requested_url=target, profile_hash=profile, requested_at=requested, received_at=received,
                    status_code=status_code, category=category, body_hash=checksum,
                    compressed_body=gzip.compress(raw, mtime=0), etag=etag, body_packet_key=body_packet_key)
    # Packet and item associations commit together. The receipt is deliberately
    # committed separately so the availability clock is strictly post-packet.
    with engine.begin() as connection:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        constructor = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
        values = dict(key=key, provider=provider, requested_url=target, profile_hash=profile, requested_at=requested, received_at=received,
                      status_code=status_code, category=category, body_hash=checksum,
                      compressed_body=packet.compressed_body, etag=etag, body_packet_key=body_packet_key)
        connection.execute(constructor(cast(Table, Packet.__table__)).values(values).on_conflict_do_nothing(index_elements=["key"]))
        if parsed:
            for item in parsed.items:
                item_key = digest(canonical((key, item.id, item.content_version_hash)))
                connection.execute(constructor(cast(Table, Observation.__table__)).values(
                    key=item_key, packet_key=key, provider=provider, item_id=item.id,
                    version_hash=item.content_version_hash, item_json=item.model_dump_json()
                ).on_conflict_do_nothing(index_elements=["key"]))
    available = aware(clock() if clock else datetime.now(UTC))
    if available < received:
        raise ValueError("availability precedes response")
    insert(engine, cast(Table, Receipt.__table__), [dict(key=key, packet_hash=signature(packet), available_at=available)])
    return key


def body(packet: Packet) -> bytes:
    # A compressed corrupt/hostile DB value cannot allocate an unbounded buffer.
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(packet.compressed_body)) as stream:
        raw = stream.read(settings().max_body_bytes + 1)
    if len(raw) > settings().max_body_bytes or digest(raw) != packet.body_hash:
        raise ValueError("stored body integrity failure")
    return raw


def valid_receipt(packet: Packet, receipt: Receipt | None, now: datetime) -> bool:
    return bool(receipt is not None and receipt.packet_hash == signature(packet)
                and stored(packet.requested_at) <= stored(packet.received_at) <= stored(receipt.available_at) <= now)


def previous_body(engine: Engine, provider: Provider, now: datetime) -> tuple[Packet, Receipt] | None:
    """Only an authenticated, validated and physically committed 200 is a cache."""
    with session_factory(engine)() as session:
        pairs = session.execute(select(Packet, Receipt).options(defer(Packet.compressed_body)).join(Receipt, Receipt.key == Packet.key)
                                .where(Packet.provider == provider, Packet.category == "ok", Packet.status_code == 200,
                                       Receipt.available_at <= now)
                                .order_by(Packet.requested_at.desc(), Packet.key.desc()).limit(settings().read_packet_limit)).all()
        for packet, receipt in pairs:
            if valid_receipt(packet, receipt, now):
                # Materialize only the chosen bounded body before detaching.
                _ = packet.compressed_body
                return packet, receipt
    return None
