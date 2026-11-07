"""Private bounded projection: model interpretation is never publisher fact."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from webapp.semantic_news import config, sources, storage
from webapp.semantic_news.model import Envelope, Metadata, Record, Request, aware, digest
from webapp.semantic_news.validation import decode, validate
from webapp.shadow_capture import storage as shadow


@dataclass(frozen=True)
class EvidenceView:
    quote: str
    field: str


@dataclass(frozen=True)
class ItemView:
    id: str
    ticker: str
    relation: str
    event: str
    direction: str
    certainty: str
    materiality: str
    reason: str
    evidence: tuple[EvidenceView, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    candidate_key: str
    ticker: str
    status: str = "unknown"
    direction: str = "unknown"
    supports: bool | None = None
    available_at: datetime | None = None
    request_id: str | None = None
    source_hash: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    schema_hash: str | None = None
    eligible_before_entry: bool = False
    reason: str = ""
    input_count: int = 0
    input_limited: bool = False
    items: tuple[ItemView, ...] = ()
    source_packet_key: str | None = None
    source_payload_hash: str | None = None
    analysis_started_at: datetime | None = None
    analysis_finished_at: datetime | None = None
    requested_at: datetime | None = None
    received_at: datetime | None = None
    entry_target: datetime | None = None
    deadline: datetime | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    checked_empty: bool = False


@dataclass(frozen=True)
class Report:
    as_of: datetime
    day: date
    status: str
    reason: str
    method: str = "semantic_news_v1"
    configured_enabled: bool = False
    profile_hash: str = ""
    implementation_hash: str = ""
    reserved: int = 0
    completed: int = 0
    pending: int = 0
    unknown: int = 0
    checked_empty: int = 0
    skipped: int = 0
    limited: bool = False
    rows: tuple[Row, ...] = ()
    inference: str = "NOT_PERFORMED"
    winner: None = None


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("clock_missing")
    return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _unknown(record: Record, reason: str, status: str = "unknown") -> Row:
    return Row(
        str(record.payload.get("candidate_key", record.unique_owner or "")),
        str(record.payload.get("ticker", "")),
        status,
        "unknown",
        None,
        record.confirmed_at,
        None,
        None,
        None,
        None,
        None,
        False,
        reason,
        0,
        False,
    )


def _row(engine: Engine, claim: Record, now: datetime) -> Row:
    if claim.payload.get("identity") != config.identity():
        return _unknown(claim, "different_method_identity")
    try:
        target = _time(claim.payload.get("entry_target"))
        deadline = _time(claim.payload.get("deadline"))
        request = Request.model_validate(claim.payload.get("request"))
        if not request.valid(claim.at):
            raise ValueError("request_binding_invalid")
        bound = storage.get(engine, claim.parent_key or "", now)
        if (
            bound is None
            or bound.kind != "binding"
            or bound.confirmed_at is None
            or bound.confirmed_at > claim.at
            or bound.payload.get("identity") != config.identity()
        ):
            raise ValueError("model_binding_unverified")
        metadata = Metadata.model_validate(bound.payload.get("metadata"))
        if (
            not metadata.configured
            or metadata.prompt_hash != config.settings().expected_prompt_hash
            or metadata.schema_hash != config.settings().expected_schema_hash
            or metadata.binding() != claim.payload.get("binding")
        ):
            raise ValueError("model_binding_changed")
        candidate = shadow.get(engine, claim.unique_owner or "", now)
        if candidate is None or candidate.checksum != claim.payload.get("candidate_checksum"):
            raise ValueError("candidate_unverified")
        original = sources.source(engine, candidate, claim.at)
        if (
            original.request != request
            or original.entry_target != target
            or original.candidate_key != claim.payload.get("candidate_key")
            or original.source_packet_key != claim.payload.get("source_packet_key")
            or original.source_payload_hash != claim.payload.get("source_payload_hash")
            or original.source_available_at.isoformat() != claim.payload.get("source_available_at")
        ):
            raise ValueError("source_provenance_changed")
        from datetime import timedelta

        if deadline != target - timedelta(seconds=config.settings().min_completion_lead_seconds):
            raise ValueError("entry_deadline_changed")
        base = Row(
            candidate_key=original.candidate_key,
            ticker=original.ticker,
            request_id=request.request_id,
            source_hash=request.source_hash,
            model=metadata.model,
            prompt_hash=metadata.prompt_hash,
            schema_hash=metadata.schema_hash,
            input_count=len(request.items),
            input_limited=bool(claim.payload.get("input_limited")),
            source_packet_key=original.source_packet_key,
            source_payload_hash=original.source_payload_hash,
            entry_target=target,
            deadline=deadline,
        )
        if claim.confirmed_at is None:
            return replace(
                base,
                status="pending" if now <= deadline else "unknown",
                direction="unknown",
                supports=None,
                available_at=None,
                eligible_before_entry=False,
                reason="claim_confirmation_missing",
            )
        response_key = digest(("semantic_response", claim.key))
        response = storage.get(engine, response_key, now)
        if response is None:
            return replace(
                base,
                status="pending" if now <= deadline else "unknown",
                direction="unknown",
                supports=None,
                available_at=None,
                eligible_before_entry=False,
                reason="response_pending_or_unconfirmed",
            )
        payload = response.payload
        if (
            response.kind != "response"
            or response.parent_key != claim.key
            or response.payload.get("identity") != config.identity()
            or response.day != claim.day
            or response.confirmed_at is None
            or response.at < claim.confirmed_at
            or payload.get("request_id") != request.request_id
            or payload.get("candidate_key") != original.candidate_key
            or payload.get("binding") != metadata.binding()
        ):
            raise ValueError("response_binding_unverified")
        requested, received = _time(payload.get("requested_at")), _time(payload.get("received_at"))
        if (
            not claim.confirmed_at
            <= requested
            <= received
            <= response.at
            <= response.confirmed_at
            <= now
        ):
            raise ValueError("wire_chronology_unverified")
        eligible = response.confirmed_at <= deadline
        if payload.get("status") != "completed":
            pending = payload.get("status") == "pending" and now <= deadline
            return replace(
                base,
                status="pending" if pending else "unknown",
                direction="unknown",
                supports=None,
                available_at=response.confirmed_at,
                eligible_before_entry=False,
                reason=str(payload.get("reason", "analysis_unknown")),
                requested_at=requested,
                received_at=received,
            )
        if (
            response.blob is None
            or payload.get("http_status") != 200
            or payload.get("wire_attempted") is not True
        ):
            raise ValueError("raw_response_unverified")
        envelope = Envelope.model_validate(decode(response.blob))
        assessment = validate(request, envelope, metadata, received)
        if envelope.status != "completed" or envelope.model_dump(mode="json") != payload.get(
            "envelope"
        ):
            raise ValueError("stored_interpretation_changed")
        if (
            payload.get("direction") != assessment.direction
            or payload.get("supports") is not assessment.supports
            or payload.get("reason") != assessment.reason
        ):
            raise ValueError("stored_assessment_changed")
        items = tuple(
            ItemView(
                i.id,
                i.ticker,
                i.relation,
                i.event,
                i.direction,
                i.certainty,
                i.materiality,
                i.reason,
                tuple(EvidenceView(e.quote, e.field) for e in i.evidence),
                i.limitations,
            )
            for i in envelope.items
        )
        return replace(
            base,
            status="completed" if eligible else "late",
            direction=assessment.direction,
            supports=assessment.supports if eligible else None,
            available_at=response.confirmed_at,
            eligible_before_entry=eligible,
            reason=assessment.reason if eligible else "interpretation_after_entry_cutoff",
            items=items,
            analysis_started_at=envelope.analysis_started_at,
            analysis_finished_at=envelope.analysis_finished_at,
            requested_at=requested,
            received_at=received,
            input_tokens=envelope.usage.input_tokens,
            output_tokens=envelope.usage.output_tokens,
            cost_usd=envelope.usage.cost_usd,
        )
    except (ValueError, TypeError):
        return _unknown(claim, "interpretation_or_source_unverified")


def _skip_row(engine: Engine, record: Record, now: datetime) -> Row:
    if record.payload.get("checked_empty") is not True:
        return _unknown(record, str(record.payload.get("reason", "source_unknown")), "skipped")
    try:
        if record.confirmed_at is None:
            raise ValueError("skip_unconfirmed")
        candidate = shadow.get(engine, str(record.payload.get("candidate_key", "")), now)
        if candidate is None or candidate.checksum != record.payload.get("candidate_checksum"):
            raise ValueError("empty_candidate_unverified")
        original = sources.empty_source(engine, candidate, record.at)
        if (
            original is None
            or original.source_packet_key != record.payload.get("source_packet_key")
            or original.source_payload_hash != record.payload.get("source_payload_hash")
            or original.reason != record.payload.get("reason")
        ):
            raise ValueError("empty_source_unverified")
        from datetime import timedelta

        deadline = original.entry_target - timedelta(
            seconds=config.settings().min_completion_lead_seconds
        )
        eligible = record.confirmed_at <= deadline
        return Row(
            candidate_key=original.candidate_key,
            ticker=original.ticker,
            status="skipped",
            direction="neutral",
            supports=False if eligible else None,
            available_at=record.confirmed_at,
            eligible_before_entry=eligible,
            reason=original.reason if eligible else "empty_receipt_after_cutoff",
            source_packet_key=original.source_packet_key,
            source_payload_hash=original.source_payload_hash,
            entry_target=original.entry_target,
            deadline=deadline,
            checked_empty=eligible,
        )
    except (ValueError, TypeError):
        return _unknown(record, "empty_source_unverified", "skipped")


def build(engine: Engine, now: datetime, day: date | None = None) -> Report:
    at = aware(now)
    selected = day or at.astimezone(ZoneInfo("America/New_York")).date()
    base = Report(
        as_of=at,
        day=selected,
        status="AWAITING_CANDIDATE",
        reason="",
        configured_enabled=config.enabled(),
        profile_hash=config.profile_hash(),
        implementation_hash=config.implementation_hash(),
    )
    try:
        if not storage.available(engine):
            return replace(
                base, status="STORAGE_UNAVAILABLE", reason="Yorum kayıtları henüz okunamıyor."
            )
        claims = storage.list_events(
            engine, "claim", at, day=selected, limit=5, require_receipt=False
        )
        rows = [_row(engine, c, at) for c in claims]
        skips = storage.list_events(engine, "skip", at, day=selected, limit=5)
        claimed = {r.candidate_key for r in rows}
        rows.extend(
            _skip_row(engine, s, at)
            for s in skips
            if str(s.payload.get("candidate_key")) not in claimed
            and s.payload.get("identity") == config.identity()
        )
        reserved = storage.count(engine, "claim", at, day=selected, include_future=True)
        completed = sum(r.status == "completed" for r in rows)
        pending = sum(r.status == "pending" for r in rows)
        unknown = sum(
            r.status not in {"completed", "pending"} and not r.checked_empty for r in rows
        )
        state = (
            "HALTED"
            if storage.list_events(engine, "halt", at, day=selected, require_receipt=False)
            else "AVAILABLE"
            if rows
            else "AWAITING_CANDIDATE"
        )
        return replace(
            base,
            status=state,
            reason="Kaynak kontrolleri ve model yorumları ayrı deneysel kanıttır; işlem başarısı olasılığı değildir.",
            reserved=reserved,
            completed=completed,
            pending=pending,
            unknown=unknown,
            checked_empty=sum(r.checked_empty for r in rows),
            skipped=sum(r.status == "skipped" for r in rows),
            limited=reserved > config.settings().daily_analyses,
            rows=tuple(rows),
        )
    except Exception:
        return replace(base, status="STORAGE_UNAVAILABLE", reason="Yorum kayıtları doğrulanamadı.")
