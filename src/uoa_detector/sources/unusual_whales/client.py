"""Unusual Whales HTTP client.

Phase 3.3.3.2: the transport layer the live source (3.3.3.3) and
the six providers (3.3.3.4 + 3.3.3.5) build on. Implements:

  - Authenticated HTTP via UW's public API (https://api.unusualwhales.com).
  - Bearer-token auth (Authorization: Bearer <key>).
  - Token-bucket rate limiting (UnusualWhalesSettings.rate_limit_*).
  - Retry with exponential backoff on transient failures.
  - HTTP 429 retried with a longer backoff (Phase 3.9.3); a daily-quota
    429 fails fast.
  - HTTP 404/422 raised as ``UnusualWhalesNotFoundError`` outside the
    circuit breaker (Phase 3.9.3).
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

decision (response shape: object returned, top-level array wrapped):
  Most UW endpoints return a JSON object with a top-level ``data``
  key, and providers extract ``data`` themselves. Some endpoints
  return a top-level JSON array instead (live 2026-09-14:
  ``/api/stock/{ticker}/flow-recent`` returns the last 50 trades as a
  bare array, contrary to the vendored spec). The client wraps such
  an array as ``{"data": [...]}`` so every caller reads
  ``resp.get("data", [])`` the same way. A scalar or string at the top
  level is still a serialisation error (``UnusualWhalesTransientError``).
  So is a 200 whose body is not JSON at all (empty, or an HTML proxy
  page): it is retried and counts toward the breaker instead of
  escaping as a bare ``json.JSONDecodeError``.

decision (HTTP 429 is backpressure, not a hard 4xx):
  UW enforces a per-minute burst limit and a daily token quota
  (live headers: ``x-uw-token-req-limit``). A plain 429 raises
  ``UnusualWhalesRateLimitError``. It is retried with a backoff based
  on ``rate_limit_backoff_s`` rather than the transient base, and each
  attempt records a breaker failure so a sustained limit backs the
  whole client off. After the last attempt the typed
  ``UnusualWhalesRateLimitError`` is raised, not a transient error, so
  callers can tell a rate limit from an outage. A 429 whose body
  carries the ``daily_request_limit`` code raises
  ``UnusualWhalesDailyLimitError`` at once. Retrying it only spends
  quota. The message always embeds ``(HTTP 429): <body[:120]>`` so
  callers can match the vendor code in ``str(exc)``.

decision (HTTP 404/422 are input misses, outside the breaker):
  UW returns 404 for an unknown element and 422 for an invalid path
  input (for example an unknown ticker or a malformed OSI symbol).
  Neither says anything about service health. They raise
  ``UnusualWhalesNotFoundError`` (a subclass of
  ``UnusualWhalesAuthError``, so existing 4xx catches still work).
  They are not retried and are recorded on the breaker as neither
  a failure nor a success. Providers map them to their documented
  no-data return. 401/403 keep counting, because a bad key must never
  look like "no data".

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

# Substring of the vendor error code in a daily-quota 429 body
# (``daily_request_limit_hit``). Retrying such a 429 cannot succeed
# until the quota window rolls over.
_DAILY_LIMIT_MARKER = "daily_request_limit"

# HTTP statuses that mean "this request's input has no answer" rather
# than "the service is unhealthy": 404 unknown element, 422 invalid
# path input (live 2026-09-14: /api/stock/NOTATICKER/info -> 422).
_NOT_FOUND_STATUSES: frozenset[int] = frozenset({404, 422})


class UnusualWhalesError(RuntimeError):
    """Base exception for Unusual Whales client errors."""


class UnusualWhalesAuthError(UnusualWhalesError):
    """Authentication or authorisation failure (4xx)."""


class UnusualWhalesNotFoundError(UnusualWhalesAuthError):
    """HTTP 404 or 422: the requested element does not exist or the
    path input is invalid (unknown ticker, malformed option symbol).

    Not retried and not recorded on the circuit breaker (neither a
    failure nor a success), because it is an input miss, not a
    service-health signal. It subclasses ``UnusualWhalesAuthError``
    so existing 4xx catches keep working. Providers map it to their
    documented no-data return. ``status_code`` carries 404 or 422.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class UnusualWhalesRateLimitError(UnusualWhalesError):
    """HTTP 429: UW rejected the request for exceeding a rate limit.

    Retried inside ``request_json`` with exponential backoff based on
    the client's ``rate_limit_backoff_s``. Every attempt records a
    circuit-breaker failure (backpressure). This exception reaches
    the caller only after the last attempt, and its message embeds
    ``(HTTP 429): <response body[:120]>`` so the vendor error code
    stays visible in ``str(exc)``.
    """


