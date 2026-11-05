"""Single-attempt native UW quote transport; no SQL, implicit retries or price substitution.

``Any`` is confined to the untrusted vendor JSON adapter boundary. Source admission
and durable post-commit availability belong to the collector, never this transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources._http_base import RetryPolicy
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesResponseBodyError,
    UnusualWhalesTransientError,
)

Status = Literal[
    "ok", "auth", "daily_quota", "rate_limited", "not_found", "transport",
    "timeout", "invalid_payload", "body_limit", "clock_error", "circuit_open",
    "late",
    "quota_unknown",
]
_TICKER = re.compile(r"[A-Z][A-Z0-9.-]*", re.ASCII)


def _now() -> datetime:
    return datetime.now(UTC)


class _ClockError(ValueError):
    pass


class _BeforeRequestError(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error


class _RequestLateError(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _ClockError("physical clock must be aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Packet:
    ticker: str
    status: Status
    requested_at: datetime | None
    received_at: datetime | None
    payload: dict[str, Any] | None = field(repr=False)
    http_status: int | None
    body_sha256: str | None
    body_bytes: int
    wire_attempted: bool
    native_daily_count: int | None
    native_count_at: datetime | None
    raw_body: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _Flight:
    requested_at: datetime | None = None
    received_at: datetime | None = None
    http_status: int | None = None
    body_sha256: str | None = None
    body_bytes: int = 0
    wire_attempted: bool = False
    json_object: bool = False
    raw_body: bytes | None = field(default=None, repr=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(value: str) -> Any:
    raise ValueError("non-finite JSON number")


class QuoteTransport:
    """One native client retains its token bucket, proxy/CA settings and circuit breaker."""

    def __init__(
        self, *, api_key: SecretStr, settings: UnusualWhalesSettings,
        timeout_seconds: float, max_response_bytes: int,
        clock: Callable[[], datetime] = _now,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("positive finite timeout required")
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()
        self._flight = _Flight()
        self._before: Callable[[], Awaitable[None]] | None = None
        self._deadline: datetime | None = None
        self._client = UnusualWhalesClient(
            api_key=api_key, settings=settings, retry=RetryPolicy(max_attempts=1),
            transport=transport, before_request=self._before_wire,
            after_response=self._after_response, max_response_bytes=max_response_bytes,
        )

    @property
    def last_daily_request_count(self) -> int | None:
        return self._client.last_daily_request_count

    @property
    def last_daily_request_at(self) -> datetime | None:
        return self._client.last_daily_request_at

    async def _before_wire(self) -> None:
        if self._before is not None:
            try:
                await self._before()
            except Exception as exc:
                raise _BeforeRequestError(exc) from None
        at = _utc(self._clock())
        self._flight = replace(self._flight, requested_at=at)
        if self._deadline is not None and at >= self._deadline:
            raise _RequestLateError("request deadline reached")
        self._flight = replace(self._flight, wire_attempted=True)

    async def _after_response(self, response: httpx.Response) -> None:
        # Called after the bounded stream is fully read, never on header arrival.
        moment = self._clock()
        raw = response.content
        self._flight = replace(
            self._flight, http_status=response.status_code,
            body_sha256=hashlib.sha256(raw).hexdigest(), body_bytes=len(raw),
            raw_body=raw,
        )
        received = _utc(moment)
        self._flight = replace(self._flight, received_at=received)
        if self._flight.requested_at is None or received < self._flight.requested_at:
            raise _ClockError("physical receipt precedes request")
        try:
            decoded = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_number)
        except (ValueError, TypeError, RecursionError):
            return
        self._flight = replace(self._flight, json_object=isinstance(decoded, dict))

    def _packet(self, ticker: str, status: Status, payload: dict[str, Any] | None = None) -> Packet:
        flight = self._flight
        return Packet(
            ticker=ticker, status=status, requested_at=flight.requested_at,
            received_at=flight.received_at, payload=payload,
            http_status=flight.http_status, body_sha256=flight.body_sha256,
            body_bytes=flight.body_bytes, wire_attempted=flight.wire_attempted,
            native_daily_count=self.last_daily_request_count,
            native_count_at=self.last_daily_request_at,
            raw_body=flight.raw_body,
        )

    async def fetch(
        self, ticker: str, *, before_request: Callable[[], Awaitable[None]] | None = None,
        deadline: datetime | None = None,
    ) -> Packet:
        """Callback refusal/cancellation escapes; provider errors return only fixed categories."""
        if not _TICKER.fullmatch(ticker):
            raise ValueError("uppercase ticker required")
        if deadline is not None:
            deadline = _utc(deadline)
        async with self._lock:
            self._flight, self._before, self._deadline = _Flight(), before_request, deadline
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    payload = await self._client.request_json(f"/api/stock/{ticker}/quote")
                if self._flight.http_status != 200 or not self._flight.json_object:
                    return self._packet(ticker, "invalid_payload")
                if not isinstance(payload.get("data"), dict):
                    return self._packet(ticker, "invalid_payload")
                return self._packet(ticker, "ok", payload)
            except _BeforeRequestError as exc:
                raise exc.error from None
            except _ClockError:
                return self._packet(ticker, "clock_error")
            except _RequestLateError:
                return self._packet(ticker, "late")
            except (TimeoutError, httpx.TimeoutException):
                return self._packet(ticker, "timeout")
            except UnusualWhalesResponseBodyError as exc:
                self._flight = replace(
                    self._flight, http_status=exc.status_code, body_bytes=exc.body_bytes,
                )
                if exc.status_code in (401, 403):
                    return self._packet(ticker, "auth")
                if exc.status_code == 429:
                    return self._packet(ticker, "quota_unknown")
                return self._packet(
                    ticker, "body_limit" if exc.reason == "body_limit" else "invalid_payload",
                )
            except UnusualWhalesDailyLimitError:
                return self._packet(ticker, "daily_quota")
            except UnusualWhalesNotFoundError:
                return self._packet(ticker, "not_found")
            except UnusualWhalesRateLimitError:
                return self._packet(ticker, "rate_limited")
            except UnusualWhalesAuthError:
                return self._packet(ticker, "auth")
            except CircuitBreakerOpenError:
                return self._packet(ticker, "circuit_open")
            except UnusualWhalesTransientError:
                return self._packet(
                    ticker, "invalid_payload" if self._flight.http_status == 200 else "transport",
                )
            except (ValueError, TypeError, RecursionError):
                return self._packet(ticker, "invalid_payload")
            finally:
                self._before, self._deadline = None, None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> QuoteTransport:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
