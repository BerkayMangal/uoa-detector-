"""Prospective paired quote collection; never changes live advice or old marks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from webapp.board import quota_ledger
from webapp.board.settings import load_board_settings
from webapp.opening.model import clock as market_clock
from webapp.shadow_capture import config, storage
from webapp.shadow_capture.model import Record, aware, digest
from webapp.timed_measurement.collector import Quote, QuoteClient, parse_quote, schedule
from webapp.timed_measurement.transport import Packet

_HALTS: WeakKeyDictionary[Engine, dict[date, str]] = WeakKeyDictionary()
_COOLDOWNS: WeakKeyDictionary[Engine, dict[date, datetime]] = WeakKeyDictionary()
_ET = ZoneInfo("America/New_York")


class _RefusedError(Exception):
    pass


def _key(kind: str, *parts: object) -> str:
    return digest((config.settings().version, kind, *parts))


def _time(value: object) -> datetime | None:
    try:
        return aware(datetime.fromisoformat(value)) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def _physical(clock: Callable[[], datetime], previous: datetime) -> datetime:
    moment = aware(clock())
    if moment < previous:
        raise _RefusedError("clock_error")
    return moment


def _identity(record: Record, activation: Record) -> bool:
    return (
        record.payload.get("identity") == config.identity()
        and record.payload.get("activation_key") == activation.key
        and record.confirmed_at is not None
        and activation.confirmed_at is not None
        and record.confirmed_at >= activation.confirmed_at
    )


def _candidate(engine: Engine, target: Record, at: datetime) -> Record | None:
    from webapp.shadow_capture.bindings import valid_candidate

    activation = storage.active(engine, at)
    candidate = storage.get(engine, target.parent_key or "", at)
    if (
        activation is None
        or candidate is None
        or not _identity(target, activation)
        or not _identity(candidate, activation)
        or not valid_candidate(engine, candidate, activation, at)
        or target.day != candidate.day
        or target.payload.get("candidate_key") != candidate.key
        or target.payload.get("ticker") != candidate.payload.get("ticker")
    ):
        return None
    return candidate


def _target_valid(engine: Engine, target: Record, at: datetime) -> bool:
    candidate = _candidate(engine, target, at)
    if candidate is None or candidate.confirmed_at is None or target.confirmed_at is None:
        return False
    phase = target.payload.get("phase")
    if (
        phase not in {"entry", "m15", "m60"}
        or target.key != _key("target", candidate.key, phase)
        or target.at < candidate.confirmed_at
    ):
        return False
    if phase == "entry":
        expected = candidate.confirmed_at + timedelta(seconds=config.settings().entry_delay_seconds)
    else:
        entry_target = storage.get(engine, _key("target", candidate.key, "entry"), at)
        entry = verified_outcome(engine, entry_target, at)
        if entry is None or entry.payload.get("status") != "observed" or entry.confirmed_at is None:
            return False
        if target.at < entry.confirmed_at:
            return False
        expected = entry.confirmed_at + timedelta(minutes=15 if phase == "m15" else 60)
    deadline, eligible, reason = _window(candidate, expected)
    return (
        _time(target.payload.get("target_at")) == expected
        and _time(target.payload.get("deadline")) == deadline
        and target.payload.get("eligible") is eligible
        and target.payload.get("reason") == reason
    )


def _halt(
    engine: Engine, day: date, reason: str, at: datetime, clock: Callable[[], datetime]
) -> None:
    # A durable write failure must not clear a known native refusal in memory.
    _HALTS.setdefault(engine, {})[day] = reason
    storage.put(
        engine,
        key=_key("halt", day, reason),
        kind="halt",
        at=at,
        day=day,
        payload={"identity": config.identity(), "reason": reason, "until": None},
        clock=clock,
    )


def _halted(engine: Engine, day: date, at: datetime) -> bool:
    if day in _HALTS.get(engine, {}):
        return True
    rows = storage.list_events(engine, "halt", at, day=day, require_receipt=False)
    return any(r.payload.get("until") is None for r in rows)


def _cooling(engine: Engine, day: date, at: datetime) -> bool:
    local = _COOLDOWNS.get(engine, {}).get(day)
    if local is not None and at < local:
        return True
    return any(
        (until := _time(r.payload.get("until"))) is not None and at < until
        for r in storage.list_events(engine, "halt", at, day=day, require_receipt=False)
    )


def _cooldown(engine: Engine, day: date, at: datetime, clock: Callable[[], datetime]) -> None:
    until = at + timedelta(seconds=config.settings().rate_limit_cooldown_seconds)
    _COOLDOWNS.setdefault(engine, {})[day] = until
    storage.put(
        engine,
        key=_key("cooldown", day, until.isoformat()),
        kind="halt",
        at=at,
        day=day,
        payload={
            "identity": config.identity(),
            "reason": "rate_limited",
            "until": until.isoformat(),
        },
        clock=clock,
    )


def _window(candidate: Record, target_at: datetime) -> tuple[datetime, bool, str]:
    assert candidate.confirmed_at is not None
    original, target = market_clock(candidate.confirmed_at), market_clock(target_at)
    end = target_at + timedelta(seconds=config.settings().window_seconds)
    if original.closing is None or original.opening is None:
        return end, False, "unknown_calendar"
    end = min(end, original.closing)
    if (
        original.day != target.day
        or not original.opening <= candidate.confirmed_at < original.closing
        or not original.opening < target_at < original.closing
    ):
        return end, False, "outside_regular_session"
    return end, True, ""


def _save_target(
    engine: Engine,
    candidate: Record,
    phase: str,
    target_at: datetime,
    at: datetime,
    clock: Callable[[], datetime],
) -> None:
    deadline, eligible, reason = _window(candidate, target_at)
    storage.put(
        engine,
        key=_key("target", candidate.key, phase),
        kind="target",
        at=at,
        day=candidate.day,
        parent_key=candidate.key,
        payload={
            "identity": config.identity(),
            "activation_key": candidate.payload["activation_key"],
            "candidate_key": candidate.key,
            "ticker": candidate.payload["ticker"],
            "phase": phase,
            "target_at": target_at.isoformat(),
            "deadline": deadline.isoformat(),
            "eligible": eligible,
            "reason": reason,
        },
        clock=clock,
    )


def ensure_targets(
    engine: Engine, activation: Record, now: datetime, *, clock: Callable[[], datetime]
) -> int:
    """Idempotent scheduling only; a verified observed entry owns its exit clocks."""
    at = aware(now)
    day = at.astimezone(_ET).date()
    candidates = storage.list_events(engine, "candidate", at, day=day, limit=128)
    from webapp.shadow_capture.bindings import valid_candidate

    admitted = [r for r in candidates if _identity(r, activation) and r.payload.get("base") is True]
    if len(admitted) > config.settings().candidate_slots:
        _halt(engine, day, "candidate_limit_invalid", at, clock)
        return 0
    for candidate in admitted:
        assert candidate.confirmed_at is not None
        target = storage.get(engine, _key("target", candidate.key, "entry"), at)
        if target is None:
            if not valid_candidate(engine, candidate, activation, at):
                continue
            _save_target(
                engine,
                candidate,
                "entry",
                candidate.confirmed_at + timedelta(seconds=config.settings().entry_delay_seconds),
                at,
                clock,
            )
            target = storage.get(engine, _key("target", candidate.key, "entry"), at)
        if all(
            storage.get(engine, _key("target", candidate.key, f"m{minutes}"), at) is not None
            for minutes in config.settings().horizons_minutes
        ):
            continue
        # Quiet pending targets must not repeatedly parse source snapshots/news.
        if target is None:
            continue
        saved_entry = storage.get(engine, _key("outcome", target.key), at)
        if saved_entry is None or saved_entry.payload.get("status") != "observed":
            continue
        entry = verified_outcome(engine, target, at)
        if entry is not None and entry.payload.get("status") == "observed":
            assert entry.confirmed_at is not None
            for minutes in config.settings().horizons_minutes:
                _save_target(
                    engine,
                    candidate,
                    f"m{minutes}",
                    entry.confirmed_at + timedelta(minutes=minutes),
                    at,
                    clock,
                )
    return len(admitted)


def _decode(raw: bytes) -> object:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise ValueError("nonfinite_json")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _quote(packet: Record | None, ticker: str, target: Record, at: datetime) -> Quote:
    if (
        packet is None
        or packet.kind != "packet"
        or packet.confirmed_at is None
        or packet.confirmed_at > at
        or packet.payload.get("status") != "ok"
        or packet.payload.get("integrity") is not True
        or packet.blob is None
        or packet.payload.get("http_status") != 200
        or packet.payload.get("wire_attempted") is not True
        or packet.payload.get("ticker") != ticker
        or packet.payload.get("body_bytes") != len(packet.blob)
        or packet.payload.get("body_sha256") != hashlib.sha256(packet.blob).hexdigest()
    ):
        return Quote("invalid_source", "packet_unverified")
    due, deadline = _time(target.payload.get("target_at")), _time(target.payload.get("deadline"))
    if due is None or deadline is None or packet.confirmed_at > deadline:
        return Quote("invalid_source", "confirmation_outside_window")
    try:
        payload = _decode(packet.blob)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return Quote("invalid_source", "raw_json_invalid")
    result = parse_quote(
        ticker,
        payload,
        _time(packet.payload.get("requested_at")),
        _time(packet.payload.get("received_at")),
        due,
        deadline,
        trade_age_seconds=config.settings().trade_max_age_seconds,
    )
    if result.trade_at is not None and result.trade_at < due:
        return Quote("invalid_source", "trade_before_target")
    return result


def verified_outcome(engine: Engine, target: Record | None, now: datetime) -> Record | None:
    """Bounded read-only chain verification for scheduling and the private view."""
    if target is None or target.kind != "target" or target.confirmed_at is None:
        return None
    at = aware(now)
    if not _target_valid(engine, target, at):
        return None
    outcome = storage.get(engine, _key("outcome", target.key), at)
    if (
        outcome is None
        or outcome.kind != "outcome"
        or outcome.day != target.day
        or outcome.parent_key != target.key
        or outcome.payload.get("identity") != config.identity()
        or outcome.payload.get("candidate_key") != target.parent_key
        or outcome.payload.get("phase") != target.payload.get("phase")
        or outcome.payload.get("activation_key") != target.payload.get("activation_key")
        or outcome.payload.get("ticker") != target.payload.get("ticker")
        or outcome.at < target.confirmed_at
    ):
        return None
    if outcome.payload.get("status") in {"missing", "not_applicable"}:
        return (
            outcome
            if (
                outcome.payload.get("own_price") is None
                and outcome.payload.get("spy_price") is None
                and (
                    outcome.payload.get("status") != "not_applicable"
                    or target.payload.get("eligible") is False
                )
            )
            else None
        )
    if outcome.payload.get("status") != "observed":
        return None
    deadline = _time(target.payload.get("deadline"))
    if outcome.confirmed_at is None or deadline is None or outcome.confirmed_at > deadline:
        return None
    attempt_key = outcome.payload.get("attempt_key")
    if not isinstance(attempt_key, str):
        return None
    attempt = storage.get(engine, attempt_key, at)
    done = storage.get(engine, _key("attempt_done", attempt_key), at)
    if (
        attempt is None
        or attempt.kind != "attempt"
        or attempt.parent_key != target.key
        or type(attempt.payload.get("ordinal")) is not int
        or attempt.payload["ordinal"] not in (1, 2, 3)
        or attempt.key != _key("attempt", target.key, attempt.payload["ordinal"])
        or attempt.payload.get("target_key") != target.key
        or attempt.payload.get("deadline") != target.payload.get("deadline")
        or done is None
        or done.kind != "attempt_done"
        or done.parent_key != attempt_key
        or done.confirmed_at is None
        or done.confirmed_at > outcome.at
        or done.payload.get("paired") is not True
        or attempt.confirmed_at is None
        or attempt.at < target.confirmed_at
        or attempt.day != target.day
        or done.day != target.day
        or attempt.payload.get("identity") != config.identity()
        or done.payload.get("identity") != config.identity()
    ):
        return None
    quotes = []
    for leg, ticker in (("own", str(target.payload.get("ticker"))), ("spy", "SPY")):
        key = outcome.payload.get(leg + "_packet_key")
        if key != _key("packet", attempt_key, leg):
            return None
        packet = storage.get(engine, key, at)
        if (
            packet is None
            or packet.parent_key != attempt_key
            or packet.payload.get("leg") != leg
            or packet.payload.get("identity") != config.identity()
            or packet.at < attempt.confirmed_at
            or packet.confirmed_at is None
            or packet.confirmed_at > done.at
        ):
            return None
        requested = _time(packet.payload.get("requested_at"))
        if requested is None or requested < attempt.confirmed_at:
            return None
        slots = storage.list_events(engine, "quota_slot", at, day=target.day, limit=129)
        own_slots = [r for r in slots if r.unique_owner == digest((attempt_key, leg))]
        if (
            len(own_slots) != 1
            or own_slots[0].payload.get("identity") != config.identity()
            or own_slots[0].payload.get("ticker") != ticker
            or own_slots[0].parent_key != attempt_key
            or own_slots[0].payload.get("attempt_key") != attempt_key
            or own_slots[0].payload.get("leg") != leg
            or type(own_slots[0].payload.get("slot")) is not int
            or not 1 <= own_slots[0].payload["slot"] <= config.settings().module_daily_cap
            or own_slots[0].key != _key("quota_slot", target.day, own_slots[0].payload["slot"])
            or own_slots[0].confirmed_at is None
            or own_slots[0].confirmed_at > requested
        ):
            return None
        quote = _quote(packet, ticker, target, outcome.confirmed_at)
        if (
            quote.status != "ok"
            or quote.price != outcome.payload.get(leg + "_price")
            or quote.trade_at is None
            or quote.trade_at.isoformat() != outcome.payload.get(leg + "_trade_at")
        ):
            return None
        quotes.append(quote)
    own, spy = quotes
    assert own.trade_at is not None and spy.trade_at is not None
    if abs((own.trade_at - spy.trade_at).total_seconds()) > config.settings().pair_skew_seconds:
        return None
    return outcome


def _outcome(
    engine: Engine,
    target: Record,
    at: datetime,
    status: str,
    reason: str,
    clock: Callable[[], datetime],
    *,
    attempt: Record | None = None,
    own: Quote | None = None,
    spy: Quote | None = None,
) -> Record | None:
    return storage.put(
        engine,
        key=_key("outcome", target.key),
        kind="outcome",
        at=at,
        day=target.day,
        parent_key=target.key,
        payload={
            "identity": config.identity(),
            "activation_key": target.payload["activation_key"],
            "candidate_key": target.parent_key,
            "ticker": target.payload["ticker"],
            "phase": target.payload["phase"],
            "status": status,
            "reason": reason,
            "attempt_key": attempt.key if attempt else None,
            "own_packet_key": _key("packet", attempt.key, "own") if attempt else None,
            "spy_packet_key": _key("packet", attempt.key, "spy") if attempt else None,
            "own_price": own.price if own else None,
            "spy_price": spy.price if spy else None,
            "own_trade_at": own.trade_at.isoformat() if own and own.trade_at else None,
            "spy_trade_at": spy.trade_at.isoformat() if spy and spy.trade_at else None,
        },
        clock=clock,
    )


def _bootstrap(
    engine: Engine, at: datetime, clock: Callable[[], datetime], owned: str | None = None
) -> bool:
    day = at.astimezone(_ET).date()
    attempts = storage.list_events(engine, "attempt", at, day=day, require_receipt=False, limit=129)
    if len(attempts) > 128 or storage.count(
        engine, "attempt", at, day=day, include_future=True
    ) > len(attempts):
        _halt(engine, day, "ownership_unverified", at, clock)
        return False
    pending = False
    for attempt in attempts:
        if attempt.key == owned:
            continue
        done = storage.get(engine, _key("attempt_done", attempt.key), at)
        if (
            done is not None
            and done.kind == "attempt_done"
            and done.parent_key == attempt.key
            and done.day == attempt.day
            and done.payload.get("identity") == attempt.payload.get("identity")
            and attempt.confirmed_at is not None
            and done.at >= attempt.confirmed_at
        ):
            continue
        deadline = _time(attempt.payload.get("deadline"))
        if deadline is None or at >= deadline:
            _halt(engine, day, "unknown_provider_outcome", at, clock)
            return False
        pending = True
    return not pending


def _reserve(
    engine: Engine,
    attempt: Record,
    target: Record,
    leg: str,
    ticker: str,
    at: datetime,
    clock: Callable[[], datetime],
    client: QuoteClient,
) -> None:
    day = at.astimezone(_ET).date()
    if (
        day != target.day
        or _halted(engine, day, at)
        or not _bootstrap(engine, at, clock, attempt.key)
    ):
        raise _RefusedError("day_halted")
    if _cooling(engine, day, at):
        raise _RefusedError("cooldown")
    existing = storage.list_events(
        engine, "quota_slot", at, day=day, require_receipt=False, limit=129
    )
    owner = digest((attempt.key, leg))
    if (
        len(existing) > config.settings().module_daily_cap
        or storage.count(engine, "quota_slot", at, day=day, include_future=True) > len(existing)
        or any(r.unique_owner == owner for r in existing)
    ):
        raise _RefusedError("request_replay")
    granted = None
    for ordinal in range(1, config.settings().module_daily_cap + 1):
        granted = storage.put(
            engine,
            key=_key("quota_slot", day, ordinal),
            kind="quota_slot",
            at=at,
            day=day,
            parent_key=attempt.key,
            unique_owner=owner,
            payload={
                "identity": config.identity(),
                "attempt_key": attempt.key,
                "ticker": ticker,
                "leg": leg,
                "slot": ordinal,
            },
            clock=clock,
        )
        if granted is not None:
            break
    if granted is None:
        _halt(engine, day, "module_limit", at, clock)
        raise _RefusedError("module_limit")
    reserve = quota_ledger.reserve(
        engine,
        day=day,
        n=1,
        cap=load_board_settings().refresh.daily_request_soft_cap,
        observed=quota_ledger.observed_for_day(client, day, now=at),
        now=at,
    )
    if not reserve.granted:
        _halt(engine, day, "shared_limit", at, clock)
        raise _RefusedError("shared_limit")


def _record_packet(
    engine: Engine,
    attempt: Record,
    leg: str,
    packet: Packet,
    at: datetime,
    clock: Callable[[], datetime],
) -> Record | None:
    raw = packet.raw_body
    integrity = (
        raw is not None
        and len(raw) <= config.settings().max_body_bytes
        and len(raw) == packet.body_bytes
        and hashlib.sha256(raw).hexdigest() == packet.body_sha256
    )
    return storage.put(
        engine,
        key=_key("packet", attempt.key, leg),
        kind="packet",
        at=at,
        day=attempt.day,
        parent_key=attempt.key,
        blob=raw if raw is not None and len(raw) <= config.settings().max_body_bytes else None,
        payload={
            "identity": config.identity(),
            "leg": leg,
            "ticker": packet.ticker,
            "status": packet.status,
            "http_status": packet.http_status,
            "requested_at": packet.requested_at.isoformat() if packet.requested_at else None,
            "received_at": packet.received_at.isoformat() if packet.received_at else None,
            "body_sha256": packet.body_sha256,
            "body_bytes": packet.body_bytes,
            "wire_attempted": packet.wire_attempted,
            "integrity": integrity,
        },
        clock=clock,
    )


async def _attempt(
    engine: Engine,
    client: QuoteClient,
    target: Record,
    ordinal: int,
    at: datetime,
    clock: Callable[[], datetime],
) -> bool:
    deadline = _time(target.payload.get("deadline"))
    assert deadline is not None and target.day is not None
    attempt = storage.put(
        engine,
        key=_key("attempt", target.key, ordinal),
        kind="attempt",
        at=at,
        day=target.day,
        parent_key=target.key,
        payload={
            "identity": config.identity(),
            "target_key": target.key,
            "ordinal": ordinal,
            "deadline": deadline.isoformat(),
        },
        clock=clock,
    )
    if attempt is None:
        return False
    reason = ""
    try:
        for leg, ticker in (("own", str(target.payload["ticker"])), ("spy", "SPY")):

            async def before_request(leg: str = leg, ticker: str = ticker) -> None:
                nonlocal at
                at = _physical(clock, at)
                if at >= deadline:
                    raise _RefusedError("late")
                _reserve(engine, attempt, target, leg, ticker, at, clock, client)
                at = _physical(clock, at)
                if at >= deadline:
                    raise _RefusedError("late")

            try:
                packet = await client.fetch(
                    ticker, before_request=before_request, deadline=deadline
                )
            except _RefusedError as exc:
                reason = str(exc)
                break
            at = _physical(clock, max(at, packet.received_at or at))
            if packet.status in {"auth", "daily_quota", "quota_unknown"}:
                _halt(engine, target.day, packet.status, at, clock)
                reason = packet.status
            elif packet.wire_attempted and (packet.raw_body is None or packet.http_status is None):
                _halt(engine, target.day, "unknown_provider_outcome", at, clock)
                reason = "unknown_provider_outcome"
            elif packet.status == "rate_limited":
                _cooldown(engine, target.day, at, clock)
                reason = "rate_limited"
            recorded = _record_packet(engine, attempt, leg, packet, at, clock)
            if recorded is None or recorded.confirmed_at is None:
                raise _RefusedError("packet_commit_unverified")
            at = _physical(clock, max(at, recorded.confirmed_at))
            if reason:
                break
        own_row = storage.get(engine, _key("packet", attempt.key, "own"), at)
        spy_row = storage.get(engine, _key("packet", attempt.key, "spy"), at)
        own = _quote(own_row, str(target.payload["ticker"]), target, at)
        spy = _quote(spy_row, "SPY", target, at)
        paired = (
            own.status == spy.status == "ok"
            and own.trade_at is not None
            and spy.trade_at is not None
            and abs((own.trade_at - spy.trade_at).total_seconds())
            <= config.settings().pair_skew_seconds
        )
        done = storage.put(
            engine,
            key=_key("attempt_done", attempt.key),
            kind="attempt_done",
            at=at,
            day=target.day,
            parent_key=attempt.key,
            payload={
                "identity": config.identity(),
                "paired": paired,
                "reason": reason or own.reason or spy.reason,
            },
            clock=clock,
        )
        if done is None or done.confirmed_at is None:
            raise _RefusedError("completion_unverified")
        at = _physical(clock, max(at, done.confirmed_at))
        if paired and at <= deadline:
            _outcome(engine, target, at, "observed", "", clock, attempt=attempt, own=own, spy=spy)
            if verified_outcome(engine, target, aware(clock())) is None:
                _halt(engine, target.day, "outcome_unverified", aware(clock()), clock)
                return False
            return True
        return False
    except Exception:
        # Unknown storage/client outcomes cannot permit a second request later.
        _HALTS.setdefault(engine, {})[target.day] = "storage_unavailable"
        raise


async def _tick(
    engine: Engine, client: QuoteClient, now: datetime, *, clock: Callable[[], datetime]
) -> dict[str, object]:
    at = aware(now)
    active = storage.active(engine, at)
    if active is None:
        return {"status": "activation_unavailable", "targets": 0, "attempts": 0, "observed": 0}
    candidates = ensure_targets(engine, active, at, clock=clock)
    day = at.astimezone(_ET).date()
    if not _bootstrap(engine, at, clock):
        return {
            "status": "halted" if _halted(engine, day, at) else "bootstrap_hold",
            "candidates": candidates,
            "targets": 0,
            "attempts": 0,
            "observed": 0,
        }
    targets = storage.list_events(engine, "target", at, day=day, limit=128)
    attempts_count = observed = pending = 0
    for target in targets:
        if not _identity(target, active):
            continue
        at = _physical(clock, at)
        existing = storage.get(engine, _key("outcome", target.key), at, require_receipt=False)
        if existing is not None:
            if storage.get(engine, existing.key, at) is None:
                _halt(engine, day, "outcome_unverified", at, clock)
            continue
        if target.payload.get("eligible") is not True:
            if not _target_valid(engine, target, at):
                _halt(engine, day, "target_unverified", at, clock)
                continue
            _outcome(engine, target, at, "not_applicable", str(target.payload.get("reason")), clock)
            continue
        due, deadline = (
            _time(target.payload.get("target_at")),
            _time(target.payload.get("deadline")),
        )
        if due is None or deadline is None:
            _halt(engine, day, "target_unverified", at, clock)
            continue
        attempts = storage.list_events(engine, "attempt", at, day=day, parent_key=target.key)
        done = {
            int(a.payload["ordinal"]): d.confirmed_at
            for a in attempts
            if (d := storage.get(engine, _key("attempt_done", a.key), at)) is not None
            and d.confirmed_at is not None
        }
        planned = schedule(
            due,
            deadline,
            at,
            [int(a.payload["ordinal"]) for a in attempts],
            done,
            offsets=config.settings().attempt_offsets_seconds,
            spacing_seconds=config.settings().attempt_spacing_seconds,
        )
        if _halted(engine, day, at):
            _outcome(engine, target, at, "missing", "day_halted", clock)
        elif planned.state in {"late", "exhausted", "invalid"}:
            if not _target_valid(engine, target, at):
                _halt(engine, day, "target_unverified", at, clock)
                continue
            _outcome(
                engine,
                target,
                at,
                "missing",
                {
                    "late": "window_expired",
                    "exhausted": "attempts_exhausted",
                    "invalid": "invalid_attempt",
                }[planned.state],
                clock,
            )
        elif planned.state == "ready" and not _cooling(engine, day, at):
            if not _target_valid(engine, target, at):
                _halt(engine, day, "target_unverified", at, clock)
                continue
            assert planned.ordinal is not None
            observed += await _attempt(engine, client, target, planned.ordinal, at, clock)
            attempts_count += 1
        else:
            pending += 1
    return {
        "status": "halted" if _halted(engine, day, at) else "active" if targets else "waiting",
        "candidates": candidates,
        "targets": len(targets),
        "attempts": attempts_count,
        "observed": observed,
        "pending": pending,
    }


async def tick(
    engine: Engine, client: QuoteClient, now: datetime, *, clock: Callable[[], datetime]
) -> dict[str, object]:
    """Run one bounded poll, retaining any unknown failure for this engine's day."""
    at = aware(now)
    try:
        return await _tick(engine, client, at, clock=clock)
    except Exception:
        _HALTS.setdefault(engine, {})[at.astimezone(_ET).date()] = "storage_unavailable"
        raise