class UnusualWhalesDailyLimitError(UnusualWhalesRateLimitError):
    """HTTP 429 whose body carries the ``daily_request_limit`` code.

    The daily token quota is spent, so a retry cannot succeed until
    the quota window rolls over. Raised on the first response with no
    retry, and recorded once as a circuit-breaker failure.
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
          response = await client.request_json("/api/stock/AAPL/info")
      finally:
          await client.aclose()

    ``rate_limit_backoff_s`` is the backoff base for HTTP 429 retries
    (``min(base * 2**attempt, retry.max_backoff_s)``). It is a
    transport tunable like the breaker settings, not a scoring
    threshold. Non-429 retries use ``retry.initial_backoff_s``.
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
        rate_limit_backoff_s: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._settings = settings
        self._base_url = base_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._rate_limit_backoff_s = rate_limit_backoff_s
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
        self._last_daily_request_count: int | None = None

    @property
    def settings(self) -> UnusualWhalesSettings:
        return self._settings

    @property
    def last_daily_request_count(self) -> int | None:
        """The last ``x-uw-daily-req-count`` header value seen, or ``None``.

        Phase 5.2.A2 (decision P17): read-only budget visibility for the
        Alfa Board refresher's soft cap. The value is captured from every
        HTTP response that carries a parseable, non-negative header, error
        responses included. The count is key-wide, so it also covers other
        processes using the same key. Reading it never changes request
        behaviour.
        """
        return self._last_daily_request_count

    def _capture_daily_count(self, response: httpx.Response) -> None:
        raw = response.headers.get("x-uw-daily-req-count")
        if raw is None:
            return
        try:
            value = int(raw.strip())
        except ValueError:
            return
        if value >= 0:
            self._last_daily_request_count = value

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

    @staticmethod
    def _parse_body(
        response: httpx.Response, *, method: str, path: str,
    ) -> dict[str, Any]:
        """Parse a 2xx body into a JSON object.

        A top-level array is wrapped as ``{"data": [...]}`` (live
        ``/flow-recent`` shape). A body that is not JSON (empty, HTML
        proxy page) or a JSON scalar raises
        ``UnusualWhalesTransientError``, which the caller retries.
        """
        try:
            parsed: Any = response.json()
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            msg = (
                f"UnusualWhales {method} {path} returned non-JSON body "
                f"(HTTP {response.status_code}): {response.text[:120]!r}"
            )
            raise UnusualWhalesTransientError(msg) from exc
        if isinstance(parsed, list):
            return {"data": parsed}
        if not isinstance(parsed, dict):
            msg = (
                f"UnusualWhales {method} {path} returned non-object "
                f"JSON: {type(parsed).__name__}"
            )
            raise UnusualWhalesTransientError(msg)
        return parsed

    # -- core request loop --------------------------------------------------

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        """Make an authenticated HTTP request and return parsed JSON.

        Applies rate limit, retry policy, and circuit breaker. A
        top-level JSON array is returned as ``{"data": [...]}``. Raises:
          - UnusualWhalesNotFoundError on 404/422 (no retry; NOT recorded
            on the breaker, neither failure nor success)
          - UnusualWhalesDailyLimitError on a daily-quota 429 (no retry;
            counts once toward the breaker)
          - UnusualWhalesRateLimitError on 429 after retries exhausted
            (each attempt counts toward the breaker)
          - UnusualWhalesAuthError on other 4xx (no retry; counts to breaker)
          - UnusualWhalesTransientError on 5xx, network error, or a 200
            whose body is not a JSON object/array, after retries exhausted
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
                self._capture_daily_count(response)
                status = response.status_code
                if status >= 500:
                    msg = f"UnusualWhales {method} {path} returned HTTP {status}"
                    raise UnusualWhalesTransientError(msg)
                if status == 429:
                    msg = (
                        f"UnusualWhales {method} {path} rate-limited "
                        f"(HTTP 429): {response.text[:120]}"
                    )
                    if _DAILY_LIMIT_MARKER in response.text:
                        raise UnusualWhalesDailyLimitError(msg)
                    raise UnusualWhalesRateLimitError(msg)
                if status >= 400:
                    msg = (
                        f"UnusualWhales {method} {path} returned "
                        f"HTTP {status}: {response.text[:200]}"
                    )
                    if status in _NOT_FOUND_STATUSES:
                        raise UnusualWhalesNotFoundError(msg, status_code=status)
                    raise UnusualWhalesAuthError(msg)
                result = self._parse_body(response, method=method, path=path)
                # Success is recorded only once the body parsed, so a 200
                # carrying garbage counts as a failure, not a reset.
                self._breaker.record_success()
                return result
            except (
                httpx.RequestError,
                UnusualWhalesTransientError,
                UnusualWhalesRateLimitError,
            ) as exc:
                self._breaker.record_failure()
                if isinstance(exc, UnusualWhalesDailyLimitError):
                    # Daily quota spent: a retry only burns more quota.
                    raise
                last_error = exc
                if attempt + 1 < self._retry.max_attempts:
                    # Rate limits get a longer cool-off than a network blip.
                    base = (
                        self._rate_limit_backoff_s
                        if isinstance(exc, UnusualWhalesRateLimitError)
                        else self._retry.initial_backoff_s
                    )
                    backoff = min(base * (2 ** attempt), self._retry.max_backoff_s)
                    await asyncio.sleep(backoff)
                continue
            except UnusualWhalesAuthError as exc:
                # 4xx — don't retry. 404/422 are input misses and leave the
                # breaker untouched; every other 4xx counts as a failure.
                if not isinstance(exc, UnusualWhalesNotFoundError):
                    self._breaker.record_failure()
                raise
        # Retries exhausted.
        if last_error is not None:
            msg = (
                f"UnusualWhales {method} {path} failed after "
                f"{self._retry.max_attempts} attempts: {last_error}"
            )
            if isinstance(last_error, UnusualWhalesRateLimitError):
                raise UnusualWhalesRateLimitError(msg)
            raise UnusualWhalesTransientError(msg)
        # Unreachable: loop body either returns or raises.
        msg = "unreachable"
        raise RuntimeError(msg)
