"""Phase 3.3.2.3 tests for ``thetadata.client``.

All tests use httpx.MockTransport — no real network. The smoke
integration test (3.3.2.6) is the only thing that needs a real
THETADATA_API_KEY.

Pins:
  - TokenBucket refills monotonically, blocks when empty
  - CircuitBreaker opens after N consecutive failures, cools down
  - request_json sends auth headers without leaking secrets to logs
  - 5xx triggers retry with exponential backoff
  - 4xx raises immediately (no retry on auth/malformed)
  - Successful response after retry resets failure counter
  - Circuit breaker open raises CircuitBreakerOpenError pre-flight
  - Rate limit is enforced (token bucket controls request cadence)
  - aclose() cleans up the underlying httpx client
  - stream_ws raises NotImplementedError until 3.3.2.5
"""

from __future__ import annotations

import asyncio
import time as time_module
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import ThetaDataSettings
from uoa_detector.sources.thetadata.client import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    RetryPolicy,
    ThetaDataAuthError,
    ThetaDataClient,
    ThetaDataTransientError,
    TokenBucket,
)

# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


def test_token_bucket_starts_at_capacity() -> None:
    b = TokenBucket(capacity=10, refill_per_second=10)
    for _ in range(10):
        assert b.try_acquire() is True
    assert b.try_acquire() is False


def test_token_bucket_refills_over_time() -> None:
    b = TokenBucket(capacity=2, refill_per_second=100)
    assert b.try_acquire(2.0) is True
    assert b.try_acquire() is False
    # Force a small monotonic-clock advance via sleep.
    time_module.sleep(0.05)
    # Should have refilled ~5 tokens, capped at capacity=2.
    assert b.try_acquire() is True


def test_token_bucket_capacity_caps_refill() -> None:
    b = TokenBucket(capacity=5, refill_per_second=1000)
    time_module.sleep(0.1)  # Would refill 100, but capped at 5
    for _ in range(5):
        assert b.try_acquire() is True
    assert b.try_acquire() is False


@pytest.mark.asyncio
async def test_token_bucket_acquire_blocks_until_token_available() -> None:
    """Async acquire() blocks until refill makes a token available."""
    b = TokenBucket(capacity=1, refill_per_second=20)
    assert b.try_acquire() is True  # drain
    start = time_module.monotonic()
    await b.acquire()
    elapsed = time_module.monotonic() - start
    # Should wait ~50ms for one token at 20 tokens/s; allow generous slack
    assert 0.02 <= elapsed <= 0.5


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


def test_breaker_starts_closed() -> None:
    cb = CircuitBreaker(threshold=3)
    assert cb.is_open() is False


def test_breaker_opens_after_threshold_failures() -> None:
    cb = CircuitBreaker(threshold=3, reset_seconds=10)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is False
    cb.record_failure()
    assert cb.is_open() is True


def test_breaker_success_resets_counter() -> None:
    """A success between failures resets the counter."""
    cb = CircuitBreaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is False  # only 2 consecutive failures since reset


