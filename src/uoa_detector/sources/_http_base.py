"""Shared HTTP infrastructure for vendor adapters.

Phase 3.3.3 extracts the rate-limit + circuit-breaker + retry-policy
primitives that ThetaData (Phase 3.3.2) and Unusual Whales (Phase
3.3.3) share. Each vendor's client wraps these with its own auth
headers, base URL, and exception hierarchy — but the throttling,
back-pressure, and retry semantics are identical.

The classes here are vendor-neutral. Vendor errors live in the
respective adapter modules and inherit from RuntimeError directly.
The one exception that needs to live here is ``CircuitBreakerOpenError``
because it's raised by the breaker itself (which is vendor-neutral)
and clients raise it pre-flight when the breaker is open.

Design notes:
  - TokenBucket uses ``time.monotonic()`` so wall-clock jumps don't
    cause spurious refills.
  - CircuitBreaker is closed/open/half-open with a single internal
    flag (``_opened_at``); half-open is implicit when cooldown has
    elapsed but failures still register.
  - RetryPolicy is a plain dataclass — no behaviour, just tunables.
    Clients decide how to use it.

These classes were originally inlined in
``sources/thetadata/client.py``; Phase 3.3.3.2 moved them here. The
ThetaData client now imports from this module and re-exports for
backwards compatibility with its existing test surface.
"""

from __future__ import annotations

import asyncio
import time as time_module
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Vendor-neutral exception
# ---------------------------------------------------------------------------


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a request is refused pre-flight because the
    breaker is open. Vendor adapters should treat this as a
    transient operational error (operator should investigate the
    sustained failure that tripped it) — not as a programming error.
    """


# ---------------------------------------------------------------------------
# Token bucket rate limiter
# ---------------------------------------------------------------------------


@dataclass
class TokenBucket:
    """Simple token-bucket rate limiter.

    ``capacity`` tokens, refilled at ``refill_per_second`` tokens
    per second. ``acquire()`` blocks (asyncio sleep) until a token
    is available; ``try_acquire()`` returns immediately with a bool.

    The bucket is monotonic-clock based; it survives wall-clock
    jumps without spurious refills.
    """

    capacity: float
    refill_per_second: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time_module.monotonic()

    def _refill(self) -> None:
        now = time_module.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.refill_per_second,
            )
            self._last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking: take ``tokens`` if available, else False."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> None:
        """Async: block until ``tokens`` tokens are available."""
        while not self.try_acquire(tokens):
            self._refill()
            shortfall = tokens - self._tokens
            wait_s = shortfall / self.refill_per_second
            await asyncio.sleep(max(wait_s, 0.001))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """Trips after N consecutive failures; resets after a cooldown.

    States:
      CLOSED  — requests pass through; failures increment a counter
      OPEN    — requests refused; cooldown timer running
      HALF    — one probe allowed; success closes, failure re-opens

    The probe-on-half-open is the standard pattern; we don't expose
    it as a separate state externally.
    """

    threshold: int = 5
    reset_seconds: float = 30.0
    _failures: int = field(init=False, default=0)
    _opened_at: float | None = field(init=False, default=None)

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        # Open while cooldown not yet elapsed; otherwise allow probe.
        return time_module.monotonic() - self._opened_at < self.reset_seconds

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time_module.monotonic()


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass
class RetryPolicy:
    """Retry policy for transient errors.

    ``max_attempts=3`` means up to 3 calls total (1 initial + 2
    retries). ``initial_backoff_s`` doubles per retry up to
    ``max_backoff_s``.
    """

    max_attempts: int = 3
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 30.0
