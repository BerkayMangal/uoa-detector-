"""Bounded opening-entry admission and stored-packet accountability.

JSON/Any is confined to immutable source, persisted evidence and HTTP adapter
boundaries. This module neither changes a decision nor calls a market provider.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, cast

from sqlalchemy import (
    Date,
    DateTime,
    LargeBinary,
    String,
    Table,
    Text,
    and_,
    create_engine,
    exists,
    func,
    inspect,
    or_,
    select,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import InstrumentedAttribute, Mapped, defer, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from uoa_detector.live_alpha.calendar import close_time
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.options_alpha.theta_quotes import ThetaContract
from webapp.alpha_lab.db import digest as source_digest
from webapp.board.db import AlfaBase, normalize_database_url, session_factory
from webapp.live_alpha.store import LiveScan
from webapp.opening.model import Advice
from webapp.opening.storage import OpeningCycle, OpeningDecision, OpeningPrimary
from webapp.theta_validation import service, storage

METHOD = "theta_opening_auto_v1"
PROSPECTIVE = "observed_committed_source_plus_60_seconds"
RETROSPECTIVE = "retrospective_publication_plus_60_seconds"
SOURCE_BATCH = 32
MAX_COHORT_ROWS = 256
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
STATE: dict[str, object] = {}


class AutomaticActivation(AlfaBase):
    __tablename__ = "alfa_theta_automatic_activation"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    catchup_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AutomaticAdmission(AlfaBase):
    __tablename__ = "alfa_theta_automatic_admission"
    admission_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_primary_key: Mapped[str] = mapped_column(String, unique=True)
    source_day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    disposition: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String)


class AutomaticReport(AlfaBase):
    __tablename__ = "alfa_theta_automatic_report"
    report_id: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String)


TABLES = (AutomaticActivation, AutomaticAdmission, AutomaticReport)


def enabled() -> bool:
    return os.getenv("THETA_AUTOMATIC_ENABLED", "true").strip().lower() in {"true", "1", "yes", "on"}


def aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware_clock_required")
    return at.astimezone(UTC)


@cache
def implementation_hash() -> str:
    from uoa_detector.options_alpha import theta_quotes

    paths = (Path(__file__), Path(service.__file__), Path(theta_quotes.__file__))
    return hashlib.sha256(b"".join(path.name.encode() + path.read_bytes() for path in paths)).hexdigest()


def ensure(engine: Engine) -> None:
    storage.ensure(engine)
    for model in TABLES:
        cast(Table, model.__table__).create(engine, checkfirst=True)


def available(engine: Engine) -> bool:
    names = set(inspect(engine).get_table_names())
    return all(model.__tablename__ in names for model in TABLES) and storage.available(engine)


def _insert(engine: Engine, model: type[AlfaBase], values: dict[str, Any], key: str) -> None:
    constructor = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    with engine.begin() as connection:
        connection.execute(constructor(cast(Table, model.__table__)).values(**values).on_conflict_do_nothing(index_elements=[key]))


def _activation(engine: Engine, now: datetime | None = None) -> AutomaticActivation | None:
    if now is not None:
        _insert(engine, AutomaticActivation, {"key": METHOD, "activated_at": now,
                                              "catchup_start": now - timedelta(days=7)}, "key")
    with session_factory(engine)() as session:
        return session.get(AutomaticActivation, METHOD)


def _source(
    primary: OpeningPrimary, decision: OpeningDecision | None, cycle: OpeningCycle | None,
    scan: LiveScan | None, observed: datetime, activation: datetime,
    source_payloads: tuple[str, str, str] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    published = storage.utc(primary.published_at)
    basis = RETROSPECTIVE if published < activation else PROSPECTIVE
    target = (published if basis == RETROSPECTIVE else observed) + timedelta(seconds=60)
    audit: dict[str, Any] = {
        "method": METHOD, "implementation_hash": implementation_hash(), "source_kind": "opening_entry",
        "source_primary_key": primary.key, "source_decision_key": primary.decision_key,
        "source_cycle_key": primary.cycle_key, "source_day": primary.day.isoformat(),
        "ticker": primary.ticker, "source_published_at": published.isoformat(),
        "first_observed_committed_at": observed.isoformat(), "entry_basis": basis,
        "historical_physical_availability_proven": False,
        "planned_entry_at": target.isoformat(), "instrument": None, "option_symbol": None,
        "standard_deliverable": "100_shares_assumed_unverified",
    }
    try:
        if (primary.kind != "entry" or decision is None or cycle is None or scan is None
                or decision.key != primary.decision_key or decision.cycle_key != primary.cycle_key
                or cycle.key != primary.cycle_key or cycle.scan_id != scan.scan_id
                or decision.ticker != primary.ticker or decision.day != primary.day or cycle.day != primary.day
                or decision.action != "BUY"):
            raise ValueError("source_identity_mismatch")
        if (published > observed or storage.utc(decision.published_at) != published
                or storage.utc(cycle.published_at) != published
                or storage.utc(scan.finished_at) > published or storage.utc(scan.started_at) > storage.utc(scan.finished_at)):
            raise ValueError("source_clock_invalid")
        if source_payloads is None:
            raise ValueError("source_payload_too_large")
        decision_body, cycle_body, snapshot_body = source_payloads
        advice = Advice.model_validate_json(decision_body)
        metadata, snapshot = json.loads(cycle_body), json.loads(snapshot_body)
        if (primary.key != source_digest((metadata.get("profile_hash"), primary.day, "entry", primary.ticker))
                or cycle.key != source_digest((metadata.get("profile_hash"), primary.day, scan.scan_id, metadata.get("phase")))
                or decision.key != source_digest((cycle.key, advice.scope, advice.position_id, primary.ticker))):
            raise ValueError("source_identity_mismatch")
        audit.update(source_scan_id=scan.scan_id, source_decision_sha256=storage.digest(decision_body),
                     source_cycle_sha256=storage.digest(cycle_body), source_snapshot_sha256=source_digest(snapshot),
                     source_policy=advice.source_policy, source_profile_hash=advice.source_profile_hash,
                     source_card_hash=advice.source_card_hash, instrument=advice.instrument)
        if (advice.action != "BUY" or advice.scope != "idea" or advice.ticker != primary.ticker
                or metadata.get("scan_id") != scan.scan_id or metadata.get("source_snapshot_hash") != source_digest(snapshot)
                or snapshot.get("policy_version") != advice.source_policy
                or snapshot.get("profile_sha256") != advice.source_profile_hash):
            raise ValueError("source_snapshot_mismatch")
        card_list = snapshot.get("cards")
        if not isinstance(card_list, list) or len(card_list) > MAX_COHORT_ROWS:
            raise ValueError("source_card_missing")
        cards = [card for card in card_list if isinstance(card, dict) and source_digest(card) == advice.source_card_hash]
        if len(cards) != 1:
            raise ValueError("source_card_missing")
        card = cards[0]
        if advice.instrument in ("bull_call_debit", "bear_put_debit"):
            raise ValueError("multiple_legs")
        if advice.instrument not in ("long_call", "long_put"):
            raise ValueError("unsupported_instrument")
        plan = advice.plan
        if plan is None or plan.instrument != advice.instrument:
            raise ValueError("source_plan_mismatch")
        if len(plan.contracts) != 1:
            raise ValueError("multiple_legs" if plan.contracts else "missing_contract")
        symbol = plan.contracts[0]
        audit["option_symbol"] = symbol
        try:
            contract = ThetaContract.from_occ(symbol)
        except ValueError:
            raise ValueError("invalid_contract") from None
        expected = "call" if advice.instrument == "long_call" else "put"
        if (contract.underlying != primary.ticker or contract.right != expected
                or advice.direction != ("up" if expected == "call" else "down")):
            raise ValueError("contract_direction_mismatch")
        instrument = card.get("instrument")
        if not isinstance(instrument, dict) or instrument.get("preferred") != advice.instrument:
            raise ValueError("source_plan_mismatch")
        options = card.get("options")
        if not isinstance(options, list):
            raise ValueError("source_plan_mismatch")
        structures = [item for item in options if isinstance(item, dict) and item.get("kind") == advice.instrument]
        if len(structures) != 1 or structures[0].get("readiness") != "READY":
            raise ValueError("source_plan_mismatch")
        structure = structures[0]
        if structure.get("multiplier") != 100:
            raise ValueError("nonstandard_multiplier")
        legs = structure.get("legs")
        if (not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict)
                or legs[0].get("option_symbol") != symbol or legs[0].get("is_long") is not True):
            raise ValueError("source_leg_not_single_long")
        if advice.expires_at is None or target >= aware(advice.expires_at):
            raise ValueError("expired_plan_before_target")
        audit["source_expires_at"] = aware(advice.expires_at).isoformat()
        if target.astimezone(service.ET).date() != primary.day:
            raise ValueError("source_clock_invalid")
        service._valid_entry(symbol, target)
        calendar = load_live_alpha_settings().calendar
        closing = datetime.combine(primary.day, close_time(primary.day, calendar), service.ET).astimezone(UTC)
        request = {
            "version": "theta_validation_v1", "method": METHOD, "option_symbol": symbol,
            "planned_entry_at": target.isoformat(), "source_kind": "opening_entry", "source_id": primary.key,
            "source_label": f"Otomatik kayıtlı giriş · {primary.ticker}", "entry_basis": basis,
            "original_option_trade": False, "session_close_at": closing.isoformat(), "source_binding": audit,
            "source_expires_at": audit["source_expires_at"],
        }
        return audit, request, None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        allowed = {"source_identity_mismatch", "source_clock_invalid", "source_payload_too_large", "source_snapshot_mismatch",
                   "source_card_missing", "multiple_legs", "unsupported_instrument", "source_plan_mismatch", "missing_contract",
                   "invalid_contract", "contract_direction_mismatch", "nonstandard_multiplier", "source_leg_not_single_long",
                   "expired_plan_before_target", "entry_outside_known_regular_session", "contract_expired_before_entry"}
        reason = str(error) if str(error) in allowed else "source_identity_mismatch"
        return audit, None, reason


def run_once(engine: Engine, now: datetime, *, clock: Callable[[], datetime] | None = None) -> dict[str, object]:
    moment = aware(now)
    if not enabled():
        STATE.update(status="disabled", at=moment.isoformat())
        return dict(STATE)
    ensure(engine)
    activation = _activation(engine, moment)
    assert activation is not None
    activated = storage.utc(activation.activated_at)
    if moment < activated:
        raise ValueError("clock_before_activation")
    source_tables = (OpeningPrimary, OpeningDecision, OpeningCycle, LiveScan)
    names = set(inspect(engine).get_table_names())
    admitted = excluded = 0
    if all(model.__tablename__ in names for model in source_tables):
        old = exists().where(AutomaticAdmission.source_primary_key == OpeningPrimary.key)
        query = (select(OpeningPrimary, OpeningDecision, OpeningCycle, LiveScan,
                        _byte_length(engine, OpeningDecision.payload_json), _byte_length(engine, OpeningCycle.payload_json),
                        _byte_length(engine, LiveScan.snapshot_json))
                 .options(defer(OpeningDecision.payload_json), defer(OpeningCycle.payload_json), defer(LiveScan.snapshot_json))
                 .outerjoin(OpeningDecision, OpeningDecision.key == OpeningPrimary.decision_key)
                 .outerjoin(OpeningCycle, OpeningCycle.key == OpeningPrimary.cycle_key)
                 .outerjoin(LiveScan, LiveScan.scan_id == OpeningCycle.scan_id)
                 .where(OpeningPrimary.kind == "entry", OpeningPrimary.published_at >= activation.catchup_start,
                        OpeningPrimary.published_at <= moment, ~old)
                 .order_by(OpeningPrimary.published_at, OpeningPrimary.key).limit(SOURCE_BATCH))
        with session_factory(engine)() as session:
            sources = session.execute(query).all()
        for primary, decision, cycle, scan, decision_bytes, cycle_bytes, scan_bytes in sources:
            source_payloads = None
            if (decision is not None and cycle is not None and scan is not None
                    and all(isinstance(size, int) and size <= MAX_SOURCE_BYTES for size in (decision_bytes, cycle_bytes, scan_bytes))):
                with session_factory(engine)() as session:
                    native = session.execute(select(OpeningDecision.payload_json, OpeningCycle.payload_json, LiveScan.snapshot_json)
                        .select_from(OpeningPrimary).join(OpeningDecision, OpeningDecision.key == OpeningPrimary.decision_key)
                        .join(OpeningCycle, OpeningCycle.key == OpeningPrimary.cycle_key)
                        .join(LiveScan, LiveScan.scan_id == OpeningCycle.scan_id).where(OpeningPrimary.key == primary.key)).first()
                    if native is not None:
                        source_payloads = (native[0], native[1], native[2])
                    del native
            observed = max(moment, aware((clock or (lambda: datetime.now(UTC)))()))
            audit, request, reason = _source(primary, decision, cycle, scan, observed, activated, source_payloads)
            del source_payloads
            payload = {"source": audit, "request": request}
            body = storage.canonical(payload)
            request_id = "theta_" + storage.digest(storage.canonical(request))[:32] if request is not None else None
            _insert(engine, AutomaticAdmission, {
                "admission_id": storage.digest(METHOD + ":" + primary.key), "source_primary_key": primary.key,
                "source_day": primary.day, "observed_at": observed, "disposition": "admitted" if request else "excluded",
                "reason": reason, "request_id": request_id, "payload_json": body, "payload_sha256": storage.digest(body),
            }, "admission_id")
            admitted += int(request is not None)
            excluded += int(request is None)
    # The first immutable admission wins. Resume its exact reserved request
    # after a crash between the admission commit and request creation.
    with session_factory(engine)() as session:
        missing = session.scalars(select(AutomaticAdmission).where(
            AutomaticAdmission.disposition == "admitted",
            ~exists().where(storage.ThetaRequest.request_id == AutomaticAdmission.request_id),
        ).order_by(AutomaticAdmission.observed_at, AutomaticAdmission.admission_id).limit(SOURCE_BATCH)).all()
    for row in missing:
        if storage.digest(row.payload_json) != row.payload_sha256:
            raise ValueError("admission_integrity_error")
        payload = json.loads(row.payload_json)
        created = storage.add_request(engine, payload["request"], moment)
        if created["request_id"] != row.request_id:
            raise ValueError("request_identity_mismatch")
    STATE.clear()
    STATE.update(status="ok" if all(model.__tablename__ in names for model in source_tables) else "no_source_storage",
                 at=moment.isoformat(), admitted=admitted, excluded=excluded, resumed=len(missing))
    return dict(STATE)


def _byte_length(engine: Engine, column: InstrumentedAttribute[str]) -> ColumnElement[int]:
    expression = func.octet_length(column) if engine.dialect.name == "postgresql" else func.length(sql_cast(column, LargeBinary))
    return cast(ColumnElement[int], expression)


def _receipt_budget(engine: Engine, receipt_ids: list[str], maximum: int) -> None:
    if not receipt_ids:
        return
    with session_factory(engine)() as session:
        size = session.scalar(select(func.coalesce(func.sum(_byte_length(engine, storage.ThetaReceipt.payload_json)), 0))
                              .where(storage.ThetaReceipt.receipt_id.in_(receipt_ids)))
    if int(size or 0) > maximum:
        raise ValueError("cohort_export_too_large")


def _raw_upload(engine: Engine, receipt: dict[str, Any]) -> dict[str, Any]:
    with session_factory(engine)() as session:
        body = session.scalar(select(storage.ThetaReceipt.payload_json).where(storage.ThetaReceipt.receipt_id == receipt["receipt_id"]))
    if body is None or storage.digest(body) != receipt["upload_sha256"]:
        raise ValueError("receipt_integrity_error")
    return cast(dict[str, Any], json.loads(body))


def _source_context(engine: Engine, day: date | None = None) -> dict[str, Any]:
    """Bounded backlog evidence; an observed cohort need not yet be complete."""
    activation = _activation(engine)
    value: dict[str, Any] = {"unobserved": 0, "unobserved_is_lower_bound": False,
                             "scan_complete": False, "denominator": "observed_immutable_admissions"}
    if activation is None or not inspect(engine).has_table(OpeningPrimary.__tablename__):
        return value
    old = exists().where(AutomaticAdmission.source_primary_key == OpeningPrimary.key)
    query = select(OpeningPrimary.key).where(OpeningPrimary.kind == "entry",
                                             OpeningPrimary.published_at >= activation.catchup_start, ~old)
    if day is not None:
        query = query.where(OpeningPrimary.day == day)
    with session_factory(engine)() as session:
        keys = session.scalars(query.limit(MAX_COHORT_ROWS + 1)).all()
    value.update(unobserved=min(len(keys), MAX_COHORT_ROWS),
                 unobserved_is_lower_bound=len(keys) > MAX_COHORT_ROWS, scan_complete=not keys)
    return value


def _rows(engine: Engine, day: date, *, packets: bool) -> list[dict[str, Any]]:
    with session_factory(engine)() as session:
        query = select(AutomaticAdmission).where(AutomaticAdmission.source_day == day)
        size = session.scalar(select(func.coalesce(func.sum(_byte_length(engine, AutomaticAdmission.payload_json)), 0))
                              .where(AutomaticAdmission.source_day == day))
        if int(size or 0) > MAX_EXPORT_BYTES:
            raise ValueError("cohort_export_too_large")
        admissions = session.scalars(query.order_by(AutomaticAdmission.observed_at, AutomaticAdmission.admission_id)
                                    .limit(MAX_COHORT_ROWS + 1)).all()
        if len(admissions) > MAX_COHORT_ROWS:
            raise ValueError("cohort_row_limit_exceeded")
        original_ids = [row.request_id for row in admissions if row.request_id]
        source_ids = [row.source_primary_key for row in admissions if row.request_id]
        request_scope = or_(storage.ThetaRequest.request_id.in_(original_ids), and_(
            storage.ThetaRequest.source_kind == "opening_entry", storage.ThetaRequest.source_id.in_(source_ids)))
        request_size = session.scalar(select(func.coalesce(func.sum(_byte_length(engine, storage.ThetaRequest.payload_json)), 0))
                                      .where(request_scope)) if original_ids else 0
        if int(request_size or 0) > MAX_EXPORT_BYTES:
            raise ValueError("cohort_export_too_large")
        request_rows = session.scalars(select(storage.ThetaRequest).where(request_scope)
                                       .limit(MAX_COHORT_ROWS * (storage.MAX_AUTOMATIC_RETRIES + 1) + 1)).all() if original_ids else []
        if len(request_rows) > MAX_COHORT_ROWS * (storage.MAX_AUTOMATIC_RETRIES + 1):
            raise ValueError("cohort_row_limit_exceeded")
        requests = {row.request_id: row for row in request_rows}
        ids = list(requests)
        # Crucially, status/export metadata never SELECT payload_json or result_json.
        receipt_rows = session.execute(select(
            storage.ThetaReceipt.receipt_id, storage.ThetaReceipt.request_id, storage.ThetaReceipt.attempt,
            storage.ThetaReceipt.received_at, storage.ThetaReceipt.upload_sha256,
            storage.ThetaReceipt.status, storage.ThetaReceipt.finalized,
        ).where(storage.ThetaReceipt.request_id.in_(ids)).order_by(storage.ThetaReceipt.attempt)
          .limit(MAX_COHORT_ROWS * storage.MAX_ATTEMPTS * (storage.MAX_AUTOMATIC_RETRIES + 1) + 1)).all() if ids else []
        if len(receipt_rows) > MAX_COHORT_ROWS * storage.MAX_ATTEMPTS * (storage.MAX_AUTOMATIC_RETRIES + 1):
            raise ValueError("cohort_row_limit_exceeded")
        by_request: dict[str, list[dict[str, Any]]] = {}
        for receipt_id, request_id, attempt, received_at, checksum, receipt_status, finalized in receipt_rows:
            if storage.digest(request_id + ":" + checksum) != receipt_id:
                raise ValueError("receipt_integrity_error")
            by_request.setdefault(request_id, []).append({
                "receipt_id": receipt_id, "request_id": request_id, "attempt": attempt,
                "received_at": storage.utc(received_at).isoformat(), "upload_sha256": checksum,
                "status": receipt_status, "finalized": bool(finalized), "result": {"status": receipt_status},
            })
        result: list[dict[str, Any]] = []
        for row in admissions:
            if storage.digest(row.payload_json) != row.payload_sha256:
                raise ValueError("admission_integrity_error")
            payload = json.loads(row.payload_json)
            request = requests.get(row.request_id) if row.request_id else None
            if request is not None and (storage.digest(request.payload_json) != request.request_hash
                                        or json.loads(request.payload_json) != payload["request"]):
                raise ValueError("request_integrity_error")
            lineage: list[dict[str, Any]] = []
            active = request
            if request is not None:
                candidates = {key: item for key, item in requests.items()
                              if item.source_kind == "opening_entry" and item.source_id == row.source_primary_key}
                candidates[request.request_id] = request
                children: dict[str, list[storage.ThetaRequest]] = {}
                for item in candidates.values():
                    saved_payload = json.loads(item.payload_json)
                    if (storage.digest(item.payload_json) != item.request_hash
                            or item.request_id != "theta_" + item.request_hash[:32]
                            or item.option_symbol != saved_payload.get("option_symbol")
                            or storage.utc(item.planned_entry_at).isoformat() != saved_payload.get("planned_entry_at")
                            or item.source_kind != saved_payload.get("source_kind")
                            or item.source_id != saved_payload.get("source_id")):
                        raise ValueError("request_integrity_error")
                    parent = saved_payload.pop("retry_of", None)
                    if saved_payload != payload["request"]:
                        raise ValueError("retry_source_identity_mismatch")
                    if parent is not None:
                        if not isinstance(parent, str):
                            raise ValueError("invalid_retry_lineage")
                        children.setdefault(parent, []).append(item)
                while active is not None:
                    if len(lineage) > storage.MAX_AUTOMATIC_RETRIES:
                        raise ValueError("automatic_retry_lineage_limit")
                    receipts = by_request.get(active.request_id, [])
                    lineage.append({"request": storage.request_view(active), "payload": json.loads(active.payload_json),
                                    "receipt_ids": [receipt["receipt_id"] for receipt in receipts]})
                    next_requests = children.get(active.request_id, [])
                    if not next_requests:
                        break
                    if (len(next_requests) != 1 or len(receipts) != storage.MAX_ATTEMPTS
                            or any(receipt["finalized"] for receipt in receipts)):
                        raise ValueError("invalid_retry_lineage")
                    active = next_requests[0]
                if len(lineage) != len(candidates):
                    raise ValueError("invalid_retry_lineage")
            all_receipts = [receipt for item in lineage
                            for receipt in by_request.get(item["request"]["request_id"], [])]
            result.append({"admission_id": row.admission_id, "source_primary_key": row.source_primary_key,
                           "disposition": row.disposition, "reason": row.reason, "source": payload["source"],
                           "admission_sha256": row.payload_sha256, "request_id": row.request_id,
                           "active_request_id": active.request_id if active else row.request_id,
                           "retry_depth": max(0, len(lineage) - 1), "request_lineage": lineage,
                           "request": storage.request_view(active) if active else None,
                           "receipts": all_receipts})
    if packets:
        all_receipts = [receipt for row in result for receipt in row["receipts"]]
        _receipt_budget(engine, [receipt["receipt_id"] for receipt in all_receipts], MAX_EXPORT_BYTES)
        for receipt in all_receipts:
            receipt["upload"] = _raw_upload(engine, receipt)
    return result


def cohort(engine: Engine, day: date, *, include_packets: bool = False) -> dict[str, Any]:
    if not available(engine):
        raise ValueError("storage_unavailable")
    rows = _rows(engine, day, packets=include_packets)
    source_context = _source_context(engine, day)
    source_basis = [{key: row[key] for key in ("admission_id", "admission_sha256", "disposition", "reason", "request_id")} for row in rows]
    inputs = [{"admission_id": row["admission_id"], "requests": [
        {key: item["request"][key] for key in ("request_id", "request_hash", "retry_of")}
        for item in row["request_lineage"]], "receipts": [
        {key: receipt[key] for key in ("receipt_id", "upload_sha256", "attempt", "received_at", "finalized")}
        for receipt in row["receipts"]]} for row in rows]
    value = {"method": METHOD, "day": day.isoformat(), "cohort_hash": storage.digest(storage.canonical({"rows": source_basis, "source_context": source_context})),
             "input_hash": storage.digest(storage.canonical(inputs)), "rows": rows, "truncated": False,
             "raw_packets_included": include_packets, "raw_packets_retained_on_server": True,
             "limits": {"rows": MAX_COHORT_ROWS, "bytes": MAX_EXPORT_BYTES},
             "source_context": source_context,
             "data_use": "fixed_one_contract_quote_markout_audit_not_strategy_optimization"}
    if len(storage.canonical(value).encode()) > MAX_EXPORT_BYTES:
        raise ValueError("cohort_export_too_large")
    return value


def _active_receipts(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [receipt for receipt in row["receipts"] if receipt["request_id"] == row["active_request_id"]]


def _state(row: dict[str, Any], now: datetime) -> str:
    if row["disposition"] == "excluded":
        return "excluded"
    receipts = _active_receipts(row)
    if receipts and receipts[-1]["finalized"]:
        return "evaluated" if receipts[-1]["result"].get("status") == "EVALUATED" else "unknown"
    if len(receipts) >= storage.MAX_ATTEMPTS:
        return "retry_limit"
    target = datetime.fromisoformat(row["source"]["planned_entry_at"])
    return "pending_maturity" if now < target + timedelta(minutes=64) else "awaiting_connector"


def _counts(rows: list[dict[str, Any]], now: datetime) -> dict[str, int]:
    states = [_state(row, now) for row in rows]
    return {"observed": len(rows), "admitted": sum(row["disposition"] == "admitted" for row in rows),
            "excluded": states.count("excluded"), "pending": states.count("pending_maturity") + states.count("awaiting_connector"),
            "pending_maturity": states.count("pending_maturity"), "awaiting_connector": states.count("awaiting_connector"),
            "evaluated": states.count("evaluated"), "unknown": states.count("unknown"), "retry_limit": states.count("retry_limit")}


def rerun(engine: Engine, day: date, now: datetime) -> dict[str, Any]:
    moment = aware(now)
    exported = cohort(engine, day)
    calendar_hash = storage.digest(storage.canonical(load_live_alpha_settings().calendar.model_dump(mode="json")))
    identity = storage.digest(storage.canonical({"method": METHOD, "implementation_hash": implementation_hash(),
                                                "calendar_hash": calendar_hash, "day": day.isoformat(),
                                                "cohort_hash": exported["cohort_hash"], "input_hash": exported["input_hash"]}))
    with session_factory(engine)() as session:
        existing = session.get(AutomaticReport, identity)
        if existing is not None:
            if storage.digest(existing.payload_json) != existing.payload_sha256:
                raise ValueError("report_integrity_error")
            return cast(dict[str, Any], json.loads(existing.payload_json))
    # Replay each latest packet once, retaining only the two compact markouts.
    # Never load a day's raw packets together merely to compute its report.
    marks: dict[str, list[dict[str, Any]]] = {}
    for row in exported["rows"]:
        receipts = _active_receipts(row)
        if row["disposition"] != "admitted" or not receipts or row["request"] is None:
            continue
        receipt = receipts[-1]
        _receipt_budget(engine, [receipt["receipt_id"]], MAX_EXPORT_BYTES)
        upload = _raw_upload(engine, receipt)
        evaluated = service.evaluate_upload(row["request"], service.UploadSpec.model_validate(upload),
                                            datetime.fromisoformat(receipt["received_at"]))
        marks[row["admission_id"]] = evaluated.result["markouts"]
        receipt.update(status=evaluated.status, finalized=evaluated.finalized,
                       result={"status": evaluated.result["status"]})
        del evaluated, upload
    groups = []
    for basis in (PROSPECTIVE, RETROSPECTIVE):
        rows = [row for row in exported["rows"] if row["source"]["entry_basis"] == basis]
        horizons = []
        for minutes in (15, 60):
            amounts: list[Decimal] = []
            unknown = pending = 0
            for row in rows:
                if row["disposition"] != "admitted":
                    continue
                if not _active_receipts(row) or row["request"] is None:
                    pending += 1
                    continue
                mark = next(item for item in marks[row["admission_id"]] if item["horizon_minutes"] == minutes)
                if mark["status"] == "EVALUATED":
                    amounts.append(Decimal(mark["modeled_net_pnl_usd"]))
                else:
                    unknown += 1
            horizons.append({"minutes": minutes, "evaluated": len(amounts), "unknown": unknown, "pending": pending,
                             "positive": sum(value > 0 for value in amounts), "negative": sum(value < 0 for value in amounts),
                             "zero": sum(value == 0 for value in amounts),
                             "modeled_net_pnl_usd_total": str(sum(amounts, Decimal(0))) if amounts else None})
        groups.append({"entry_basis": basis, "counts": _counts(rows, moment), "horizons": horizons})
    result = {"report_id": identity, "day": day.isoformat(), "created_at": moment.isoformat(),
              "method": METHOD, "implementation_hash": implementation_hash(), "calendar_hash": calendar_hash,
              "cohort_hash": exported["cohort_hash"],
              "input_hash": exported["input_hash"], "groups": groups, "status": "DESCRIPTIVE_ONLY",
              "source_context": exported["source_context"],
              "costs": {"contracts": 1, "multiplier": "100_assumed_unverified", "slippage_per_share_per_side": "0.01", "fee_per_side_usd": "0.65"},
              "quote_horizons_minutes": [15, 60], "execution_proven": False, "winner": None,
              "interpretation": "Stored native packets recomputed; alternative quote markouts, not portfolio profit or evidence of edge."}
    body = storage.canonical(result)
    _insert(engine, AutomaticReport, {"report_id": identity, "day": day, "created_at": moment,
                                      "payload_json": body, "payload_sha256": storage.digest(body)}, "report_id")
    with session_factory(engine)() as session:
        saved = session.get(AutomaticReport, identity)
        assert saved is not None
        if storage.digest(saved.payload_json) != saved.payload_sha256:
            raise ValueError("report_integrity_error")
        return cast(dict[str, Any], json.loads(saved.payload_json))


def status(engine: Engine, now: datetime, day: date | None = None) -> dict[str, Any]:
    moment = aware(now)
    selected = day or moment.astimezone(service.ET).date()
    value: dict[str, Any] = {"method": METHOD, "status": "storage_unavailable", "as_of": moment.isoformat(),
                            "day": selected.isoformat(), "activation_at": None, "catchup_start": None,
                            "last_tick_at": STATE.get("at"), "last_tick_status": STATE.get("status"),
                            "counts": _counts([], moment), "recent": [], "latest_report": None}
    if not available(engine):
        return value
    activation = _activation(engine)
    rows = _rows(engine, selected, packets=False)
    tick = STATE.get("at")
    health = "awaiting_observer"
    if activation is not None and isinstance(tick, str):
        if moment - datetime.fromisoformat(tick) > timedelta(seconds=180):
            health = "stale"
        else:
            health = "ready" if STATE.get("status") == "ok" else str(STATE.get("status", "failed"))
    value.update(status=health if enabled() else "disabled", counts=_counts(rows, moment),
                 source_context=_source_context(engine, selected),
                 activation_at=storage.utc(activation.activated_at).isoformat() if activation else None,
                 catchup_start=storage.utc(activation.catchup_start).isoformat() if activation else None)
    value["recent"] = [{"admission_id": row["admission_id"], "source_primary_key": row["source_primary_key"],
                        **{key: row["source"].get(key) for key in ("ticker", "source_day", "source_kind", "entry_basis", "option_symbol", "planned_entry_at", "instrument")},
                        "disposition": row["disposition"], "reason": row["reason"], "request_id": row["active_request_id"],
                        "original_request_id": row["request_id"], "retry_depth": row["retry_depth"],
                        "retry_available": _state(row, moment) == "retry_limit" and row["retry_depth"] < storage.MAX_AUTOMATIC_RETRIES,
                        "state": _state(row, moment)}
                       for row in rows[-32:]]
    with session_factory(engine)() as session:
        latest = session.scalar(select(AutomaticReport).where(AutomaticReport.day == selected).order_by(AutomaticReport.created_at.desc()).limit(1))
        if latest:
            if storage.digest(latest.payload_json) != latest.payload_sha256:
                raise ValueError("report_integrity_error")
            value["latest_report"] = json.loads(latest.payload_json)
    return value


def safe_status() -> dict[str, object]:
    return {key: value for key, value in STATE.items() if key in {"status", "at", "admitted", "excluded", "resumed", "reports"}}


def configured_engine(database_url: str) -> Engine:
    """One small worker pool with database-side deadlines."""
    normalized = normalize_database_url(database_url)
    parsed = make_url(normalized)
    if parsed.get_backend_name() != "postgresql":
        return create_engine(normalized, future=True, pool_pre_ping=True)
    options = parsed.query.get("options", os.environ.get("PGOPTIONS", ""))
    if not isinstance(options, str):
        raise ValueError("ambiguous_database_options")
    protected = (options + " -c statement_timeout=15000 -c lock_timeout=1000"
                 " -c max_parallel_workers_per_gather=0").strip()
    return create_engine(normalized, future=True, pool_pre_ping=True, pool_size=1, max_overflow=0,
                         pool_timeout=5, connect_args={"options": protected, "connect_timeout": 5})


async def loop(database_url: str, *, clock: Callable[[], datetime] | None = None,
               sleep: Callable[[float], Awaitable[None]] | None = None, max_cycles: int | None = None) -> None:
    engine = configured_engine(database_url)
    now = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            moment = aware(now())
            try:
                await asyncio.to_thread(run_once, engine, moment, clock=now)
                if enabled() and available(engine):
                    with session_factory(engine)() as session:
                        days = session.scalars(select(AutomaticAdmission.source_day).distinct()
                                               .order_by(AutomaticAdmission.source_day.desc()).limit(8)).all()
                    for day in days:
                        await asyncio.to_thread(rerun, engine, day, aware(now()))
                    STATE["reports"] = len(days)
            except Exception:
                STATE.clear()
                STATE.update(status="failed", at=moment.isoformat())
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                await pause(60)
    finally:
        engine.dispose()
