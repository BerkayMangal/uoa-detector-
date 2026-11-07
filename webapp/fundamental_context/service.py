"""Owned, quota-bound prospective financial capture; GET never invokes this worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board import quota_ledger
from webapp.board.db import normalize_database_url
from webapp.board.refresher import _LIVE_CALIBRATION_PROFILE
from webapp.board.settings import load_board_settings
from webapp.fundamental_context import settings as config
from webapp.fundamental_context import storage
from webapp.fundamental_context.model import Endpoint, Record, aware, digest
from webapp.fundamental_context.transport import FundamentalTransport, Packet
from webapp.shadow_capture import bindings
from webapp.shadow_capture import storage as shadow_store
from webapp.shadow_capture.model import Record as Candidate

_ET = ZoneInfo("America/New_York")
STATE: dict[str, object] = {}
_OWN: WeakKeyDictionary[Engine, set[str]] = WeakKeyDictionary()
_HALTS: WeakKeyDictionary[Engine, dict[date, str]] = WeakKeyDictionary()


class Client(Protocol):
    async def fetch(
        self,
        ticker: str,
        endpoint: Endpoint,
        *,
        before_request: Callable[[], Awaitable[None]] | None = None,
        deadline: datetime | None = None,
    ) -> Packet: ...
    async def aclose(self) -> None: ...


class _RefusedError(Exception):
    pass


def enabled() -> bool:
    return config.enabled()


def safe_status() -> dict[str, object]:
    names = {
        "status",
        "at",
        "activation_at",
        "candidates",
        "reserved_requests",
        "packets",
        "halt_reason",
    }
    return {k: v for k, v in STATE.items() if k in names and isinstance(v, str | int | bool)}


def configured_engine(database_url: str) -> Engine:
    url = normalize_database_url(database_url)
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return create_engine(url, future=True, pool_pre_ping=True)
    original = parsed.query.get("options", os.getenv("PGOPTIONS", ""))
    if not isinstance(original, str):
        raise ValueError("ambiguous_database_options")
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        connect_args={
            "options": original
            + " -c statement_timeout=2000 -c lock_timeout=1000 -c max_parallel_workers_per_gather=0"
        },
    )


def _client(clock: Callable[[], datetime]) -> Client:
    cfg = config.settings()
    return FundamentalTransport(
        api_key=Credentials().require_unusual_whales_api_key(),
        settings=load_profile(_LIVE_CALIBRATION_PROFILE).data_sources.unusual_whales,
        timeout_seconds=cfg.request_timeout_seconds,
        max_response_bytes=cfg.max_body_bytes,
        clock=clock,
    )


def _physical(clock: Callable[[], datetime], previous: datetime) -> datetime:
    at = aware(clock())
    if at < previous:
        raise _RefusedError("clock_rollback")
    return at


def _key(kind: str, *parts: object) -> str:
    return digest((config.settings().version, kind, *parts))


def packet_key(claim: Record) -> str:
    return _key("packet", claim.key)


def _halt(
    engine: Engine, day: date, reason: str, at: datetime, clock: Callable[[], datetime]
) -> None:
    _HALTS.setdefault(engine, {})[day] = reason
    storage.put(
        engine,
        key=_key("halt", day, reason),
        kind="halt",
        at=at,
        day=day,
        payload={"identity": config.identity(), "reason": reason},
        clock=clock,
    )


def halted(engine: Engine, day: date, now: datetime) -> str | None:
    if day in _HALTS.get(engine, {}):
        return _HALTS[engine][day]
    rows = storage.list_events(engine, "halt", now, day=day, require_receipt=False)
    return str(rows[0].payload.get("reason", "unknown")) if rows else None


def _bootstrap(engine: Engine, day: date, at: datetime, clock: Callable[[], datetime]) -> bool:
    claims = storage.list_events(engine, "claim", at, day=day, require_receipt=False)
    if storage.count(engine, "claim", at, day=day, include_future=True) != len(claims):
        _halt(engine, day, "ownership_unverified", at, clock)
        return False
    if len(claims) > config.settings().max_requests_per_day:
        _halt(engine, day, "module_quota_limit", at, clock)
        return False
    for claim in claims:
        if claim.key in _OWN.get(engine, set()):
            continue
        result = storage.get(engine, packet_key(claim), at)
        if (
            result is not None
            and result.kind == "packet"
            and result.parent_key == claim.key
            and result.payload.get("identity") == claim.payload.get("identity")
            and claim.confirmed_at is not None
            and result.at >= claim.confirmed_at
        ):
            continue
        if (at - claim.at).total_seconds() >= config.settings().claim_pending_seconds:
            _halt(engine, day, "unknown_provider_outcome", at, clock)
        return False
    return True


def _eligible(
    engine: Engine,
    candidate: Candidate,
    activation: Record,
    at: datetime,
    *,
    verify_source: bool = True,
) -> bool:
    original = shadow_store.active(engine, at)
    day = at.astimezone(_ET).date()
    return not (
        candidate.day != day
        or candidate.confirmed_at is None
        or activation.confirmed_at is None
        or not activation.confirmed_at <= candidate.confirmed_at <= at
        or (at - candidate.confirmed_at).total_seconds()
        > config.settings().candidate_max_age_seconds
        or classify(at, load_live_alpha_settings().calendar).mode is not MarketMode.LIVE
        or original is None
        or candidate.parent_key != original.key
        or shadow_store.get(engine, candidate.key, at) != candidate
        or (verify_source and not bindings.valid_candidate(engine, candidate, original, at))
    )


def _reserve(
    engine: Engine,
    claim: Record,
    candidate: Candidate,
    activation: Record,
    client: Client,
    at: datetime,
    clock: Callable[[], datetime],
) -> None:
    assert claim.day is not None
    if (
        halted(engine, claim.day, at)
        or not _bootstrap(engine, claim.day, at, clock)
        or not _eligible(engine, candidate, activation, at, verify_source=False)
    ):
        raise _RefusedError("source_or_day_unavailable")
    reservation = quota_ledger.reserve(
        engine,
        day=claim.day,
        n=1,
        cap=load_board_settings().refresh.daily_request_soft_cap,
        observed=quota_ledger.observed_for_day(client, claim.day, now=at),
        now=at,
    )
    if not reservation.granted:
        _halt(engine, claim.day, "shared_quota_limit", at, clock)
        raise _RefusedError("shared_quota_limit")


async def tick(
    engine: Engine,
    now: datetime,
    *,
    client_factory: Callable[[], Client],
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    at = aware(now)
    day = at.astimezone(_ET).date()
    activation = storage.active(engine, at)
    if activation is None:
        return {"status": "activation_unavailable"}
    if halted(engine, day, at):
        return {"status": "halted", "halt_reason": halted(engine, day, at)}
    if not _bootstrap(engine, day, at, clock):
        return {"status": "halted" if halted(engine, day, at) else "ownership_pending"}
    if classify(at, load_live_alpha_settings().calendar).mode is not MarketMode.LIVE:
        return {"status": "closed", "candidates": 0}
    candidates = (
        shadow_store.list_events(engine, "candidate", at, day=day, limit=5)
        if shadow_store.available(engine)
        else ()
    )
    if len(candidates) > config.settings().max_tickers_per_day:
        _halt(engine, day, "candidate_limit_invalid", at, clock)
        return {"status": "halted"}
    candidates = tuple(c for c in candidates if _eligible(engine, c, activation, at))
    lock, semaphore = asyncio.Lock(), asyncio.Semaphore(config.settings().max_tickers_per_day)
    last_wire: datetime | None = None
    endpoints: tuple[Endpoint, ...] = (config.settings().statement_endpoint, "earnings")

    async def capture(candidate: Candidate) -> None:
        nonlocal last_wire
        async with semaphore:
            client: Client | None = None
            try:
                slot = candidate.payload.get("slot")
                ticker = candidate.payload.get("ticker")
                if (
                    type(slot) is not int
                    or not 1 <= slot <= config.settings().max_tickers_per_day
                    or not isinstance(ticker, str)
                ):
                    return
                for index, endpoint in enumerate(endpoints):
                    moment = _physical(clock, at)
                    if (
                        halted(engine, day, moment)
                        or not _bootstrap(engine, day, moment, clock)
                        or not _eligible(engine, candidate, activation, moment, verify_source=False)
                    ):
                        return
                    ordinal = (slot - 1) * len(endpoints) + index + 1
                    if ordinal > config.settings().max_requests_per_day:
                        _halt(engine, day, "request_limit_invalid", moment, clock)
                        return
                    key = _key("claim", day, ordinal)
                    existing_claim = storage.get(engine, key, moment, require_receipt=False)
                    if existing_claim is not None:
                        if storage.get(engine, packet_key(existing_claim), moment) is None:
                            return
                        continue
                    claim = storage.put(
                        engine,
                        key=key,
                        kind="claim",
                        at=moment,
                        day=day,
                        parent_key=candidate.key,
                        unique_owner=_key("request_owner", day, ticker, endpoint),
                        payload={
                            "identity": config.identity(),
                            "activation_key": activation.key,
                            "candidate_key": candidate.key,
                            "candidate_checksum": candidate.checksum,
                            "ticker": ticker,
                            "endpoint": endpoint,
                            "slot": ordinal,
                        },
                        clock=clock,
                    )
                    if claim is None or claim.confirmed_at is None:
                        return
                    _OWN.setdefault(engine, set()).add(claim.key)
                    try:
                        if client is None:
                            client = client_factory()
                        active_client = client
                        owned_claim: Record = claim
                        deadline = (
                            candidate.confirmed_at
                            + timedelta(seconds=config.settings().candidate_max_age_seconds)
                            if candidate.confirmed_at
                            else moment
                        )

                        async def before_request(
                            claim: Record = owned_claim,
                            deadline: datetime = deadline,
                            active_client: Client = active_client,
                        ) -> None:
                            nonlocal moment, last_wire
                            async with lock:
                                moment = _physical(clock, moment)
                                if last_wire is not None:
                                    delay = (
                                        config.settings().wire_min_spacing_seconds
                                        - (moment - last_wire).total_seconds()
                                    )
                                    if delay > 0:
                                        await sleep(delay)
                                        moment = _physical(clock, moment)
                                if moment >= deadline:
                                    raise _RefusedError("source_expired")
                                _reserve(
                                    engine,
                                    claim,
                                    candidate,
                                    activation,
                                    active_client,
                                    moment,
                                    clock,
                                )
                                moment = _physical(clock, moment)
                                if moment >= deadline:
                                    raise _RefusedError("source_expired")
                                last_wire = moment

                        refusal = ""
                        try:
                            packet = await client.fetch(
                                ticker, endpoint, before_request=before_request, deadline=deadline
                            )
                        except _RefusedError as exc:
                            refusal = str(exc)
                            packet = Packet(
                                ticker,
                                "late",
                                None,
                                None,
                                None,
                                None,
                                None,
                                0,
                                False,
                                None,
                                None,
                                endpoint=endpoint,
                            )
                        except Exception:
                            _halt(engine, day, "unknown_provider_outcome", aware(clock()), clock)
                            return
                        moment = _physical(clock, max(moment, packet.received_at or moment))
                        integrity = (
                            packet.ticker == ticker
                            and packet.endpoint == endpoint
                            and packet.raw_body is not None
                            and len(packet.raw_body) <= config.settings().max_body_bytes
                            and len(packet.raw_body) == packet.body_bytes
                            and hashlib.sha256(packet.raw_body).hexdigest() == packet.body_sha256
                            and packet.requested_at is not None
                            and packet.received_at is not None
                            and claim.confirmed_at
                            <= packet.requested_at
                            <= packet.received_at
                            <= moment
                        )
                        reason = refusal or packet.status
                        if packet.status in {"auth", "daily_quota", "quota_unknown"}:
                            _halt(engine, day, packet.status, moment, clock)
                        elif packet.wire_attempted and (
                            packet.raw_body is None or packet.http_status is None
                        ):
                            _halt(engine, day, "unknown_provider_outcome", moment, clock)
                        elif packet.status == "rate_limited":
                            _halt(engine, day, "rate_limited", moment, clock)
                        if packet.status == "ok" and not integrity:
                            _halt(engine, day, "packet_integrity_unverified", moment, clock)
                        saved = storage.put(
                            engine,
                            key=packet_key(claim),
                            kind="packet",
                            at=moment,
                            day=day,
                            parent_key=claim.key,
                            blob=packet.raw_body
                            if packet.raw_body is not None
                            and len(packet.raw_body) <= config.settings().max_body_bytes
                            else None,
                            payload={
                                "identity": config.identity(),
                                "activation_key": activation.key,
                                "candidate_key": candidate.key,
                                "ticker": ticker,
                                "endpoint": endpoint,
                                "status": packet.status,
                                "reason": reason,
                                "integrity": integrity,
                                "http_status": packet.http_status,
                                "wire_attempted": packet.wire_attempted,
                                "requested_at": packet.requested_at.isoformat()
                                if packet.requested_at
                                else None,
                                "received_at": packet.received_at.isoformat()
                                if packet.received_at
                                else None,
                                "body_sha256": packet.body_sha256,
                                "body_bytes": packet.body_bytes,
                            },
                            clock=clock,
                        )
                        if saved is None or saved.confirmed_at is None:
                            _halt(engine, day, "packet_commit_unverified", aware(clock()), clock)
                            return
                    finally:
                        _OWN.get(engine, set()).discard(claim.key)
            except Exception:
                _HALTS.setdefault(engine, {})[day] = "storage_or_capture_failed"
                raise
            finally:
                if client is not None:
                    await client.aclose()

    results = await asyncio.gather(*(capture(c) for c in candidates), return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        raise RuntimeError("fundamental_capture_failed")
    moment = _physical(clock, at)
    return {
        "status": "halted"
        if halted(engine, day, moment)
        else "active"
        if candidates
        else "waiting",
        "candidates": len(candidates),
        "reserved_requests": storage.count(engine, "claim", moment, day=day, include_future=True),
        "packets": storage.count(engine, "packet", moment, day=day),
        "halt_reason": halted(engine, day, moment) or "",
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
    STATE.clear()
    STATE.update(status="starting")
    try:
        storage.ensure(engine)
        quota_ledger.ensure_quota_tables(engine)
        activation = storage.activate(engine, now)
        if activation.confirmed_at is None:
            STATE.update(status="activation_unverified")
            return
        STATE.update(activation_at=activation.confirmed_at.isoformat())
        cycles = 0
        while True:
            try:
                result = await tick(
                    engine,
                    aware(now()),
                    client_factory=client_factory or (lambda: _client(now)),
                    clock=now,
                    sleep=pause,
                )
                STATE.update(result)
                STATE.update(at=aware(now()).isoformat())
            except Exception:
                STATE.update(status="storage_or_capture_failed", at=aware(now()).isoformat())
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
        engine.dispose()