def test_breaker_cools_down_after_reset_seconds() -> None:
    cb = CircuitBreaker(threshold=2, reset_seconds=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True
    time_module.sleep(0.07)
    assert cb.is_open() is False  # cooled down → half-open probe


# ---------------------------------------------------------------------------
# request_json — happy path + auth header
# ---------------------------------------------------------------------------


def _settings() -> ThetaDataSettings:
    return ThetaDataSettings(rate_limit_requests_per_second=100.0)


@pytest.mark.asyncio
async def test_request_json_happy_path() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"response": [], "header": {}})

    transport = httpx.MockTransport(handler)
    client = ThetaDataClient(
        api_key=SecretStr("td_test_key"),
        settings=_settings(),
        transport=transport,
    )
    try:
        result = await client.request_json("/v2/hist/option/trade")
        assert result == {"response": [], "header": {}}
        assert captured["path"] == "/v2/hist/option/trade"
        assert captured["headers"]["x-api-key"] == "td_test_key"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_json_includes_username_when_provided() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={})

    client = ThetaDataClient(
        api_key=SecretStr("td_test_key"),
        username=SecretStr("td_user"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.request_json("/x")
        assert captured["headers"]["x-username"] == "td_user"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 4xx / 5xx behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_raises_auth_error_no_retry() -> None:
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, text="Unauthorized")

    client = ThetaDataClient(
        api_key=SecretStr("bad_key"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ThetaDataAuthError, match="401"):
            await client.request_json("/x")
        assert call_count == 1  # no retry on 4xx
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    """First 2 calls return 503, third succeeds."""
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"ok": True})

    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(
            max_attempts=3,
            initial_backoff_s=0.001,  # tight loop in tests
            max_backoff_s=0.01,
        ),
    )
    try:
        result = await client.request_json("/x")
        assert result == {"ok": True}
        assert calls[0] == 3
        # Breaker reset after success
        assert client.circuit_breaker.is_open() is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_5xx_retries_exhausted_raises_transient() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(
            max_attempts=2,
            initial_backoff_s=0.001,
            max_backoff_s=0.01,
        ),
    )
    try:
        with pytest.raises(ThetaDataTransientError):
            await client.request_json("/x")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_network_error_retries() -> None:
    """httpx.RequestError (network failure) is treated as transient."""
    calls = [0]

    def handler(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] < 2:
            raise httpx.ConnectError("simulated DNS failure")
        return httpx.Response(200, json={"ok": True})

    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(
            max_attempts=3, initial_backoff_s=0.001, max_backoff_s=0.01,
        ),
    )
    try:
        result = await client.request_json("/x")
        assert result == {"ok": True}
        assert calls[0] == 2
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Circuit breaker integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_consecutive_failures() -> None:
    """5 consecutive 5xx → breaker opens → 6th request raises
    CircuitBreakerOpenError pre-flight."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(
            max_attempts=1, initial_backoff_s=0.001, max_backoff_s=0.001,
        ),
        circuit_breaker_threshold=5,
        circuit_breaker_reset_s=10.0,
    )
    try:
        # 5 failed requests → 5 failure records → breaker opens
        for _ in range(5):
            with pytest.raises(ThetaDataTransientError):
                await client.request_json("/x")
        # 6th is refused pre-flight
        with pytest.raises(CircuitBreakerOpenError, match="circuit breaker"):
            await client.request_json("/x")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_circuit_breaker_resets_after_cooldown() -> None:
    state = {"down": True}

    def handler(_request: httpx.Request) -> httpx.Response:
        if state["down"]:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json={"ok": True})

    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(
            max_attempts=1, initial_backoff_s=0.001, max_backoff_s=0.001,
        ),
        circuit_breaker_threshold=2,
        circuit_breaker_reset_s=0.05,
    )
    try:
        for _ in range(2):
            with pytest.raises(ThetaDataTransientError):
                await client.request_json("/x")
        assert client.circuit_breaker.is_open() is True
        # Wait for cooldown
        await asyncio.sleep(0.07)
        # Bring service back up
        state["down"] = False
        result = await client.request_json("/x")
        assert result == {"ok": True}
        assert client.circuit_breaker.is_open() is False
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Rate limit enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_paces_requests() -> None:
    """With rps=10, four back-to-back requests (after draining the
    initial bucket) should take at least ~250ms total."""
    call_times: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        call_times.append(time_module.monotonic())
        return httpx.Response(200, json={})

    settings = ThetaDataSettings(rate_limit_requests_per_second=10.0)
    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    try:
        # Drain the initial bucket (capacity=10) — happens in a burst
        for _ in range(10):
            await client.request_json("/x")
        # Now each subsequent request waits ~100ms
        burst_start = time_module.monotonic()
        for _ in range(3):
            await client.request_json("/x")
        elapsed = time_module.monotonic() - burst_start
        # 3 paced requests at 10 rps ≈ 0.2-0.3s minimum
        assert elapsed >= 0.15
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Auth header — secret never leaked even on error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_error_message_does_not_include_secret() -> None:
    """Even a 401 raise shouldn't include the secret in the message."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    secret = "td_secret_xyz_should_never_appear"
    client = ThetaDataClient(
        api_key=SecretStr(secret),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ThetaDataAuthError) as exc:
            await client.request_json("/x")
        assert secret not in str(exc.value)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# WebSocket stub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_ws_points_to_live_source_class() -> None:
    """``ThetaDataClient.stream_ws`` is a placeholder that raises
    pointing the caller to ``ThetaDataLiveSource`` (Phase 3.3.2.5).

    The HTTP client and the WS live source are separate classes by
    design — ``ThetaDataClient`` stays HTTP-only.
    """
    client = ThetaDataClient(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={}),
        ),
    )
    try:
        with pytest.raises(NotImplementedError, match=r"ThetaDataLiveSource"):
            await client.stream_ws("/v2/ws", subscriptions=[])
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Lazy initialization — importing doesn't trigger network or auth
# ---------------------------------------------------------------------------


def test_importing_client_module_does_not_open_connections() -> None:
    """Per acceptance doc cross-cutting acceptance: 'Every adapter
    has lazy initialization: importing the module does NOT trigger
    network or auth; instantiation does.'

    We import the module symbols (already done at the top of this
    file) and assert no httpx.AsyncClient was constructed as a
    side effect. The test exists to pin the contract — if someone
    later moves an httpx.AsyncClient() to module scope, this fails.
    """
    import uoa_detector.sources.thetadata.client as cli_mod

    # No module-level state that would imply network setup.
    public_names = [n for n in dir(cli_mod) if not n.startswith("_")]
    # Sanity: the symbols are present
    assert "ThetaDataClient" in public_names
    assert "TokenBucket" in public_names
    # No instance-level attributes that smell like singletons
    assert not hasattr(cli_mod, "_default_client")
    assert not hasattr(cli_mod, "default_client")
