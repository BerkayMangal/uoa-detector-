"""Private raw news packets and post-commit physical availability evidence."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import Boolean, DateTime, LargeBinary, String, Table, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column
from webapp.alpha_lab.db import append as insert
from webapp.board.db import AlfaBase, session_factory


class Packet(AlfaBase):
    __tablename__ = "alfa_news_research_packet"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_hash: Mapped[str] = mapped_column(String)
    compressed_payload: Mapped[bytes] = mapped_column(LargeBinary)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    # Binding to the legacy check clock is identity only, NEVER availability.
    application_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Receipt(AlfaBase):
    __tablename__ = "alfa_news_research_receipt"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    packet_hash: Mapped[str] = mapped_column(String)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


@dataclass(frozen=True)
class NewsState:
    flag: bool | None
    reason: str
    packet_key: str | None = None
    payload_hash: str | None = None
    requested_at: datetime | None = None
    received_at: datetime | None = None
    available_at: datetime | None = None
    headlines: tuple[str, ...] = ()
    application_checked_at: datetime | None = None


# A failed recording cannot expose an earlier success in the same worker. No old
# physical availability is manufactured when the database itself rejects a write.
_FAILURES: dict[tuple[str, str], datetime] = {}
_MAX_BYTES = 8 * 1024 * 1024  # Storage safety bound, not a strategy threshold.


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("physical clock must be timezone aware")
    return value.astimezone(UTC)


def stored(value: datetime) -> datetime:
    # SQLite strips tzinfo; all these columns are physically written in UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def signature(key: str, ticker: str, requested: datetime, received: datetime, checksum: str, succeeded: bool, application_checked_at: datetime | None = None) -> str:
    binding = stored(application_checked_at).isoformat() if application_checked_at else None
    return hashlib.sha256(canonical((key, ticker, stored(requested).isoformat(), stored(received).isoformat(), checksum, succeeded, binding))).hexdigest()


def ensure(engine: Engine) -> None:
    for model in (Packet, Receipt):
        cast(Table, model.__table__).create(engine, checkfirst=True)


def failure_key(engine: Engine, ticker: str) -> tuple[str, str]:
    return hashlib.sha256(str(engine.url).encode()).hexdigest(), ticker.upper()


def mark_failure(engine: Engine, ticker: str, at: datetime) -> None:
    value = aware(at)
    key = failure_key(engine, ticker)
    _FAILURES[key] = max(value, _FAILURES.get(key, value))


def append(
    engine: Engine,
    ticker: str,
    payload: object,
    requested_at: datetime,
    received_at: datetime,
    *,
    succeeded: bool = True,
    clock: Callable[[], datetime] | None = None,
    application_checked_at: datetime | None = None,
) -> str:
    requested, received = aware(requested_at), aware(received_at)
    binding = aware(application_checked_at) if application_checked_at is not None else None
    if requested > received:
        raise ValueError("response precedes request")
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker required")
    raw = canonical(payload)
    if len(raw) > _MAX_BYTES:
        raise ValueError("news packet exceeds storage bound")
    checksum = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256(canonical((ticker, requested.isoformat(), received.isoformat(), checksum, succeeded, binding.isoformat() if binding else None))).hexdigest()
    insert(engine, cast(Table, Packet.__table__), [dict(
        key=key, ticker=ticker, requested_at=requested, received_at=received,
        payload_hash=checksum, compressed_payload=gzip.compress(raw, mtime=0), succeeded=succeeded,
        application_checked_at=binding,
    )])
    # insert() has returned from its transaction context: this clock is AFTER
    # the packet commits. A crash here leaves an unusable packet, never a receipt.
    available = aware(clock() if clock else datetime.now(UTC))
    if available < received:
        raise ValueError("availability precedes response")
    insert(engine, cast(Table, Receipt.__table__), [dict(
        key=key, packet_hash=signature(key, ticker, requested, received, checksum, succeeded, binding),
        available_at=available,
    )])
    return key


def publication(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def evidence(
    engine: Engine,
    ticker: str,
    asof: datetime,
    window_hours: float = 36,
    max_age_seconds: int = 900,
    *,
    check_at: datetime | None = None,
) -> NewsState:
    asof = aware(asof)
    ticker = ticker.strip().upper()
    check_at = aware(check_at) if check_at is not None else None
    if window_hours <= 0 or max_age_seconds <= 0:
        raise ValueError("positive evidence windows required")
    names = set(inspect(engine).get_table_names())
    if not all(model.__tablename__ in names for model in (Packet, Receipt)):
        return NewsState(None, "physical_news_storage_unavailable")
    with session_factory(engine)() as session:
        query = select(Packet).where(Packet.ticker == ticker, Packet.received_at <= asof)
        if check_at is not None:
            query = query.where(Packet.application_checked_at == check_at)
        packets = list(session.scalars(
            query.order_by(Packet.received_at.desc(), Packet.key.desc()).limit(2)
        ))
        packet = packets[0] if packets else None
        if packet is None:
            return NewsState(None, "no_physically_captured_news")
        receipt = session.get(Receipt, packet.key)
    requested, received = stored(packet.requested_at), stored(packet.received_at)
    available = stored(receipt.available_at) if receipt else None
    binding = stored(packet.application_checked_at) if packet.application_checked_at else None
    basis = NewsState(None, "", packet.key, packet.payload_hash, requested, received, available, application_checked_at=binding)

    def unknown(reason: str) -> NewsState:
        return replace(basis, reason=reason)

    if len(packets) > 1 and stored(packets[1].received_at) == received:
        return unknown("news_capture_timing_ambiguous")

    failed = _FAILURES.get(failure_key(engine, ticker))
    if failed is not None and failed <= asof and received <= failed and (available is None or available <= failed):
        return unknown("research_capture_failed")
    if requested > received or received > asof:
        return unknown("invalid_physical_chronology")
    if receipt is None or available is None:
        return unknown("physical_receipt_missing")
    if not received <= available <= asof:
        return unknown("physical_receipt_unavailable")
    if receipt.packet_hash != signature(packet.key, ticker, requested, received, packet.payload_hash, packet.succeeded, binding):
        return unknown("physical_receipt_hash_mismatch")
    if not packet.succeeded:
        return unknown("news_retrieval_failed")
    if (asof - received).total_seconds() > max_age_seconds:
        return unknown("physical_news_stale")
    try:
        # Bounded decompression prevents a corrupted private row becoming a bomb.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(packet.compressed_payload)) as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != packet.payload_hash:
            return unknown("packet_payload_hash_mismatch")
        body = json.loads(raw)
    except (OSError, EOFError, ValueError, TypeError):
        return unknown("packet_payload_invalid")
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return unknown("news_payload_shape_invalid")
    found: dict[str, tuple[datetime, str]] = {}
    identities: dict[str, tuple[str, datetime]] = {}
    for row in body["data"]:
        if not isinstance(row, dict):
            return unknown("news_item_shape_invalid")
        headline, tickers = row.get("headline"), row.get("tickers")
        if not isinstance(headline, str) or not headline.strip():
            return unknown("news_headline_invalid")
        if not isinstance(tickers, list) or not tickers or any(not isinstance(t, str) or not t.strip() for t in tickers):
            return unknown("news_ticker_mapping_unknown")
        if ticker not in {t.strip().upper() for t in tickers}:
            continue
        if not isinstance(row.get("source"), str) or not row["source"].strip():
            return unknown("news_source_unknown")
        at = publication(row.get("created_at"))
        if at is None:
            return unknown("news_publication_unknown")
        if at > received:
            return unknown("news_publication_after_retrieval")
        if any(row.get(name) not in (None, "", False) and publication(row[name]) != at
               for name in ("updated_at", "modified_at", "last_modified", "revised_at")):
            return unknown("news_revision_unknown")
        text = headline.strip()
        normalized = re.sub(r"[^\w]+", "", text.casefold())
        if not normalized:
            return unknown("news_headline_invalid")
        identity = row.get("id")
        if identity is not None:
            key = str(identity)
            prior_identity = identities.get(key)
            if prior_identity is not None and prior_identity != (normalized, at):
                return unknown("news_conflicting_versions")
            identities[key] = (normalized, at)
        old = found.get(normalized)
        if old is None or at < old[0]:
            found[normalized] = (at, text)
    headlines = tuple(text for at, text in found.values() if asof - timedelta(hours=window_hours) <= at <= asof)
    return replace(basis, flag=bool(headlines), reason="physically_observed_presence" if headlines else "physically_checked_empty", headlines=headlines)
