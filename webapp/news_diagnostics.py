"""Read-only metadata for original candidate-bound news, never a policy input."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import session_factory
from webapp.evidence_reads import scoped_read
from webapp.live_alpha.store import LiveScan
from webapp.news_research import config as news_config
from webapp.news_research import storage as physical
from webapp.shadow_capture import bindings, config, storage
from webapp.shadow_capture.model import Record, aware, stored

_MAX_BYTES = 8 * 1024 * 1024
_FLAGS = ("limited", "truncated", "has_more", "next", "next_cursor", "next_page")


@dataclass(frozen=True)
class PaginationFlag:
    name: str
    present: bool
    active: bool


@dataclass(frozen=True)
class EvidenceCheck:
    at: datetime
    flag: bool | None
    reason: str
    bound_packet_matches: bool
    available_at: datetime | None


@dataclass(frozen=True)
class Row:
    candidate_key: str
    ticker: str
    status: str
    reason: str
    source_verified: bool = False
    original_news: bool | None = None
    source_finished_at: datetime | None = None
    candidate_at: datetime | None = None
    candidate_available_at: datetime | None = None
    packet_key: str | None = None
    payload_hash: str | None = None
    packet_verified: bool = False
    requested_at: datetime | None = None
    received_at: datetime | None = None
    application_checked_at: datetime | None = None
    physical_available_at: datetime | None = None
    item_count: int | None = None
    requested_limit: int = 0
    pagination: tuple[PaginationFlag, ...] = ()
    scan_finish: EvidenceCheck | None = None
    candidate_publication: EvidenceCheck | None = None
    diagnostic_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    as_of: datetime
    day: date
    status: str
    reason: str
    candidate_count: int = 0
    invalid_records: int = 0
    limited: bool = False
    rows: tuple[Row, ...] = ()
    method: str = "bound_news_diagnostics_v1"
    inference: str = "NOT_PERFORMED"


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate_news_json_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ValueError("nonfinite_news_json")


def _payload(blob: bytes, checksum: str) -> dict[str, object]:
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("packet_decompressed_limit")
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("packet_payload_hash_mismatch")
    body: object = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ValueError("packet_payload_shape_invalid")
    # JSON is the untyped boundary; only named metadata leaves this helper.
    return cast(dict[str, object], body)


def _check(engine: Engine, ticker: str, at: datetime, packet: physical.Packet) -> EvidenceCheck:
    cfg = news_config.settings()
    state = physical.evidence(
        engine,
        ticker,
        at,
        window_hours=cfg.news_window_hours,
        max_age_seconds=cfg.news_max_age_seconds,
        check_at=stored(packet.application_checked_at) if packet.application_checked_at else None,
    )
    return EvidenceCheck(
        at,
        state.flag,
        state.reason if re.fullmatch(r"[a-z_]{1,80}", state.reason) else "source_evidence_unknown",
        state.packet_key == packet.key and state.payload_hash == packet.payload_hash,
        state.available_at,
    )


def _inspect_row(engine: Engine, candidate: Record, active: Record, now: datetime) -> Row:
    ticker = candidate.payload.get("ticker")
    ticker = (
        ticker if isinstance(ticker, str) and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", ticker) else ""
    )
    candidate_key = candidate.key if re.fullmatch(r"[0-9a-f]{64}", candidate.key) else ""
    unknown = Row(candidate_key, ticker, "UNKNOWN", "original_candidate_unverified")
    if not bindings.valid_candidate(engine, candidate, active, now):
        return unknown
    key, checksum, scan_id = (
        candidate.payload.get(name) for name in ("news_packet_key", "news_payload_hash", "scan_id")
    )
    if (
        not isinstance(key, str)
        or not re.fullmatch(r"[0-9a-f]{64}", key)
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        or not isinstance(scan_id, str)
    ):
        return Row(candidate_key, ticker, "UNKNOWN", "bound_packet_identity_missing", True)
    with session_factory(engine)() as session:
        packet = session.get(physical.Packet, key)
        scan = session.get(LiveScan, scan_id)
        receipt = session.get(physical.Receipt, key)
    if packet is None or scan is None or receipt is None:
        return Row(candidate_key, ticker, "UNKNOWN", "bound_packet_or_receipt_missing", True)
    if packet.ticker != ticker or packet.payload_hash != checksum:
        return Row(candidate_key, ticker, "UNKNOWN", "bound_packet_identity_mismatch", True)
    expected_receipt = physical.signature(
        packet.key,
        packet.ticker,
        packet.requested_at,
        packet.received_at,
        packet.payload_hash,
        packet.succeeded,
        packet.application_checked_at,
    )
    if receipt.key != packet.key or receipt.packet_hash != expected_receipt:
        return Row(candidate_key, ticker, "UNKNOWN", "bound_physical_receipt_hash_mismatch", True)
    if not packet.succeeded:
        return Row(candidate_key, ticker, "UNKNOWN", "bound_source_retrieval_failed", True)
    finished = stored(scan.finished_at)
    publication = aware(candidate.at)
    scan_check = _check(engine, ticker, finished, packet)
    publication_check = _check(engine, ticker, publication, packet)
    available = stored(receipt.available_at)
    requested, received = stored(packet.requested_at), stored(packet.received_at)
    if not requested <= received <= available <= finished <= publication <= now:
        return Row(candidate_key, ticker, "UNKNOWN", "bound_packet_clock_unverified", True)
    if not scan_check.bound_packet_matches or scan_check.flag is not candidate.payload.get("news"):
        return Row(candidate_key, ticker, "UNKNOWN", "original_packet_evidence_unverified", True)
    try:
        body = _payload(packet.compressed_payload, packet.payload_hash)
    except (ValueError, OSError, EOFError, TypeError, RecursionError) as error:
        allowed = {
            "packet_decompressed_limit",
            "packet_payload_hash_mismatch",
            "packet_payload_shape_invalid",
            "duplicate_news_json_key",
            "nonfinite_news_json",
        }
        reason = str(error) if str(error) in allowed else "bound_packet_payload_unverified"
        return Row(candidate_key, ticker, "UNKNOWN", reason, True)
    values = body["data"]
    assert isinstance(values, list)
    flags = tuple(
        PaginationFlag(name, name in body, body.get(name) not in (None, False, "", 0))
        for name in _FLAGS
    )
    requested_limit = load_live_alpha_settings().news.limit
    codes = []
    if len(values) >= requested_limit:
        codes.append("page_at_or_over_requested_limit")
    if any(flag.active for flag in flags):
        codes.append("pagination_or_limited_marker")
    if (scan_check.flag, scan_check.reason) != (publication_check.flag, publication_check.reason):
        codes.append("source_evidence_changed_between_clocks")
    if not publication_check.bound_packet_matches or publication_check.flag is None:
        codes.append("publication_evidence_unknown")
    original_news = candidate.payload.get("news")
    if scan_check.flag is None:
        codes.append("original_source_evidence_unknown")
    if not codes:
        codes.append(
            "checked_empty_metadata_consistent"
            if original_news is False
            else "source_metadata_consistent"
        )
    return Row(
        candidate.key,
        ticker,
        "UNKNOWN_COVERAGE"
        if original_news is None
        or (original_news is False and codes != ["checked_empty_metadata_consistent"])
        else "VERIFIED_METADATA",
        "diagnostic_only_original_decisions_unchanged",
        True,
        original_news if isinstance(original_news, bool) else None,
        finished,
        publication,
        candidate.confirmed_at,
        packet.key,
        packet.payload_hash,
        True,
        requested,
        received,
        stored(packet.application_checked_at) if packet.application_checked_at else None,
        available,
        len(values),
        requested_limit,
        flags,
        scan_check,
        publication_check,
        tuple(codes),
    )


def _row(engine: Engine, candidate: Record, active: Record, now: datetime) -> Row:
    try:
        return _inspect_row(engine, candidate, active, now)
    except (SQLAlchemyError, ValueError, OSError, EOFError, TypeError, RecursionError):
        # Frozen source verifiers can also reject a malformed raw packet before
        # our metadata extraction. Neither SQL nor exception text is public.
        key = candidate.key if re.fullmatch(r"[0-9a-f]{64}", candidate.key) else ""
        ticker = candidate.payload.get("ticker")
        ticker = (
            ticker
            if isinstance(ticker, str) and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", ticker)
            else ""
        )
        return Row(key, ticker, "UNKNOWN", "source_verification_unavailable")


def _build(engine: Engine, now: datetime, day: date | None = None) -> Report:
    at = aware(now)
    today = at.astimezone(ZoneInfo("America/New_York")).date()
    selected = day or today
    if selected > today:
        return Report(at, selected, "UNKNOWN", "future_session_not_observed")
    if is_session_day(selected, load_live_alpha_settings().calendar) is not True:
        return Report(at, selected, "UNKNOWN", "not_a_verified_session")
    names = set(inspect(engine).get_table_names())
    required = (
        physical.Packet,
        physical.Receipt,
        LiveScan,
        storage.ShadowEvent,
        storage.ShadowReceipt,
    )
    if not all(model.__tablename__ in names for model in required):
        return Report(at, selected, "UNKNOWN", "source_storage_unavailable")
    active = storage.active(engine, at)
    if active is None:
        return Report(at, selected, "UNKNOWN", "original_activation_unavailable")
    candidates = storage.list_events(
        engine, "candidate", at, day=selected, limit=config.settings().candidate_slots + 1
    )
    if len(candidates) > config.settings().candidate_slots:
        return Report(
            at, selected, "UNKNOWN", "candidate_limit_exceeded", len(candidates), limited=True
        )
    rows = tuple(_row(engine, candidate, active, at) for candidate in candidates)
    invalid = sum(not row.packet_verified for row in rows)
    return Report(
        at,
        selected,
        "AVAILABLE" if rows else "AWAITING_SOURCE",
        "metadata_only_no_policy_or_outcome_change",
        len(candidates),
        invalid,
        False,
        rows,
    )


@scoped_read
def build(engine: Engine, now: datetime, day: date | None = None) -> Report:
    at = aware(now)
    selected = day or at.astimezone(ZoneInfo("America/New_York")).date()
    try:
        return _build(engine, at, selected)
    except (SQLAlchemyError, ValueError, OSError, EOFError, TypeError, RecursionError):
        return Report(at, selected, "UNKNOWN", "source_read_unavailable")
