"""Phase 3.3.3.2 tests for ``unusual_whales.client``.

All tests use httpx.MockTransport — no real network. The smoke
integration tests (3.3.3.6) are the only thing that need a real
UNUSUAL_WHALES_API_KEY.

Pins (mirrored from test_thetadata_client.py — same shared
infrastructure, same semantics; only differences captured below):
  - Authorization header uses Bearer scheme (UW-specific)
  - X-Api-Key header is NOT used (would be a regression toward
    ThetaData's pattern; explicit negative pin)
  - Default base URL is the public HTTPS endpoint
  - Rate-limit default reflects UW API-Plus tier (2 req/s)
  - SecretStr key never leaks to error messages or repr
  - Circuit breaker open raises CircuitBreakerOpenError pre-flight
  - 5xx retries; 4xx raises immediately (and counts to breaker)
  - Lazy initialisation: importing the module does NOT open
    network/auth (cross-cutting acceptance)
"""

from __future__ import annotations

import asyncio
import time as time_module
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.sources._http_base import (
    CircuitBreaker,
    RetryPolicy,
    TokenBucket,
)
from uoa_detector.sources.unusual_whales.client import (
    DEFAULT_BASE_URL,
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesError,
    UnusualWhalesTransientError,
)


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings(
        rate_limit_requests_per_second=10.0,  # bumped for tests, real default 2.0
        historical_concurrency=2,
        live_reconnect_max_attempts=5,
        live_reconnect_initial_backoff_s=1.0,
        live_reconnect_max_backoff_s=60.0,
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


def _ok_handler(payload: dict[str, Any]) -> Any:
    """Build a MockTransport handler that returns 200 + payload."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return handler


# ---------------------------------------------------------------------------
# Defaults + URL handling
# ---------------------------------------------------------------------------


def test_default_base_url_is_public_https() -> None:
    """UW base URL is HTTPS; ThetaData's was http://127.0.0.1."""
    assert DEFAULT_BASE_URL.startswith("https://")
    assert "unusualwhales" in DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# Auth header — Bearer scheme (UW-specific)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_uses_bearer_authorization_header() -> None:
    """UW auth is Bearer; this is the UW-specific pin."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Capture the Authorization header from the actual request
        auth = request.headers.get("Authorization", "")
        captured["auth"] = auth
        captured["x_api_key"] = request.headers.get("X-Api-Key", "")
        return httpx.Response(200, json={"data": []})

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test_key_value"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.request_json("/api/option-flow/recent")
    finally:
        await client.aclose()
    assert captured["auth"] == "Bearer uw_test_key_value"
    # Negative pin: X-Api-Key MUST NOT be present (that's ThetaData's scheme)
    assert captured["x_api_key"] == ""


@pytest.mark.asyncio
async def test_request_sends_accept_json_header() -> None:
    """UW expects an Accept header for JSON."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("Accept", "")
        return httpx.Response(200, json={"data": []})

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert captured["accept"] == "application/json"


def test_secret_not_in_repr() -> None:
    """Constructor doesn't leak the API key via repr."""
    client = UnusualWhalesClient(
        api_key=SecretStr("uw_super_secret_key_xyz"),
        settings=_settings(),
        transport=httpx.MockTransport(_ok_handler({})),
    )
    text = repr(client)
    assert "uw_super_secret_key_xyz" not in text


# ---------------------------------------------------------------------------
# Successful request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_json_returns_parsed_dict() -> None:
    """Happy-path: 200 + JSON object → parsed dict."""
    payload = {"data": [{"ticker": "AAPL", "premium": 12345}]}
    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        transport=httpx.MockTransport(_ok_handler(payload)),
    )
    try:
        result = await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert result == payload


@pytest.mark.asyncio
async def test_top_level_array_response_auto_wrapped() -> None:
    """Phase 3.3.9.6: UW endpoints like /flow-recent return a top-level
    JSON array; client auto-wraps to ``{"data": [...]}`` so existing
    ``resp.get("data", [])`` callers keep working.
    """
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"a": 1}, {"a": 2}])

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(max_attempts=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert result == {"data": [{"a": 1}, {"a": 2}]}


@pytest.mark.asyncio
async def test_non_dict_non_list_response_raises_transient() -> None:
    """A scalar / string / number response is still a serialisation bug."""
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json="garbage_string")

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(max_attempts=1),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UnusualWhalesTransientError, match="non-object"):
            await client.request_json("/api/x")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 4xx — auth error, no retry, breaker counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4xx_raises_auth_error_without_retry() -> None:
    call_count = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, text="Unauthorized")

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(max_attempts=3),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UnusualWhalesAuthError, match="401"):
            await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert call_count == 1  # no retry on 4xx


