"""Independent bounded semantic observer; claims commit before parallel inference."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.semantic_news import config, sources, storage
from webapp.semantic_news.model import (
    EmptySource,
    Envelope,
    Metadata,
    Record,
    Request,
    Source,
    aware,
    digest,
)
from webapp.semantic_news.transport import Client, Transport
from webapp.semantic_news.validation import decode, validate
from webapp.shadow_capture.service import configured_engine

STATE: dict[str, object] = {}
_HALTS: WeakKeyDictionary[Engine, dict[date, str]] = WeakKeyDictionary()
_INFLIGHT: WeakKeyDictionary[Engine, set[str]] = WeakKeyDictionary()
_ET = ZoneInfo("America/New_York")


def enabled() -> bool:
    return config.enabled()


def safe_status() -> dict[str, object]:
    allowed = {
        "status",
        "at",
        "claimed",
        "completed",
        "unknown",
        "requests",
        "candidates",
        "binding",
    }
    return {k: v for k, v in STATE.items() if k in allowed and isinstance(v, str | int | bool)}


def binding_key() -> str:
    return digest(("semantic_binding", config.identity()))


def _halt(
    engine: Engine, day: date, reason: str, at: datetime, clock: Callable[[], datetime]
) -> None:
    _HALTS.setdefault(engine, {})[day] = reason
    storage.put(
        engine,
        key=digest(("semantic_halt", day.isoformat(), reason)),
        kind="halt",
        at=at,
        day=day,
        payload={"identity": config.identity(), "reason": reason},
        clock=clock,
    )


def halted(engine: Engine, day: date, now: datetime) -> bool:
    return day in _HALTS.get(engine, {}) or bool(
        storage.list_events(engine, "halt", now, day=day, require_receipt=False)
    )


def bind(
    engine: Engine, metadata: Metadata, now: datetime, clock: Callable[[], datetime]
) -> Record | None:
    if (
        not metadata.configured
        or metadata.prompt_hash != config.settings().expected_prompt_hash
        or metadata.schema_hash != config.settings().expected_schema_hash
    ):
        return None
    payload = {
        "identity": config.identity(),
        "metadata": metadata.model_dump(),
        "binding": metadata.binding(),
    }
    old = storage.get(engine, binding_key(), now, require_receipt=False)
    if old is not None:
        if old.payload != payload or old.confirmed_at is None:
            _halt(engine, now.astimezone(_ET).date(), "binding_changed_or_unconfirmed", now, clock)
            return None
        return old
    result = storage.put(
        engine, key=binding_key(), kind="binding", at=now, payload=payload, clock=clock
    )
    return result or storage.get(engine, binding_key(), aware(clock()))


def _bootstrap(engine: Engine, day: date, now: datetime, clock: Callable[[], datetime]) -> bool:
    claims = storage.list_events(engine, "claim", now, day=day, limit=5, require_receipt=False)
    if storage.count(engine, "claim", now, day=day, include_future=True) > len(claims):
        _halt(engine, day, "ownership_unverified", now, clock)
        return False
    pending = False
    for claim in claims:
        if claim.key in _INFLIGHT.get(engine, set()):
            continue
        response = storage.get(engine, digest(("semantic_response", claim.key)), now)
        if (
            response is not None
            and response.kind == "response"
            and response.parent_key == claim.key
            and response.day == day
            and response.payload.get("status") != "pending"
        ):
            continue
        try:
            deadline = aware(datetime.fromisoformat(str(claim.payload["deadline"])))
        except (ValueError, KeyError):
            _halt(engine, day, "ownership_unverified", now, clock)
            return False
        if now >= deadline:
            _halt(engine, day, "unknown_provider_outcome", now, clock)
            return False
        pending = True
    return not pending


def _claimed(engine: Engine, candidate_key: str, now: datetime) -> bool:
    day = now.astimezone(_ET).date()
    owners = storage.list_events(engine, "claim", now, day=day, require_receipt=False, limit=5)
    return any(r.unique_owner == candidate_key for r in owners)


def _claim(
    engine: Engine, source: Source, binding: Record, now: datetime, clock: Callable[[], datetime]
) -> Record | None:
    cfg = config.settings()
    deadline = source.entry_target - timedelta(seconds=cfg.min_completion_lead_seconds)
    if (
        now >= deadline
        or halted(engine, source.day, now)
        or storage.count(engine, "claim", now, day=source.day, include_future=True)
        >= cfg.daily_analyses
        or _claimed(engine, source.candidate_key, now)
    ):
        return None
    for slot in range(1, cfg.daily_analyses + 1):
        result = storage.put(
            engine,
            key=digest(("semantic_claim", source.day.isoformat(), slot)),
            kind="claim",
            at=now,
            day=source.day,
            parent_key=binding.key,
            unique_owner=source.candidate_key,
            payload={
                "identity": config.identity(),
                "binding": binding.payload["binding"],
                "candidate_key": source.candidate_key,
                "candidate_checksum": source.candidate_checksum,
                "candidate_at": source.candidate_at.isoformat(),
                "entry_target": source.entry_target.isoformat(),
                "deadline": deadline.isoformat(),
                "ticker": source.ticker,
                "slot": slot,
                "source_packet_key": source.source_packet_key,
                "source_payload_hash": source.source_payload_hash,
                "source_available_at": source.source_available_at.isoformat(),
                "request": source.request.model_dump(mode="json"),
                "input_count": len(source.request.items),
                "source_headline_count": source.source_headline_count,
                "input_limited": source.source_headline_count > len(source.request.items),
            },
            clock=clock,
        )
        if result is not None:
            return result
        if _claimed(engine, source.candidate_key, aware(clock())):
            return None
    return None


def _skip(
    engine: Engine,
    candidate_key: str,
    day: date,
    ticker: str,
    reason: str,
    now: datetime,
    clock: Callable[[], datetime],
    empty: EmptySource | None = None,
) -> None:
    storage.put(
        engine,
        key=digest(("semantic_skip", config.identity(), candidate_key)),
        kind="skip",
        at=now,
        day=day,
        unique_owner="skip:" + candidate_key,
        payload={
            "identity": config.identity(),
            "candidate_key": candidate_key,
            "ticker": ticker,
            "reason": reason,
            "checked_empty": empty is not None,
            **(
                {
                    "candidate_checksum": empty.candidate_checksum,
                    "entry_target": empty.entry_target.isoformat(),
                    "source_packet_key": empty.source_packet_key,
                    "source_payload_hash": empty.source_payload_hash,
                    "source_available_at": empty.source_available_at.isoformat(),
                }
                if empty is not None
                else {}
            ),
        },
        clock=clock,
    )


async def _analyze(
    engine: Engine, client: Client, metadata: Metadata, claim: Record, clock: Callable[[], datetime]
) -> str:
    day = claim.day
    confirmed = claim.confirmed_at
    assert day is not None and confirmed is not None
    request = Request.model_validate(claim.payload["request"])
    deadline = aware(datetime.fromisoformat(str(claim.payload["deadline"])))

    async def before_request() -> None:
        at = aware(clock())
        if at < confirmed or at >= deadline or halted(engine, day, at):
            raise ValueError("request_claim_late_or_halted")
        saved = storage.get(engine, claim.key, at)
        if saved != claim:
            raise ValueError("claim_unverified_before_wire")
        from webapp.shadow_capture import storage as shadow

        candidate = shadow.get(engine, claim.unique_owner or "", at)
        if candidate is None or candidate.checksum != claim.payload.get("candidate_checksum"):
            raise ValueError("source_changed_before_wire")
        if sources.source(engine, candidate, claim.at).request != request:
            raise ValueError("source_changed_before_wire")
        after = aware(clock())
        if after < at or after >= deadline:
            raise ValueError("late_before_wire")

    try:
        packet = await client.analyze(request, deadline=deadline, before_request=before_request)
    except Exception:
        # A client may have written bytes before raising. Do not replay its claim.
        now = aware(clock())
        _halt(engine, day, "unknown_provider_outcome", now, clock)
        return "unknown"
    now = aware(clock())
    status, reason, direction = "unknown", packet.status, "unknown"
    supports: bool | None = None
    response: Envelope | None = None
    if packet.status == "ok" and packet.raw_body is not None:
        try:
            if not confirmed <= packet.requested_at <= packet.received_at <= now:
                raise ValueError("wire_chronology_invalid")
            response = Envelope.model_validate(decode(packet.raw_body))
            result = validate(request, response, metadata, packet.received_at)
            status = response.status
            direction, supports, reason = result.direction, result.supports, result.reason
        except (ValueError, TypeError):
            reason = "response_validation_failed"
            _halt(engine, day, reason, now, clock)
    elif packet.wire_attempted:
        _halt(engine, day, packet.status, now, clock)
    if response is not None and response.status in {"unknown", "rate_limited", "disabled"}:
        _halt(engine, day, "producer_" + response.status, now, clock)
    # Raw input was bound in the immutable claim; raw output remains private.
    payload: dict[str, Any] = {
        "identity": config.identity(),
        "request_id": request.request_id,
        "candidate_key": claim.unique_owner,
        "ticker": request.ticker,
        "status": status,
        "reason": reason,
        "direction": direction,
        "supports": supports,
        "binding": metadata.binding(),
        "requested_at": packet.requested_at.isoformat(),
        "received_at": packet.received_at.isoformat(),
        "http_status": packet.http_status,
        "wire_attempted": packet.wire_attempted,
        "envelope": response.model_dump(mode="json") if response is not None else None,
    }
    try:
        record = storage.put(
            engine,
            key=digest(("semantic_response", claim.key)),
            kind="response",
            at=now,
            day=day,
            parent_key=claim.key,
            payload=payload,
            blob=packet.raw_body,
            clock=clock,
        )
        if record is None or record.confirmed_at is None:
            _halt(engine, day, "response_commit_unverified", aware(clock()), clock)
            return "unknown"
        return status if record.confirmed_at <= deadline else "late"
    except Exception:
        _HALTS.setdefault(engine, {})[day] = "storage_unavailable"
        raise


async def tick(
    engine: Engine,
    client: Client,
    metadata: Metadata,
    now: datetime,
    *,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    at = aware(now)
    day = at.astimezone(_ET).date()
    binding = bind(engine, metadata, at, clock)
    if binding is None:
        return {
            "status": "binding_unavailable",
            "claimed": 0,
            "completed": 0,
            "unknown": 0,
            "requests": 0,
        }
    if classify(at, load_live_alpha_settings().calendar).mode is not MarketMode.LIVE:
        return {
            "status": "waiting_session",
            "claimed": 0,
            "completed": 0,
            "unknown": 0,
            "requests": 0,
        }
    if halted(engine, day, at):
        return {"status": "halted", "claimed": 0, "completed": 0, "unknown": 0, "requests": 0}
    if not _bootstrap(engine, day, at, clock):
        return {
            "status": "halted" if halted(engine, day, at) else "bootstrap_hold",
            "claimed": 0,
            "completed": 0,
            "unknown": 0,
            "requests": 0,
        }
    parents = sources.candidates(engine, at)
    claims = []
    for candidate in parents:
        if _claimed(engine, candidate.key, aware(clock())):
            continue
        if (
            storage.get(
                engine,
                digest(("semantic_skip", config.identity(), candidate.key)),
                aware(clock()),
                require_receipt=False,
            )
            is not None
        ):
            continue
        try:
            empty = sources.empty_source(engine, candidate, aware(clock()))
            if empty is not None:
                _skip(
                    engine,
                    candidate.key,
                    empty.day,
                    empty.ticker,
                    empty.reason,
                    aware(clock()),
                    clock,
                    empty,
                )
                continue
            source = sources.source(engine, candidate, aware(clock()))
        except ValueError as exc:
            # Only fixed internal reason strings enter the record, not exceptions
            # from SQL or transport. Unknown source evidence never becomes neutral.
            if candidate.day is not None:
                _skip(
                    engine,
                    candidate.key,
                    candidate.day,
                    str(candidate.payload.get("ticker", "")),
                    (
                        str(exc)
                        if str(exc)
                        in {
                            "candidate_unverified",
                            "entry_cutoff_passed",
                            "source_headlines_unknown_or_empty",
                            "physical_news_storage_unavailable",
                            "source_packet_unverified",
                            "physical_news_unverified",
                            "source_hash_invalid",
                            "source_shape_invalid",
                            "source_item_invalid",
                            "no_bounded_source_headlines",
                            "request_unverified_or_oversize",
                        }
                        else "source_unverified"
                    ),
                    aware(clock()),
                    clock,
                )
            continue
        claim = _claim(engine, source, binding, aware(clock()), clock)
        if claim is not None and claim.confirmed_at is not None:
            claims.append(claim)
    # All claim transactions and confirmations precede every billable await.
    inflight = _INFLIGHT.setdefault(engine, set())
    inflight.update(c.key for c in claims)
    try:
        results = await asyncio.gather(
            *(_analyze(engine, client, metadata, c, clock) for c in claims), return_exceptions=True
        )
        outcomes = [v if isinstance(v, str) else "unknown" for v in results]
        if any(not isinstance(v, str) for v in results):
            _HALTS.setdefault(engine, {})[day] = "storage_unavailable"
    finally:
        inflight.difference_update(c.key for c in claims)
    return {
        "status": "halted"
        if halted(engine, day, aware(clock()))
        else "active"
        if parents
        else "waiting",
        "claimed": len(claims),
        "completed": outcomes.count("completed"),
        "unknown": sum(v != "completed" for v in outcomes),
        "requests": len(claims),
        "candidates": len(parents),
        "binding": digest(metadata.binding()),
    }


async def loop(
    database_url: str,
    *,
    client_factory: Callable[[], Client] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> None:
    if not enabled() or (max_cycles is not None and max_cycles < 1):
        return
    now = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    engine = configured_engine(database_url)
    client: Client | None = None
    STATE.clear()
    STATE.update(status="starting")
    try:
        storage.ensure(engine)
        client = (
            client_factory()
            if client_factory
            else Transport(os.getenv(config.settings().token_env, ""), clock=now)
        )
        meta = await client.metadata()
        if meta.status != "configured" or meta.value is None:
            STATE.update(status=meta.status, at=aware(now()).isoformat())
            return
        cycles = 0
        while True:
            try:
                STATE.update(await tick(engine, client, meta.value, aware(now()), clock=now))
                STATE.update(at=aware(now()).isoformat())
            except Exception:
                _HALTS.setdefault(engine, {})[aware(now()).astimezone(_ET).date()] = (
                    "storage_unavailable"
                )
                STATE.update(status="storage_or_analysis_failed", at=aware(now()).isoformat())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            live = (
                classify(aware(now()), load_live_alpha_settings().calendar).mode is MarketMode.LIVE
            )
            await pause(
                config.settings().poll_seconds if live else config.settings().idle_poll_seconds
            )
    finally:
        if client is not None:
            await client.aclose()
        engine.dispose()
