"""Durably owned, prospective timed references; never old opening observations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol
from weakref import WeakKeyDictionary

from webapp.timed_measurement.config import settings
from webapp.timed_measurement.model import (
    Activation,
    Assessment,
    Attempt,
    Completion,
    Leg,
    LegEvidence,
    Target,
    aware,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from webapp.timed_measurement.transport import Packet

_HALTS: WeakKeyDictionary[Engine, dict[date, str]] = WeakKeyDictionary()
_COOLDOWNS: WeakKeyDictionary[Engine, dict[date, datetime]] = WeakKeyDictionary()


def _halt(engine: Engine, day: date, reason: str, at: datetime) -> None:
    from webapp.timed_measurement import storage

    # Keep the known provider refusal even when its durable write fails. The
    # owned service retains its engine after a failed tick and cannot send again.
    _HALTS.setdefault(engine, {})[day] = reason
    storage.halt(engine, day, reason, at)


def _halted(engine: Engine, day: date, at: datetime) -> bool:
    from webapp.timed_measurement import storage

    return day in _HALTS.get(engine, {}) or storage.halted(engine, day, at) is not None


def _cooling(engine: Engine, day: date, at: datetime) -> bool:
    from webapp.timed_measurement import storage

    local = _COOLDOWNS.get(engine, {}).get(day)
    return bool(local and local > at) or storage.cooling_until(engine, day, at) is not None


@dataclass(frozen=True)
class Schedule:
    state: Literal["pending", "ready", "incomplete", "exhausted", "late", "invalid"]
    ordinal: int | None = None
    due_at: datetime | None = None


def schedule(
    target_at: datetime,
    deadline: datetime,
    now: datetime,
    claims: Sequence[int],
    completed: Mapping[int, datetime],
    *,
    offsets: Sequence[int],
    spacing_seconds: int,
) -> Schedule:
    """A claimed ordinal is never retried, including an unconfirmed crash."""
    if (
        any(at.tzinfo is None for at in (target_at, deadline, now, *completed.values()))
        or deadline <= target_at
        or not offsets
        or spacing_seconds < 0
        or len(set(claims)) != len(claims)
        or any(i < 1 or i > len(offsets) for i in claims)
        or any(i not in claims for i in completed)
        or sorted(claims) != list(range(1, len(claims) + 1))
        or any(at < target_at or at > now for at in completed.values())
        or any(completed[i] < completed[i - 1] for i in completed if i > 1 and i - 1 in completed)
    ):
        return Schedule("invalid")
    if now >= deadline:
        return Schedule("late")
    if any(i not in completed for i in claims):
        return Schedule("incomplete")
    ordinal = len(claims) + 1
    if ordinal > len(offsets):
        return Schedule("exhausted")
    due = target_at + timedelta(seconds=offsets[ordinal - 1])
    if ordinal > 1:
        due = max(due, completed[ordinal - 1] + timedelta(seconds=spacing_seconds))
    return Schedule("ready" if now >= due else "pending", ordinal, due)


@dataclass(frozen=True)
class Quote:
    status: str
    reason: str
    price: float | None = None
    trade_at: datetime | None = None
    phase: str | None = None


def parse_quote(
    ticker: str,
    payload: object,
    requested_at: datetime | None,
    received_at: datetime | None,
    target_at: datetime,
    deadline: datetime,
    *,
    trade_age_seconds: int,
) -> Quote:
    """Validate the fixed UW last-trade fields, never midpoint or option prices."""
    import math
    from datetime import UTC
    from decimal import Decimal, InvalidOperation
    from zoneinfo import ZoneInfo

    from uoa_detector.live_alpha.calendar import MarketMode, classify
    from uoa_detector.live_alpha.settings import load_live_alpha_settings

    if (
        requested_at is None
        or received_at is None
        or any(at.tzinfo is None for at in (requested_at, received_at, target_at, deadline))
        or not target_at <= requested_at <= received_at <= deadline
    ):
        return Quote("invalid_source", "request_receipt_clock")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return Quote("invalid_source", "quote_envelope")
    body = payload["data"]
    for owner in (payload, body):
        for name in ("ticker", "symbol", "ticker_symbol"):
            if name in owner and owner[name] != ticker:
                return Quote("invalid_source", "ticker_mismatch")
    phase = body.get("market_time")
    if phase != "regular":
        return Quote("invalid_source", "not_regular_phase")
    trade = body.get("last_trade")
    if not isinstance(trade, dict):
        return Quote("invalid_source", "last_trade_missing")
    raw_price, raw_time = trade.get("price"), trade.get("time")
    if isinstance(raw_price, bool) or not isinstance(raw_price, int | float | str):
        return Quote("invalid_source", "last_trade_price")
    try:
        price = float(Decimal(str(raw_price)))
    except (InvalidOperation, ValueError, OverflowError):
        return Quote("invalid_source", "last_trade_price")
    if not math.isfinite(price) or price <= 0:
        return Quote("invalid_source", "last_trade_price")
    if isinstance(raw_time, bool) or not isinstance(raw_time, int) or raw_time <= 0:
        return Quote("invalid_source", "last_trade_time")
    try:
        trade_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=raw_time)
    except (ValueError, OverflowError):
        return Quote("invalid_source", "last_trade_time")
    cal = load_live_alpha_settings().calendar
    zone = ZoneInfo(cal.timezone)
    if (
        trade_at.astimezone(zone).date() != target_at.astimezone(zone).date()
        or classify(trade_at, cal).mode != MarketMode.LIVE
        or not target_at <= trade_at <= received_at
        or received_at - trade_at > timedelta(seconds=trade_age_seconds)
    ):
        return Quote("invalid_source", "last_trade_outside_window")
    return Quote("ok", "", price, trade_at, phase)


class QuoteClient(Protocol):
    @property
    def last_daily_request_count(self) -> int | None: ...

    @property
    def last_daily_request_at(self) -> datetime | None: ...

    async def fetch(
        self,
        ticker: str,
        *,
        before_request: Callable[[], Awaitable[None]] | None = ...,
        deadline: datetime | None = ...,
    ) -> Packet: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Tick:
    status: str
    targets: int = 0
    active: int = 0
    attempts: int = 0
    assessed: int = 0


class _RefusedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _physical(clock: Callable[[], datetime], previous: datetime) -> datetime:
    at = aware(clock())
    if at < previous:
        raise _RefusedError("clock_error")
    return at


def eligible(
    target: Target,
    evidence: LegEvidence | None,
    completion: Completion,
    now: datetime,
    *,
    leg: Leg,
) -> bool:
    """Only a receipt chain durably confirmed inside the window can qualify."""
    import math

    if evidence is None or evidence.status != "ok" or evidence.leg != leg:
        return False
    own = evidence.leg == "own"
    actual = completion.own_confirmed_at if own else completion.spy_confirmed_at
    if actual is None:
        return False
    confirmed = max(evidence.confirmed_at, actual)
    requested, received, trade = evidence.requested_at, evidence.received_at, evidence.trade_at
    return bool(
        evidence.ticker == (target.ticker if own else "SPY")
        and completion.target_key == target.key
        and completion.key == completion.attempt_key
        and 1 <= completion.ordinal <= settings().max_attempts
        and evidence.attempt_key == completion.attempt_key
        and evidence.wire_attempted
        and evidence.http_status == 200
        and evidence.source_path == settings().source_path.format(ticker=evidence.ticker)
        and evidence.price is not None
        and math.isfinite(evidence.price)
        and evidence.price > 0
        and requested is not None
        and received is not None
        and trade is not None
        and target.target_at <= requested <= received <= evidence.receipt_at
        <= evidence.confirmed_at <= confirmed <= target.deadline
        and confirmed <= now
        and target.target_at <= trade <= received
        and confirmed - trade <= timedelta(seconds=settings().trade_max_age_seconds)
        and evidence.market_phase == "regular"
    )


def _assessment(
    target: Target,
    completions: Sequence[Completion],
    evidence: Mapping[str, LegEvidence],
    now: datetime,
    *,
    terminal: str = "",
) -> Assessment | None:
    """First complete same-attempt pair wins; partial own is fixed only on termination."""
    import math

    def output(status: str, reason: str, own: LegEvidence | None = None,
               spy: LegEvidence | None = None) -> Assessment:
        sign = 1 if target.direction == "up" else -1
        value = excess = None
        if own and own.price is not None and target.entry_reference and target.entry_reference > 0:
            value = sign * (own.price / target.entry_reference - 1) * 100
            if spy and spy.price is not None and target.spy_entry and target.spy_entry > 0:
                excess = value - sign * (spy.price / target.spy_entry - 1) * 100
            if not math.isfinite(value) or (excess is not None and not math.isfinite(excess)):
                status, reason, value, excess = "unknown", "nonfinite_return", None, None
        return Assessment(
            key=target.key, target_key=target.key, assessed_at=now,
            status=status, reason=reason, own_key=own.key if own else None,
            spy_key=spy.key if spy else None, return_pct=value, excess_pp=excess,
        )

    if target.entry_reference is None or not math.isfinite(target.entry_reference) or target.entry_reference <= 0:
        return output("unknown", "entry_reference_invalid")
    first_own = None
    for completion in sorted(completions, key=lambda c: c.ordinal):
        own = evidence.get(completion.own_key or "")
        spy = evidence.get(completion.spy_key or "")
        own_ok = eligible(target, own, completion, now, leg="own")
        spy_ok = eligible(target, spy, completion, now, leg="spy")
        if own_ok and first_own is None:
            first_own = own
        if own_ok and spy_ok:
            assert own and spy and own.trade_at and spy.trade_at
            if abs(own.trade_at - spy.trade_at) <= timedelta(seconds=settings().pair_skew_seconds):
                if target.spy_entry is None or not math.isfinite(target.spy_entry) or target.spy_entry <= 0:
                    return output("partial", "missing_entry_spy", own, spy)
                return output("observed", "paired_reference", own, spy)
    if not terminal:
        return None
    if first_own:
        return output("partial", "missing_exit_spy", first_own)
    return output("unknown" if terminal in {"incomplete_attempt", "clock_error", "invalid_attempt"}
                  else "missing", terminal)


def _evaluate(engine: Engine, target: Target, now: datetime, terminal: str = "") -> bool:
    from webapp.timed_measurement import storage

    completed = storage.completions(engine, target.key, now)
    evidence = {
        leg.key: leg
        for completion in completed
        for leg in storage.legs(engine, completion.attempt_key, now)
    }
    result = _assessment(target, completed, evidence, now, terminal=terminal)
    return bool(result and storage.assess(engine, result))


def _discover(engine: Engine, activation: Activation, at: datetime) -> None:
    from webapp.timed_measurement.bindings import discover

    discover(engine, activation, at)


async def tick(
    engine: Engine,
    client: QuoteClient,
    now: datetime,
    *,
    clock: Callable[[], datetime],
    permit_requests: Callable[[datetime, str | None], bool] | None = None,
) -> Tick:
    """One bounded discovery/collection pass, called solely by the owned worker."""
    from webapp.board.settings import load_board_settings
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.model import LegInput

    cfg = settings()
    at = aware(now)
    activation = storage.activation(engine, at)
    if activation is None:
        return Tick("activation_unavailable")
    _discover(engine, activation, at)
    targets = tuple(
        target for target in storage.targets(engine, at)
        if target.activation_key == activation.key
        and target.profile_hash == activation.profile_hash
        and target.opening_profile_hash == activation.opening_profile_hash
        and target.published_at >= activation.activated_at
    )
    active = attempted = assessed = 0
    for target in targets:
        if storage.assessment(engine, target.key, at) is not None:
            continue
        if not target.eligible:
            result = Assessment(key=target.key, target_key=target.key,
                                assessed_at=at, status="not_applicable", reason=target.reason)
            assessed += bool(storage.assess(engine, result))
            continue
        at = _physical(clock, at)
        if _evaluate(engine, target, at):
            assessed += 1
            continue
        attempts = storage.attempts(engine, target.key)
        completions = storage.completions(engine, target.key, at)
        planned = schedule(target.target_at, target.deadline, at,
                           [a.ordinal for a in attempts],
                           {c.ordinal: c.confirmed_at for c in completions},
                           offsets=cfg.attempt_offsets_seconds,
                           spacing_seconds=cfg.attempt_spacing_seconds)
        terminal = {"late": "window_expired", "exhausted": "attempts_exhausted",
                    "invalid": "invalid_attempt"}.get(planned.state, "")
        if planned.state == "late" and any(a.ordinal not in {c.ordinal for c in completions} for a in attempts):
            terminal = "incomplete_attempt"
        if _halted(engine, target.day, at):
            terminal = "day_halted"
        if terminal:
            assessed += _evaluate(engine, target, at, terminal)
            continue
        active += 1
        if planned.state != "ready":
            continue
        if permit_requests is not None and not permit_requests(at, None):
            continue
        if _cooling(engine, target.day, at):
            continue
        assert planned.ordinal is not None
        claimed = storage.claim(engine, target, planned.ordinal, at)
        if claimed is None:
            continue
        attempt: Attempt = claimed
        attempted += 1
        own_evidence = spy_evidence = None
        own_confirmed = spy_confirmed = None
        pair_reason = ""
        for leg, ticker in (("own", target.ticker), ("spy", "SPY")):
            leg_name: Leg = "own" if leg == "own" else "spy"

            async def before_request(leg_name: Leg = leg_name, ticker: str = ticker,
                                     target: Target = target, attempt: Attempt = attempt) -> None:
                nonlocal at
                from webapp.board.quota_ledger import observed_for_day

                at = _physical(clock, at)
                if at >= target.deadline:
                    raise _RefusedError("late")
                if permit_requests is not None and not permit_requests(at, attempt.key):
                    raise _RefusedError("bootstrap_hold")
                if _halted(engine, target.day, at):
                    raise _RefusedError("day_halted")
                if _cooling(engine, target.day, at):
                    raise _RefusedError("cooldown")
                got = storage.reserve_leg(
                    engine, attempt, leg_name, ticker, at,
                    observed=observed_for_day(client, target.day, now=at),
                    shared_cap=load_board_settings().refresh.daily_request_soft_cap,
                )
                if not got.granted:
                    raise _RefusedError(got.reason)
                at = _physical(clock, at)
                if at >= target.deadline:
                    raise _RefusedError("late")

            try:
                packet = await client.fetch(ticker, before_request=before_request, deadline=target.deadline)
            except _RefusedError as refused:
                pair_reason = refused.reason
                if refused.reason not in {"late", "clock_error", "cooldown", "bootstrap_hold"}:
                    _halt(engine, target.day, refused.reason, at)
                break
            received = packet.received_at
            fatal = packet.status in {"auth", "daily_quota", "quota_unknown"}
            if fatal:
                _HALTS.setdefault(engine, {})[target.day] = packet.status
            if packet.status == "rate_limited":
                # Set the local guard first: any subsequent storage failure must
                # retain the cooldown instead of allowing another target to send.
                _COOLDOWNS.setdefault(engine, {})[target.day] = max(at, received or at) + timedelta(seconds=cfg.rate_limit_cooldown_seconds)
            if fatal or packet.status == "rate_limited":
                at = _physical(clock, max(at, received or at))
                if fatal:
                    _halt(engine, target.day, packet.status, at)
                else:
                    until = at + timedelta(seconds=cfg.rate_limit_cooldown_seconds)
                    _COOLDOWNS.setdefault(engine, {})[target.day] = until
                    storage.cooldown(engine, target.day, at, until)
            quote = parse_quote(ticker, packet.payload, packet.requested_at, received,
                                target.target_at, target.deadline,
                                trade_age_seconds=cfg.trade_max_age_seconds)
            status = quote.status if packet.status == "ok" else packet.status
            reason = quote.reason if packet.status == "ok" else packet.status
            if packet.ticker != ticker:
                status, reason = "invalid_source", "ticker_mismatch"
            leg_input = LegInput(
                leg=leg_name, ticker=ticker, status=status, reason=reason,
                requested_at=packet.requested_at, received_at=received,
                http_status=packet.http_status, payload=packet.payload,
                wire_body_hash=packet.body_sha256, body_bytes=packet.body_bytes,
                wire_attempted=packet.wire_attempted, native_daily_count=packet.native_daily_count,
                native_count_at=packet.native_count_at, price=quote.price if status == "ok" else None,
                trade_at=quote.trade_at if status == "ok" else None, market_phase=quote.phase,
                source_path=cfg.source_path.format(ticker=ticker),
                raw_body=packet.raw_body,
            )
            stored = storage.record_leg(engine, attempt, leg_input, clock)
            at = _physical(clock, max(at, received or at))
            if leg_name == "own":
                own_evidence, own_confirmed = stored, at
            else:
                spy_evidence, spy_confirmed = stored, at
            if fatal:
                pair_reason = status
                break
            if status == "rate_limited":
                pair_reason = "rate_limited"
                break
            if at >= target.deadline or status == "clock_error":
                pair_reason = "late" if at >= target.deadline else "clock_error"
                break
        completion = storage.complete(
            engine, attempt, own_evidence, spy_evidence, at, reason=pair_reason,
            own_confirmed_at=own_confirmed, spy_confirmed_at=spy_confirmed, clock=clock,
        )
        at = _physical(clock, max(at, completion.confirmed_at))
        terminal = (
            "day_halted" if _halted(engine, target.day, at)
            else "window_expired" if at >= target.deadline
            else "clock_error" if pair_reason == "clock_error"
            else "attempts_exhausted" if attempt.ordinal >= cfg.max_attempts
            else ""
        )
        assessed += _evaluate(engine, target, at, terminal)
    return Tick("ok", len(targets), active, attempted, assessed)
