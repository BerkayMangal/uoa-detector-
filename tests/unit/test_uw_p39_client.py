"""Phase 3.9.3 tests for ``unusual_whales.client``.

Contract: docs/phase-3.9-uw-endpoint-correction-acceptance.md §3.9.

All tests use httpx.MockTransport (no network, no env). Fixture bodies
are trimmed real responses captured live on 2026-09-14:
  - ``/api/stock/MSFT/flow-recent`` -> 200, top-level JSON array
  - ``/api/stock/ZZZZQ/info`` -> 404, body ``{}``
  - ``/api/stock/AAPL/not-a-real-endpoint`` -> 404, ``Route not found``
  - ``/api/stock/NOTATICKER/info`` -> 422, ``Invalid path input``

Pins:
  - Top-level arrays (including empty) are wrapped as ``{"data": [...]}``
  - 429 retried with ``rate_limit_backoff_s`` backoff, counts toward the
    breaker, typed ``UnusualWhalesRateLimitError`` on exhaustion, message
    carries ``(HTTP 429): <body[:120]>``
  - daily-quota 429 fails fast (one request, one breaker failure)
  - non-JSON / empty 200 body -> ``UnusualWhalesTransientError`` (retried,
    counted), never a bare ``json.JSONDecodeError``
  - 404/422 -> ``UnusualWhalesNotFoundError`` with ``status_code``, no
    retry, neither a breaker failure nor a breaker success
  - 401/403 stay ``UnusualWhalesAuthError`` and are never ``NotFound``
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.sources._http_base import RetryPolicy
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
    UnusualWhalesError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_CLIENT_SLEEP = "uoa_detector.sources.unusual_whales.client.asyncio.sleep"

# Trimmed real row from live GET /api/stock/MSFT/flow-recent (2026-09-14).
_FLOW_RECENT_ROW: dict[str, object] = {
    "id": "01a0920e-35e4-7702-9ac5-3f6b122c3dd8",
    "option_chain_id": "AAPL260914C00340000",
    "underlying_symbol": "AAPL",
    "executed_at": "2026-09-11T19:59:59.923266Z",
    "price": "0.20",
    "size": 10,
    "premium": "200.00",
    "nbbo_bid": "0.20",
    "nbbo_ask": "0.24",
    "tags": ["bid_side", "bearish"],
}

# Real live error bodies (2026-09-14).
_BODY_404_UNKNOWN_TICKER = "{}"
_BODY_404_ROUTE = '{"error":"Route not found"}'
_BODY_422_INVALID_INPUT = (
    '{"path":"/api/stock/NOTATICKER/info","msg":"Invalid path input: '
    'NOTATICKER (Invalid input format.)","query":""}'
)
_DAILY_LIMIT_BODY = '{"code":"daily_request_limit_hit"}'


def _settings() -> UnusualWhalesSettings:
    # 10 req/s, capacity 10: no test here drains the bucket, so the
    # TokenBucket never calls asyncio.sleep (which some tests patch).
    return UnusualWhalesSettings(
        rate_limit_requests_per_second=10.0,
        historical_concurrency=2,
        live_reconnect_max_attempts=5,
        live_reconnect_initial_backoff_s=1.0,
        live_reconnect_max_backoff_s=60.0,
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


def _fast_retry(max_attempts: int) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts, initial_backoff_s=0.001, max_backoff_s=0.01,
    )


class _Scripted:
    """MockTransport handler that replays a response script and counts
    calls. The last response repeats once the script runs out."""

    def __init__(self, responses: list[Callable[[], httpx.Response]]) -> None:
        self._responses = responses
        self.calls = 0

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]()


def _status(code: int, text: str = "") -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(code, text=text)


def _json(code: int, payload: object) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(code, json=payload)


def _client(
    handler: _Scripted,
    *,
    retry: RetryPolicy,
    threshold: int = 5,
    rate_limit_backoff_s: float | None = None,
) -> UnusualWhalesClient:
    if rate_limit_backoff_s is None:
        return UnusualWhalesClient(
            api_key=SecretStr("uw_test"),
            settings=_settings(),
            retry=retry,
            circuit_breaker_threshold=threshold,
            circuit_breaker_reset_s=60.0,
            transport=httpx.MockTransport(handler),
        )
    return UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=retry,
        circuit_breaker_threshold=threshold,
        circuit_breaker_reset_s=60.0,
        rate_limit_backoff_s=rate_limit_backoff_s,
        transport=httpx.MockTransport(handler),
    )


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_CLIENT_SLEEP, _fake_sleep)
    return sleeps


# ---------------------------------------------------------------------------
# Top-level array wrap
# ---------------------------------------------------------------------------


async def test_live_flow_recent_array_wrapped_as_data() -> None:
    """Live /flow-recent returns a bare array of trades; the client
    hands callers ``{"data": [...]}`` with rows untouched."""
    handler = _Scripted([_json(200, [_FLOW_RECENT_ROW, _FLOW_RECENT_ROW])])
    client = _client(handler, retry=_fast_retry(1))
    try:
        result = await client.request_json("/api/stock/MSFT/flow-recent")
    finally:
        await client.aclose()
    assert result == {"data": [_FLOW_RECENT_ROW, _FLOW_RECENT_ROW]}
    assert handler.calls == 1


async def test_top_level_empty_array_wrapped_not_error() -> None:
    """A quiet ticker returns ``[]``: no data, not a failure."""
    handler = _Scripted([_json(200, [])])
    client = _client(handler, retry=_fast_retry(3), threshold=1)
    try:
        result = await client.request_json("/api/stock/MSFT/flow-recent")
        assert not client.circuit_breaker.is_open()
    finally:
        await client.aclose()
    assert result == {"data": []}
    assert handler.calls == 1


# ---------------------------------------------------------------------------
# HTTP 429 — retried rate limit
# ---------------------------------------------------------------------------


async def test_429_retries_then_succeeds() -> None:
    handler = _Scripted([_status(429, "Too Many Requests"), _json(200, {"data": "ok"})])
    client = _client(handler, retry=_fast_retry(3))
    try:
        result = await client.request_json("/api/x")
        assert not client.circuit_breaker.is_open()
    finally:
        await client.aclose()
    assert result == {"data": "ok"}
    assert handler.calls == 2


async def test_429_exhaustion_raises_typed_rate_limit_error() -> None:
    handler = _Scripted([_status(429, "Too Many Requests")])
    client = _client(handler, retry=_fast_retry(3))
    try:
        with pytest.raises(
            UnusualWhalesRateLimitError, match="failed after 3 attempts",
        ) as info:
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 3
    assert isinstance(info.value, UnusualWhalesError)
    assert not isinstance(info.value, UnusualWhalesTransientError)
    assert not isinstance(info.value, UnusualWhalesDailyLimitError)
    assert "(HTTP 429): Too Many Requests" in str(info.value)


async def test_429_message_carries_first_120_chars_of_body() -> None:
    body = "R" * 100 + "S" * 19 + "T" + "U" * 30  # 150 chars; [:120] ends at "T"
    handler = _Scripted([_status(429, body)])
    client = _client(handler, retry=_fast_retry(1))
    try:
        with pytest.raises(UnusualWhalesRateLimitError) as info:
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    message = str(info.value)
    assert "(HTTP 429): " + body[:120] in message
    assert body[:121] not in message


async def test_429_backoff_uses_rate_limit_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 backoff = min(rate_limit_backoff_s * 2**attempt, max_backoff_s),
    independent of the transient ``initial_backoff_s``."""
    sleeps = _record_sleeps(monkeypatch)
    handler = _Scripted([_status(429, "Too Many Requests")])
    client = _client(
        handler,
        retry=RetryPolicy(max_attempts=3, initial_backoff_s=0.5, max_backoff_s=30.0),
    )
    try:
        with pytest.raises(UnusualWhalesRateLimitError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert sleeps == [1.0, 2.0]  # default rate_limit_backoff_s = 1.0


async def test_5xx_backoff_keeps_transient_base(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _record_sleeps(monkeypatch)
    handler = _Scripted([_status(503)])
    client = _client(
        handler,
        retry=RetryPolicy(max_attempts=3, initial_backoff_s=0.5, max_backoff_s=30.0),
    )
    try:
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert sleeps == [0.5, 1.0]


async def test_429_backoff_kwarg_is_capped_by_max_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _record_sleeps(monkeypatch)
    handler = _Scripted([_status(429, "Too Many Requests")])
    client = _client(
        handler,
        retry=RetryPolicy(max_attempts=3, initial_backoff_s=0.5, max_backoff_s=5.0),
        rate_limit_backoff_s=4.0,
    )
    try:
        with pytest.raises(UnusualWhalesRateLimitError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert sleeps == [4.0, 5.0]


async def test_429_counts_toward_breaker_failures() -> None:
    handler = _Scripted([_status(429, "Too Many Requests")])
    client = _client(handler, retry=_fast_retry(1), threshold=2)
    try:
        for _ in range(2):
            with pytest.raises(UnusualWhalesRateLimitError):
                await client.request_json("/api/x")
        with pytest.raises(CircuitBreakerOpenError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 2


# ---------------------------------------------------------------------------
# Daily-quota 429 — fail fast
# ---------------------------------------------------------------------------


async def test_daily_limit_429_fails_fast_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = _record_sleeps(monkeypatch)
    handler = _Scripted([_status(429, _DAILY_LIMIT_BODY)])
    client = _client(handler, retry=RetryPolicy(max_attempts=3))
    try:
        with pytest.raises(UnusualWhalesDailyLimitError) as info:
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 1
    assert sleeps == []
    assert isinstance(info.value, UnusualWhalesRateLimitError)
    assert "(HTTP 429): " + _DAILY_LIMIT_BODY in str(info.value)
    assert "daily_request_limit_hit" in str(info.value)


async def test_daily_limit_429_counts_once_toward_breaker() -> None:
    handler = _Scripted([_status(429, _DAILY_LIMIT_BODY)])
    client = _client(handler, retry=RetryPolicy(max_attempts=3), threshold=2)
    try:
        with pytest.raises(UnusualWhalesDailyLimitError):
            await client.request_json("/api/x")
        assert not client.circuit_breaker.is_open()  # one failure, threshold 2
        with pytest.raises(UnusualWhalesDailyLimitError):
            await client.request_json("/api/x")
        with pytest.raises(CircuitBreakerOpenError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 2


# ---------------------------------------------------------------------------
# Non-JSON 200 body
# ---------------------------------------------------------------------------


async def test_non_json_200_body_retried_as_transient() -> None:
    handler = _Scripted([
        lambda: httpx.Response(
            200, content=b"<html>cf</html>", headers={"content-type": "text/html"},
        ),
        _json(200, {"data": []}),
    ])
    client = _client(handler, retry=_fast_retry(2))
    try:
        result = await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert result == {"data": []}
    assert handler.calls == 2


async def test_empty_200_body_raises_transient_not_json_decode_error() -> None:
    handler = _Scripted([lambda: httpx.Response(200, content=b"")])
    client = _client(handler, retry=_fast_retry(2))
    try:
        with pytest.raises(UnusualWhalesTransientError, match="failed after 2 attempts") as info:
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 2
    assert isinstance(info.value, UnusualWhalesError)
    assert not isinstance(info.value, json.JSONDecodeError)
    assert "non-JSON body" in str(info.value)


async def test_non_json_200_does_not_reset_breaker_failures() -> None:
    """A garbage 200 is a failure, not a success: a 503 followed by an
    HTML 200 opens a threshold-2 breaker."""
    handler = _Scripted([
        _status(503),
        lambda: httpx.Response(200, content=b"<html>cf</html>"),
        _json(200, {"data": []}),
    ])
    client = _client(handler, retry=_fast_retry(1), threshold=2)
    try:
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
        with pytest.raises(CircuitBreakerOpenError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 2


# ---------------------------------------------------------------------------
# HTTP 404 / 422 — NotFound outside the breaker
# ---------------------------------------------------------------------------


_NOT_FOUND_CASES = [
    pytest.param(404, _BODY_404_UNKNOWN_TICKER, id="404-unknown-ticker"),
    pytest.param(404, _BODY_404_ROUTE, id="404-route"),
    pytest.param(422, _BODY_422_INVALID_INPUT, id="422-invalid-input"),
]


@pytest.mark.parametrize(("status", "body"), _NOT_FOUND_CASES)
async def test_404_422_raise_not_found_with_status_code_without_retry(
    status: int, body: str,
) -> None:
    handler = _Scripted([_status(status, body)])
    client = _client(handler, retry=RetryPolicy(max_attempts=3))
    try:
        with pytest.raises(UnusualWhalesNotFoundError) as info:
            await client.request_json("/api/stock/NOTATICKER/info")
    finally:
        await client.aclose()
    assert handler.calls == 1
    assert info.value.status_code == status
    assert isinstance(info.value, UnusualWhalesAuthError)
    assert f"HTTP {status}" in str(info.value)


@pytest.mark.parametrize(("status", "body"), _NOT_FOUND_CASES)
async def test_404_422_never_open_the_breaker(status: int, body: str) -> None:
    handler = _Scripted([_status(status, body)])
    client = _client(handler, retry=_fast_retry(1), threshold=2)
    try:
        for _ in range(5):
            with pytest.raises(UnusualWhalesNotFoundError):
                await client.request_json("/api/stock/NOTATICKER/info")
        assert not client.circuit_breaker.is_open()
    finally:
        await client.aclose()
    assert handler.calls == 5


@pytest.mark.parametrize(("status", "body"), _NOT_FOUND_CASES)
async def test_not_found_between_503s_does_not_reset_failure_count(
    status: int, body: str,
) -> None:
    """NotFound is neither a failure nor a success: 503, 404/422, 503
    opens a threshold-2 breaker."""
    handler = _Scripted([_status(503), _status(status, body), _status(503)])
    client = _client(handler, retry=_fast_retry(1), threshold=2)
    try:
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
        with pytest.raises(UnusualWhalesNotFoundError):
            await client.request_json("/api/x")
        assert not client.circuit_breaker.is_open()
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
        with pytest.raises(CircuitBreakerOpenError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert handler.calls == 3


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_other_4xx_stay_auth_error_not_not_found(status: int) -> None:
    """A bad key must never look like "no data"."""
    handler = _Scripted([_status(status, '{"code":"unrecognized_token"}')])
    client = _client(handler, retry=RetryPolicy(max_attempts=3), threshold=1)
    try:
        with pytest.raises(UnusualWhalesAuthError) as info:
            await client.request_json("/api/x")
        assert client.circuit_breaker.is_open()  # counted
    finally:
        await client.aclose()
    assert handler.calls == 1
    assert not isinstance(info.value, UnusualWhalesNotFoundError)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_new_error_classes_exported_from_package() -> None:
    import uoa_detector.sources.unusual_whales as uw

    assert uw.UnusualWhalesNotFoundError is UnusualWhalesNotFoundError
    assert uw.UnusualWhalesDailyLimitError is UnusualWhalesDailyLimitError
    assert "UnusualWhalesNotFoundError" in uw.__all__
    assert "UnusualWhalesDailyLimitError" in uw.__all__
    assert issubclass(UnusualWhalesNotFoundError, UnusualWhalesAuthError)
    assert issubclass(UnusualWhalesDailyLimitError, UnusualWhalesRateLimitError)
    assert not issubclass(UnusualWhalesRateLimitError, UnusualWhalesTransientError)
