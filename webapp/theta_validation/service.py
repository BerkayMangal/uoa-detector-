"""Validate connector receipts and recompute economics on the application server."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.options_alpha.theta_quotes import (
    ThetaContract,
    ThetaQuoteBatch,
    evaluate_theta_long_option,
    parse_theta_quote_payload,
    select_first_theta_quote,
)
from webapp.board.cards import AlfaDecisionCard
from webapp.board.db import session_factory
from webapp.theta_validation import storage

ET = ZoneInfo("America/New_York")
Failure = Literal[
    "terminal_unreachable",
    "authentication_failed",
    "unsupported_payload",
    "request_failed",
    "response_too_large",
    "invalid_request",
]


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    option_symbol: str | None = Field(default=None, max_length=21)
    planned_entry_at: AwareDatetime | None = None
    source_kind: Literal["manual_research", "decision_card"] = "manual_research"
    source_id: str | None = Field(default=None, max_length=100)
    source_label: str = Field(default="Elle seçilen araştırma", max_length=120)


class PacketSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: Literal["entry", "m15", "m60"]
    window_start: AwareDatetime
    window_end: AwareDatetime
    received_at: AwareDatetime
    requested_interval: Literal["tick"]
    http_status: Literal[200, 472]
    # Native provider JSON is the explicit adapter boundary, not trusted prices.
    payload: Any


class UploadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempted_at: AwareDatetime | None = None
    status: Literal[
        "ok",
        "terminal_unreachable",
        "authentication_failed",
        "unsupported_payload",
        "request_failed",
        "response_too_large",
        "invalid_request",
    ] = "ok"
    packets: list[PacketSpec] = Field(default_factory=list, max_length=3)


def _valid_entry(symbol: str, at: datetime) -> None:
    contract = ThetaContract.from_occ(symbol)
    local = at.astimezone(ET)
    calendar = load_live_alpha_settings().calendar
    if is_session_day(
        local.date(), calendar
    ) is not True or not calendar.regular_open <= local.time() < close_time(local.date(), calendar):
        raise ValueError("entry_outside_known_regular_session")
    if contract.expiry < local.date():
        raise ValueError("contract_expired_before_entry")


def sources(engine: Engine, now: datetime) -> dict[str, Any]:
    if AlfaDecisionCard.__tablename__ not in inspect(engine).get_table_names():
        return {"sources": []}
    with session_factory(engine)() as session:
        rows = list(
            session.scalars(
                select(AlfaDecisionCard)
                .where(
                    AlfaDecisionCard.dominant_option_symbol.is_not(None),
                    AlfaDecisionCard.created_at <= now,
                )
                .order_by(AlfaDecisionCard.created_at.desc())
                .limit(32)
            )
        )
        result = []
        for row in rows:
            symbol = row.dominant_option_symbol
            if not symbol:
                continue
            at = storage.utc(row.created_at) + timedelta(seconds=60)
            try:
                _valid_entry(symbol, at)
            except ValueError:
                continue
            result.append(
                {
                    "source_kind": "decision_card",
                    "source_id": row.id,
                    "option_symbol": symbol,
                    "planned_entry_at": at.isoformat(),
                    "ticker": row.ticker,
                    "source_label": f"UOA kayıtlı karar · {row.ticker} · {row.decision}",
                    "entry_basis": "saved_decision_time_plus_60_seconds",
                    "original_option_trade": False,
                }
            )
        return {"sources": result}


def create_request(engine: Engine, spec: RequestSpec, now: datetime) -> dict[str, Any]:
    if not storage.available(engine):
        raise ValueError("storage_unavailable")
    if spec.source_kind == "decision_card":
        if not spec.source_id:
            raise ValueError("source_id_required")
        source = next(
            (row for row in sources(engine, now)["sources"] if row["source_id"] == spec.source_id),
            None,
        )
        if source is None:
            raise ValueError("saved_source_not_available")
        if spec.option_symbol is not None and spec.option_symbol != source["option_symbol"]:
            raise ValueError("source_contract_mismatch")
        at = datetime.fromisoformat(source["planned_entry_at"])
        if spec.planned_entry_at is not None and spec.planned_entry_at != at:
            raise ValueError("source_entry_time_mismatch")
        symbol, label = source["option_symbol"], source["source_label"]
    else:
        if spec.source_id is not None:
            raise ValueError("manual_source_cannot_claim_saved_identity")
        if spec.option_symbol is None or spec.planned_entry_at is None:
            raise ValueError("manual_contract_and_entry_time_required")
        symbol, at, label = (
            spec.option_symbol,
            spec.planned_entry_at.astimezone(UTC),
            spec.source_label,
        )
        if at > now:
            raise ValueError("manual_research_entry_must_be_historical")
    _valid_entry(symbol, at)
    local = at.astimezone(ET)
    calendar = load_live_alpha_settings().calendar
    session_close = datetime.combine(local.date(), close_time(local.date(), calendar), ET)
    return storage.add_request(
        engine,
        {
            "version": "theta_validation_v1",
            "option_symbol": symbol,
            "planned_entry_at": at.astimezone(UTC).isoformat(),
            "source_kind": spec.source_kind,
            "source_id": spec.source_id,
            "source_label": label,
            "entry_basis": "saved_decision_time_plus_60_seconds"
            if spec.source_kind == "decision_card"
            else "owner_selected_historical_time",
            "original_option_trade": False,
            "session_close_at": session_close.astimezone(UTC).isoformat(),
        },
        now,
    )


def pending_jobs(engine: Engine, now: datetime, limit: int) -> dict[str, Any]:
    view = storage.view(engine, now=now)
    # Entry may wait 120s, then the60-minute exit may wait another120s.
    return {
        "status": view["status"],
        "jobs": storage.pending(engine, now, limit),
        "as_of": now.isoformat(),
    }


def _failure_result(request: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "source": "thetadata",
        "status": "UNKNOWN",
        "reason": reason,
        "option_symbol": request["option_symbol"],
        "planned_entry_at": request["planned_entry_at"],
        "entry_quote": None,
        "data_use": "retrospective_quote_markout",
        "executable_fill_proven": False,
        "markouts": [
            {
                "horizon_minutes": minutes,
                "status": "UNKNOWN",
                "reason": reason,
                "modeled_net_pnl_usd": None,
            }
            for minutes in (15, 60)
        ],
    }


def ingest(engine: Engine, request_id: str, upload: UploadSpec, now: datetime) -> dict[str, Any]:
    request = storage.get_request(engine, request_id)
    if request is None:
        raise ValueError("request_not_found")
    if upload.request_hash != request["request_hash"]:
        raise ValueError("request_hash_mismatch")
    if upload.attempted_at is not None and upload.attempted_at > now:
        raise ValueError("attempt_time_in_future")
    body = upload.model_dump(mode="json")
    # Reject NaN and non-JSON objects before any stored receipt is written.
    storage.canonical(body)
    if upload.status != "ok":
        if upload.packets:
            raise ValueError("failure_upload_must_not_contain_packets")
        result = _failure_result(request, upload.status)
        return storage.append_receipt(
            engine,
            request_id=request_id,
            upload=body,
            result=result,
            status=upload.status,
            finalized=False,
            now=now,
        )
    packets: dict[str, PacketSpec] = {packet.label: packet for packet in upload.packets}
    if len(packets) != len(upload.packets) or "entry" not in packets:
        raise ValueError("unique_entry_packet_required")
    entry_at = datetime.fromisoformat(request["planned_entry_at"])
    session_day = entry_at.astimezone(ET).date()

    def parse(label: str, target: datetime) -> ThetaQuoteBatch:
        packet = packets[label]
        if packet.window_start != target or packet.window_end != target + timedelta(seconds=120):
            raise ValueError("packet_query_window_mismatch")
        if packet.received_at > now or packet.received_at < packet.window_end:
            raise ValueError("packet_receipt_time_invalid")
        if packet.http_status == 472:
            batch = ThetaQuoteBatch(
                request["option_symbol"],
                session_day,
                packet.window_start,
                packet.window_end,
                packet.received_at,
                "no_quotes",
                "provider_http_472_no_data",
                coverage_complete=True,
                http_status=472,
                raw_sha256=storage.digest(storage.canonical(packet.payload)),
                raw_payload=packet.payload,
                requested_interval="tick",
            )
        else:
            batch = parse_theta_quote_payload(
                packet.payload,
                option_symbol=request["option_symbol"],
                session=session_day,
                window_start=packet.window_start,
                window_end=packet.window_end,
                received_at=packet.received_at,
                http_status=packet.http_status,
                raw_sha256=storage.digest(storage.canonical(packet.payload)),
                requested_interval="tick",
            )
        if (
            batch.status == "invalid_request"
            or "contract" in (batch.reason or "")
            or any("contract" in key for key in batch.rejected_rows)
        ):
            raise ValueError("packet_contract_or_request_invalid")
        return batch

    entry = parse("entry", entry_at)
    calendar = load_live_alpha_settings().calendar
    selected = select_first_theta_quote(entry, target_at=entry_at, calendar=calendar)
    exits: dict[int, ThetaQuoteBatch] = {}
    if selected is not None:
        targets = {minutes: selected.quote_at + timedelta(minutes=minutes) for minutes in (15, 60)}
        valid_targets = {
            minutes: target
            for minutes, target in targets.items()
            if target.astimezone(ET).date() == session_day
            and calendar.regular_open
            <= target.astimezone(ET).time()
            < close_time(session_day, calendar)
        }
        if set(packets) != {"entry", *(f"m{minutes}" for minutes in valid_targets)}:
            raise ValueError("both_exit_packets_required_after_valid_entry")
        for minutes, target in valid_targets.items():
            exits[minutes] = parse(f"m{minutes}", target)
    elif set(packets) != {"entry"}:
        raise ValueError("exit_packets_without_valid_entry")
    result = evaluate_theta_long_option(entry_batch=entry, exit_batches=exits, calendar=calendar)
    result.update(
        {
            "request_id": request_id,
            "request_hash": upload.request_hash,
            "source_kind": request["source_kind"],
            "source_label": request["source_label"],
            "uploaded_at": now.isoformat(),
            "source_identity_independently_verified": False,
            "provider_packets": [
                {**batch.as_dict(), "hash_basis": "canonical_json"}
                for batch in [entry, *exits.values()]
            ],
        }
    )
    finalized = all(batch.status in {"ok", "no_quotes"} for batch in [entry, *exits.values()])
    return storage.append_receipt(
        engine,
        request_id=request_id,
        upload=body,
        result=result,
        status=str(result["status"]),
        finalized=finalized,
        now=now,
    )
