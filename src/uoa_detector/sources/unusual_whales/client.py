"""Unusual Whales HTTP client.

Phase 3.3.3.2: the transport layer the live source (3.3.3.3) and
the six providers (3.3.3.4 + 3.3.3.5) build on. Implements:

  - Authenticated HTTP via UW's public API (https://api.unusualwhales.com).
  - Bearer-token auth (Authorization: Bearer <key>).
  - Token-bucket rate limiting (UnusualWhalesSettings.rate_limit_*).
  - Retry with exponential backoff on transient failures.
  - Circuit breaker that trips after N consecutive failures.

Unlike ThetaData (which routes through a local Terminal proxy), UW
goes directly to their public endpoint over TLS. This means:
  - Real network in production; the API key travels in the
    Authorization header on every request.
  - Tests use httpx.MockTransport to swap out network entirely.
  - Smoke integration tests (3.3.3.6) hit the real endpoint and
    are skipped without UNUSUAL_WHALES_API_KEY.

The shared TokenBucket / CircuitBreaker / RetryPolicy primitives
are imported from ``sources/_http_base.py`` (extracted in Phase
3.3.3.2 from ``thetadata/client.py``). UW-specific exceptions live
here.

decision (Bearer auth, not X-Api-Key):
  UW's documented auth scheme is ``Authorization: Bearer <key>``,
  matching their REST conventions. ThetaData uses X-Api-Key. Same
  header-injection pattern (``_auth_headers()`` is called only at
  request time, never logged) but different header name.

decision (HTTPS direct, no local proxy):
  UW does not provide a local Terminal-style proxy. The API key
  goes over the wire, protected by TLS. The redact_secrets log
  processor (Phase 3.3.1.2) ensures the key never appears in any
  emitted log line.

decision (response shape: dict expected, list-wrapped tolerated):
  Most UW endpoints return JSON objects with a top-level ``data``
  key. We require dict at the top level and let providers extract
  ``data`` themselves; this lets the client stay vendor-neutral
  in shape while still rejecting bare-list responses (which would
  hint at a serialisation bug). ThetaData's client makes the same
  call and we keep the contract consistent across vendors.

decision (no built-in cache here):
  Caching is per-provider-type (UnusualWhalesProviderCacheTTL)
  and lives in the provider classes (Phase 3.3.3.4-5). The client
  itself stays cache-free so that calls bypassing the providers
  (e.g. live source subscribe) don't accidentally hit a stale entry.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from uoa_detector.sources._http_base import (
    CircuitBreaker,
    RetryPolicy,
    TokenBucket,
)
from uoa_detector.sources._http_base import (
    CircuitBreakerOpenError as _BaseCircuitBreakerOpenError,
)

if TYPE_CHECKING:
    from pydantic import SecretStr

    from uoa_detector.calibration.profile import UnusualWhalesSettings


# UW public API — operator can override via the ``base_url`` kwarg
# if they're behind a corporate proxy or pinning a sandbox.
DEFAULT_BASE_URL = "https://api.unusualwhales.com"


class UnusualWhalesError(RuntimeError):
    """Base exception for Unusual Whales client errors."""


class UnusualWhalesAuthError(UnusualWhalesError):
    """Authentication or authorisation failure (4xx)."""


class UnusualWhalesRateLimitError(UnusualWhalesError):
    """The configured token bucket would be exceeded.

    Raised when a request is attempted while the bucket is empty
    AND the client is configured to fail-fast on rate-limit rather
    than wait. Default behaviour is to wait; this exception exists
    for tests and for callers who want hard limits.
    """


class UnusualWhalesTransientError(UnusualWhalesError):
    """Retryable failure: 5xx, network error, timeout."""


class CircuitBreakerOpenError(_BaseCircuitBreakerOpenError, UnusualWhalesError):
    """The circuit breaker is open; refusing requests until reset.

    Inherits both the vendor-neutral base (so shared code can raise
    a single class) and ``UnusualWhalesError`` (so callers can
    ``except UnusualWhalesError`` and catch it).
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class UnusualWhalesClient:
    """HTTP client for the Unusual Whales API.

    Constructor injection of credentials + settings. The client is
    stateful (token bucket, circuit breaker, http client) and should
    be a singleton-per-process — one client serves the live source
    and all six providers.

    Lifecycle:
      client = UnusualWhalesClient(api_key=..., settings=...)
      try:
          response = await client.request_json("/api/option-flow/...")
      finally:
          await client.aclose()
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        settings: UnusualWhalesSettings,
        base_url: str = DEFAULT_BASE_URL,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_reset_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._settings = settings
        self._base_url = base_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._bucket = TokenBucket(
            capacity=max(settings.rate_limit_requests_per_second, 1.0),
            refill_per_second=settings.rate_limit_requests_per_second,
        )
        self._breaker = CircuitBreaker(
            threshold=circuit_breaker_threshold,
            reset_seconds=circuit_breaker_reset_s,
        )
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            transport=transport,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
        )

    @property
    def settings(self) -> UnusualWhalesSettings:
        return self._settings

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- request building ---------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers. Called only at the request site —
        never logged, never returned to callers.

        UW uses the standard ``Authorization: Bearer <key>`` scheme.
        """
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Accept": "application/json",
        }

    # -- core request loop --------------------------------------------------

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        """Make an authenticated HTTP request and return parsed JSON.

        Applies rate limit, retry policy, and circuit breaker. Raises:
          - UnusualWhalesAuthError on 4xx (no retry; counts to breaker)
          - UnusualWhalesTransientError on 5xx after retries exhausted
          - CircuitBreakerOpenError if breaker is open
        """
        if self._breaker.is_open():
            msg = (
                f"circuit breaker is open after {self._breaker.threshold}+ "
                f"consecutive failures; cooldown {self._breaker.reset_seconds}s"
            )
            raise CircuitBreakerOpenError(msg)

        last_error: Exception | None = None
        for attempt in range(self._retry.max_attempts):
            await self._bucket.acquire()
            try:
                response = await self._http.request(
                    method, path,
                    params=params,
                    headers=self._auth_headers(),
                )
                if response.status_code >= 500:
                    msg = (
                        f"UnusualWhales {method} {path} returned "
                        f"HTTP {response.status_code}"
                    )
                    raise UnusualWhalesTransientError(msg)
                if response.status_code >= 400:
                    msg = (
                        f"UnusualWhales {method} {path} returned "
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                    raise UnusualWhalesAuthError(msg)
                self._breaker.record_success()
                parsed: Any = response.json()
                # Phase 3.3.9.6: a handful of UW endpoints (notably
                # /api/stock/{t}/flow-recent) return a top-level JSON
                # array rather than an object. Auto-wrap so existing
                # ``resp.get("data", [])`` callers keep working.
                if isinstance(parsed, list):
                    return {"data": parsed}
                if not isinstance(parsed, dict):
                    msg = (
                        f"UnusualWhales {method} {path} returned non-object "
                        f"JSON: {type(parsed).__name__}"
                    )
                    raise UnusualWhalesTransientError(msg)
                return parsed
            except (httpx.RequestError, UnusualWhalesTransientError) as exc:
                last_error = exc
                self._breaker.record_failure()
                if attempt + 1 < self._retry.max_attempts:
                    backoff = min(
                        self._retry.initial_backoff_s * (2 ** attempt),
                        self._retry.max_backoff_s,
                    )
                    await asyncio.sleep(backoff)
                continue
            except UnusualWhalesAuthError:
                # 4xx — don't retry, but record as failure for the breaker.
                self._breaker.record_failure()
                raise
        # Retries exhausted.
        if last_error is not None:
            msg = (
                f"UnusualWhales {method} {path} failed after "
                f"{self._retry.max_attempts} attempts: {last_error}"
            )
            raise UnusualWhalesTransientError(msg)
        # Unreachable: loop body either returns or raises.
        msg = "unreachable"
        raise RuntimeError(msg)
