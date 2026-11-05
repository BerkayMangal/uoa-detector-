"""Opt-in isolated collector lifetime; no HTTP handler can start measurement I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from webapp.timed_measurement.collector import QuoteClient, tick
from webapp.timed_measurement.config import configured_engine, settings
from webapp.timed_measurement.config import enabled as enabled
from webapp.timed_measurement.model import Activation, Attempt, Target, aware

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

STATE: dict[str, object] = {}


def safe_status() -> dict[str, object]:
    """Process-local safe aggregates only; private payloads/identities never enter STATE."""
    return {key: STATE[key] for key in ("status", "targets", "active", "attempts", "assessed", "at", "barrier", "barrier_pending")
            if key in STATE and isinstance(STATE[key], str | int | float | bool)}


class _BootstrapBarrier:
    """Unfinished pre-worker claims cannot hide a refusal lost during a DB outage."""

    def __init__(self, engine: Engine, activation: Activation, started_at: datetime) -> None:
        self.engine = engine
        self.activation = activation
        self.boundary = aware(started_at)
        self.day = self.boundary.astimezone(ZoneInfo("America/New_York")).date()
        self.remaining: tuple[tuple[Attempt, Target], ...] = ()
        self.status = "unverified"

    def allow(self, now: datetime, owned_attempt_key: str | None = None) -> bool:
        from webapp.timed_measurement import storage
        from webapp.timed_measurement.collector import _halt

        at = aware(now)
        day = at.astimezone(ZoneInfo("America/New_York")).date()
        if day != self.day:
            self.day, self.boundary = day, at
            self.remaining = ()
        try:
            # A prior owner may have stamped its claim before our boundary while
            # its transaction was still uncommitted. Re-read on every grant so a
            # late-visible pre-worker claim cannot escape the startup barrier.
            opened = storage.read_open_attempts(self.engine, self.activation, self.day, at)
            if opened.limited:
                self.status = "limited"
                return False
            self.remaining = tuple((a, t) for a, t in opened.rows
                                   if a.claimed_at <= self.boundary and a.key != owned_attempt_key)
            unresolved = []
            for attempt, target in self.remaining:
                complete = any(c.attempt_key == attempt.key and c.ordinal == attempt.ordinal
                               for c in storage.completions(self.engine, target.key, at))
                if complete:
                    continue
                unresolved.append((attempt, target))
                if at >= target.deadline:
                    _halt(self.engine, self.day, "unknown_provider_outcome", at)
                    self.status = "unknown_provider_outcome"
                    return False
            self.remaining = tuple(unresolved)
        except Exception:
            # A failed or truncated ownership read cannot grant permission.
            self.status = "storage_unavailable"
            return False
        self.status = "holding" if self.remaining else "ready"
        return not self.remaining


def _client(clock: Callable[[], datetime]) -> QuoteClient:
    from uoa_detector.calibration import load_profile
    from uoa_detector.config.credentials import Credentials
    from webapp.board.refresher import _LIVE_CALIBRATION_PROFILE
    from webapp.timed_measurement.transport import QuoteTransport

    cfg = settings()
    return QuoteTransport(
        api_key=Credentials().require_unusual_whales_api_key(),
        settings=load_profile(_LIVE_CALIBRATION_PROFILE).data_sources.unusual_whales,
        timeout_seconds=cfg.request_timeout_seconds,
        max_response_bytes=cfg.max_body_bytes,
        clock=clock,
    )


async def loop(
    database_url: str,
    *,
    client_factory: Callable[[], QuoteClient] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> None:
    """Called inside existing run_service, owning engine/client on that worker loop."""
    if not enabled():
        STATE.clear()
        STATE.update(status="disabled")
        return
    if max_cycles is not None and max_cycles < 1:
        return
    from webapp.timed_measurement import storage

    now = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    cfg = settings()
    STATE.clear()
    STATE.update(status="starting")
    engine = configured_engine(database_url)
    client: QuoteClient | None = None
    try:
        client = client_factory() if client_factory else _client(now)
        storage.ensure(engine)
        activation = storage.activate(engine, now)
        if activation is None:
            STATE.update(status="activation_unavailable")
            return
        barrier = _BootstrapBarrier(engine, activation, aware(now()))
        barrier.allow(aware(now()))
        cycles = 0
        while True:
            delay = float(cfg.idle_poll_seconds)
            try:
                at = aware(now())
                result = await tick(engine, client, at, clock=now, permit_requests=barrier.allow)
                STATE.clear()
                STATE.update(asdict(result))
                STATE.update(at=aware(now()).isoformat())
                STATE.update(barrier=barrier.status, barrier_pending=len(barrier.remaining))
                if barrier.status != "ready":
                    STATE.update(status="bootstrap_hold")
                if result.active:
                    delay = cfg.active_poll_seconds
            except asyncio.CancelledError:
                STATE.clear()
                STATE.update(status="stopped")
                raise
            except Exception:
                # Exceptions from native SQL/HTTP can contain credentials. Fixed
                # categories only, with no stringification or raw traceback here.
                STATE.clear()
                STATE.update(status="failed")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            await pause(delay)
    finally:
        try:
            if client is not None:
                await client.aclose()
        finally:
            engine.dispose()
