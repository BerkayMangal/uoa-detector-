"""Bounded read-only financial context projection; no collection or inference."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from webapp.fundamental_context import settings as config
from webapp.fundamental_context import storage
from webapp.fundamental_context.extract import extract
from webapp.fundamental_context.model import Endpoint, Facts, Record, aware, digest
from webapp.shadow_capture import config as shadow_config
from webapp.shadow_capture import storage as shadow_store


@dataclass(frozen=True)
class Observation:
    endpoint: Endpoint
    status: str
    available_at: datetime | None = None
    reason: str = ""
    packet_key: str | None = None
    body_sha256: str | None = None
    facts: Facts | None = None


@dataclass(frozen=True)
class Row:
    ticker: str
    candidate_key: str
    status: str
    observations: tuple[Observation, ...]
    facts: tuple[Facts, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    as_of: datetime
    day: date
    status: str
    reason: str
    method: str = "fundamental_context_v1"
    configured_enabled: bool = True
    activation_at: datetime | None = None
    profile_hash: str | None = None
    implementation_hash: str | None = None
    reserved_requests: int | None = None
    candidate_count: int | None = None
    invalid_records: int = 0
    limited: bool = False
    halt_reason: str | None = None
    rows: tuple[Row, ...] = ()
    inference: str = "NOT_PERFORMED"


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("clock_missing")
    return aware(datetime.fromisoformat(value))


def _decode(raw: bytes) -> dict[str, Any]:
    # Any is confined to the original immutable vendor JSON adapter boundary.
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in values:
            if k in out:
                raise ValueError("duplicate_json_key")
            out[k] = v
        return out

    def invalid(_value: str) -> None:
        raise ValueError("nonfinite_json")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("object_required")
    return value


def _claim(
    engine: Engine, claim: Record, activation: Record, now: datetime
) -> tuple[str, str, Endpoint]:
    p = claim.payload
    ticker, endpoint, ordinal = p.get("ticker"), p.get("endpoint"), p.get("slot")
    candidate = (
        shadow_store.get(engine, claim.parent_key or "", now)
        if shadow_store.available(engine)
        else None
    )
    shadow = shadow_store.active(engine, now)
    endpoints = (config.settings().statement_endpoint, "earnings")
    if (
        claim.kind != "claim"
        or p.get("identity") != config.identity()
        or p.get("activation_key") != activation.key
        or claim.confirmed_at is None
        or activation.confirmed_at is None
        or claim.at < activation.confirmed_at
        or candidate is None
        or candidate.kind != "candidate"
        or shadow is None
        or candidate.parent_key != shadow.key
        or candidate.payload.get("identity") != shadow_config.identity()
        or candidate.confirmed_at is None
        or not activation.confirmed_at <= candidate.confirmed_at <= claim.at
        or candidate.day != claim.day
        or candidate.checksum != p.get("candidate_checksum")
        or candidate.key != p.get("candidate_key")
        or candidate.payload.get("ticker") != ticker
        or not isinstance(ticker, str)
        or endpoint not in endpoints
        or type(ordinal) is not int
        or not 1 <= ordinal <= config.settings().max_requests_per_day
        or claim.key != digest((config.settings().version, "claim", claim.day, ordinal))
        or claim.unique_owner
        != digest((config.settings().version, "request_owner", claim.day, ticker, endpoint))
    ):
        raise ValueError("claim_unverified")
    slot = candidate.payload.get("slot")
    if (
        type(slot) is not int
        or not 1 <= slot <= config.settings().max_tickers_per_day
        or ordinal != (slot - 1) * len(endpoints) + endpoints.index(endpoint) + 1
    ):
        raise ValueError("claim_slot_unverified")
    return ticker, candidate.key, cast(Endpoint, endpoint)


def _observation(
    engine: Engine, claim: Record, endpoint: Endpoint, ticker: str, now: datetime
) -> Observation:
    key = digest((config.settings().version, "packet", claim.key))
    packet = storage.get(engine, key, now)
    if packet is None:
        return Observation(endpoint, "unknown", reason="packet_pending_or_unverified")
    p = packet.payload
    if (
        packet.kind != "packet"
        or packet.parent_key != claim.key
        or packet.day != claim.day
        or p.get("identity") != claim.payload.get("identity")
        or p.get("activation_key") != claim.payload.get("activation_key")
        or p.get("candidate_key") != claim.parent_key
        or p.get("ticker") != ticker
        or p.get("endpoint") != endpoint
        or packet.confirmed_at is None
        or claim.confirmed_at is None
        or packet.at < claim.confirmed_at
    ):
        return Observation(endpoint, "unknown", reason="packet_chain_unverified")
    if p.get("status") != "ok":
        return Observation(
            endpoint,
            "unavailable",
            packet.confirmed_at,
            str(p.get("reason", "source_unavailable")),
            packet.key,
        )
    try:
        requested, received = _time(p.get("requested_at")), _time(p.get("received_at"))
        if (
            p.get("integrity") is not True
            or p.get("http_status") != 200
            or p.get("wire_attempted") is not True
            or packet.blob is None
            or len(packet.blob) > config.settings().max_body_bytes
            or len(packet.blob) != p.get("body_bytes")
            or hashlib.sha256(packet.blob).hexdigest() != p.get("body_sha256")
            or not claim.confirmed_at
            <= requested
            <= received
            <= packet.at
            <= packet.confirmed_at
            <= now
        ):
            raise ValueError("packet_integrity_unverified")
        facts = extract(
            ticker, endpoint, _decode(packet.blob), available_at=packet.confirmed_at, asof=now
        )
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return Observation(endpoint, "unknown", reason="packet_integrity_unverified")
    return Observation(
        endpoint, "recorded", packet.confirmed_at, "", packet.key, str(p["body_sha256"]), facts
    )


def build(engine: Engine, now: datetime, day: date | None = None) -> Report:
    at = aware(now)
    current = at.astimezone(ZoneInfo("America/New_York")).date()
    selected = day or current
    initial = Report(
        at,
        selected,
        "STORAGE_UNAVAILABLE",
        "Temel veri kayıt durumu okunamıyor.",
        configured_enabled=config.enabled(),
    )
    if not storage.available(engine):
        return initial
    activation = storage.active(engine, at)
    if activation is None or activation.confirmed_at is None:
        return replace(
            initial, status="NOT_ACTIVATED", reason="Temel veri hattı henüz etkinleşmedi."
        )
    initial = replace(
        initial,
        activation_at=activation.confirmed_at,
        profile_hash=config.profile_hash(),
        implementation_hash=config.implementation_hash(),
    )
    if selected < activation.confirmed_at.astimezone(ZoneInfo("America/New_York")).date():
        return replace(
            initial,
            status="BEFORE_ACTIVATION",
            reason="Bu gün temel veri hattı henüz etkin değildi.",
        )
    if selected > current:
        return replace(initial, status="FUTURE", reason="Seçilen gün henüz gelmedi.")
    claims = storage.list_events(engine, "claim", at, day=selected, require_receipt=False)
    reserved = storage.count(engine, "claim", at, day=selected, include_future=True)
    grouped: dict[tuple[str, str], list[Observation]] = {}
    invalid = max(0, reserved - len(claims))
    for claim in claims:
        try:
            ticker, candidate, endpoint = _claim(engine, claim, activation, at)
        except (ValueError, TypeError):
            invalid += 1
            continue
        grouped.setdefault((ticker, candidate), []).append(
            _observation(engine, claim, endpoint, ticker, at)
        )
    rows = []
    for (ticker, candidate), items in sorted(grouped.items()):
        for endpoint in cast(
            tuple[Endpoint, ...], (config.settings().statement_endpoint, "earnings")
        ):
            if not any(i.endpoint == endpoint for i in items):
                items.append(Observation(endpoint, "unknown", reason="endpoint_not_recorded"))
        facts = tuple(i.facts for i in items if i.facts is not None)
        missing = tuple(
            sorted({m for f in facts for m in f.missing} | {i.reason for i in items if i.reason})
        )
        rows.append(
            Row(
                ticker,
                candidate,
                "RECORDED" if all(i.status == "recorded" for i in items) else "PARTIAL",
                tuple(items),
                facts,
                missing,
            )
        )
    halts = storage.list_events(engine, "halt", at, day=selected, require_receipt=False)
    halt = str(halts[0].payload.get("reason", "unknown")) if halts else None
    status = (
        "HALTED"
        if halt
        else "PARTIAL"
        if invalid or any(r.status != "RECORDED" for r in rows)
        else "RECORDED"
        if rows
        else "AWAITING_SOURCE"
    )
    reason = (
        "Temel veriler alındıkları andan itibaren bağlamdır; geçmişe taşınmaz."
        if rows
        else "Yeni ve zamanında gözlenen aday bekleniyor."
    )
    return replace(
        initial,
        status=status,
        reason=reason,
        reserved_requests=reserved,
        candidate_count=len(rows),
        invalid_records=invalid,
        limited=reserved > config.settings().max_requests_per_day,
        halt_reason=halt,
        rows=tuple(rows),
    )