@pytest.mark.asyncio
async def test_4xx_counts_toward_breaker_failures() -> None:
    """A flood of 4xx (rotated keys, etc.) trips the breaker."""
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(max_attempts=1),
        circuit_breaker_threshold=3,
        circuit_breaker_reset_s=30.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        for _ in range(3):
            with pytest.raises(UnusualWhalesAuthError):
                await client.request_json("/api/x")
        # Now the breaker should be open
        with pytest.raises(CircuitBreakerOpenError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 5xx — transient, retry with backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    """First call 503, second 200 → returns success."""
    call_count = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"data": "ok"})

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(
            max_attempts=3,
            initial_backoff_s=0.001,  # fast for tests
            max_backoff_s=0.01,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert call_count == 2
    assert result == {"data": "ok"}


@pytest.mark.asyncio
async def test_5xx_exhausts_retries_then_raises_transient() -> None:
    """All attempts fail → UnusualWhalesTransientError."""
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(
            max_attempts=2, initial_backoff_s=0.001, max_backoff_s=0.01,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_successful_response_after_retry_resets_breaker_failures() -> None:
    """A 5xx-then-200 sequence does NOT leave breaker failures lingering."""
    call_count = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": []})

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(
            max_attempts=2, initial_backoff_s=0.001, max_backoff_s=0.01,
        ),
        circuit_breaker_threshold=2,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.request_json("/api/x")
        # Internal: the failure count must be reset; otherwise the next
        # 5xx would trip the breaker. Verify by inspecting the breaker.
        breaker = client.circuit_breaker
        assert not breaker.is_open()
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Circuit breaker pre-flight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_circuit_breaker_raises_pre_flight() -> None:
    """Once tripped, requests are refused without hitting the network."""
    network_calls = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(
            max_attempts=1, initial_backoff_s=0.001, max_backoff_s=0.01,
        ),
        circuit_breaker_threshold=2,
        circuit_breaker_reset_s=60.0,  # long enough that we won't probe
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
        with pytest.raises(UnusualWhalesTransientError):
            await client.request_json("/api/x")
        # Breaker now open
        before_calls = network_calls
        with pytest.raises(CircuitBreakerOpenError):
            await client.request_json("/api/x")
        # No additional network call when breaker open
        assert network_calls == before_calls
    finally:
        await client.aclose()


def test_circuit_breaker_open_error_inherits_unusual_whales_error() -> None:
    """``except UnusualWhalesError`` should catch CircuitBreakerOpenError."""
    e = CircuitBreakerOpenError("test")
    assert isinstance(e, UnusualWhalesError)


# ---------------------------------------------------------------------------
# Rate limit pacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_paces_requests() -> None:
    """Three requests at 10 req/s take >= ~150ms total (2 refills)."""
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=UnusualWhalesSettings(rate_limit_requests_per_second=10.0),
        transport=httpx.MockTransport(handler),
    )
    try:
        # Warm-up: drain the bucket
        for _ in range(int(10.0)):  # capacity == 10
            await client.request_json("/api/x")
        # Now three more requests at 10/s → ~100ms gaps
        start = time_module.monotonic()
        for _ in range(3):
            await client.request_json("/api/x")
        elapsed = time_module.monotonic() - start
        assert elapsed >= 0.15
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Lazy init — importing the module doesn't trigger network or auth
# ---------------------------------------------------------------------------


def test_importing_client_module_does_not_open_connections() -> None:
    """Cross-cutting acceptance: 'Every adapter has lazy initialization:
    importing the module does NOT trigger network or auth; instantiation does.'
    """
    import uoa_detector.sources.unusual_whales.client as cli_mod

    public_names = [n for n in dir(cli_mod) if not n.startswith("_")]
    assert "UnusualWhalesClient" in public_names
    assert "UnusualWhalesError" in public_names
    assert not hasattr(cli_mod, "_default_client")
    assert not hasattr(cli_mod, "default_client")


# ---------------------------------------------------------------------------
# Shared base — TokenBucket, CircuitBreaker, RetryPolicy
# ---------------------------------------------------------------------------


def test_shared_base_token_bucket_tries_acquire() -> None:
    """Verifying the extracted shared base works; consolidates the
    multi-vendor surface in one place."""
    bucket = TokenBucket(capacity=2.0, refill_per_second=10.0)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False  # empty


def test_shared_base_circuit_breaker_trips_after_threshold() -> None:
    breaker = CircuitBreaker(threshold=2, reset_seconds=30.0)
    assert not breaker.is_open()
    breaker.record_failure()
    assert not breaker.is_open()
    breaker.record_failure()
    assert breaker.is_open()
    breaker.record_success()
    assert not breaker.is_open()


def test_shared_base_retry_policy_defaults() -> None:
    rp = RetryPolicy()
    assert rp.max_attempts == 3
    assert rp.initial_backoff_s == 0.5
    assert rp.max_backoff_s == 30.0


# ---------------------------------------------------------------------------
# Vendor isolation — UW errors don't leak ThetaData types
# ---------------------------------------------------------------------------


def test_uw_error_does_not_inherit_thetadata_error() -> None:
    """Vendor errors stay vendor-isolated. Cross-catching by intent."""
    from uoa_detector.sources.thetadata.client import ThetaDataError

    e = UnusualWhalesError("oops")
    assert not isinstance(e, ThetaDataError)


# ---------------------------------------------------------------------------
# aclose cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_underlying_httpx_client() -> None:
    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        transport=httpx.MockTransport(_ok_handler({})),
    )
    await client.aclose()
    # A second aclose should be safe (httpx allows double-close)
    await client.aclose()


# ---------------------------------------------------------------------------
# Network error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_treated_as_transient_and_retried() -> None:
    """httpx.ConnectError → transient → retry."""
    call_count = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json={"data": "ok"})

    client = UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(
            max_attempts=2, initial_backoff_s=0.001, max_backoff_s=0.01,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.request_json("/api/x")
    finally:
        await client.aclose()
    assert call_count == 2
    assert result == {"data": "ok"}


# Mark unused imports as referenced (asyncio used inside functions only)
_ = asyncio
