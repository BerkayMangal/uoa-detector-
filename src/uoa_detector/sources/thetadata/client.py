"""ThetaData HTTP / WebSocket client.

Phase 3.3.2.3: the transport layer the historical downloader
(3.3.2.4) and live source (3.3.2.5) build on. Implements:

  - Authenticated HTTP via the local Theta Terminal proxy.
    The Terminal listens on http://127.0.0.1:25510 by default
    (per ThetaData docs); credentials are forwarded by the
    Terminal, not sent over the public network. Our client still
    accepts ``Credentials`` so different Terminal configs (a
    cloud-hosted Terminal in the future, etc.) can be plumbed
    without changing call sites.
  - Token-bucket rate limiting (ThetaDataSettings.rate_limit_*)
  - Retry with exponential backoff on transient failures
  - Circuit breaker that trips after N consecutive failures
  - WebSocket subscribe/iterate with the same auth + retry loop

This module ships the **skeleton + transport contract**. The
endpoint-specific helpers (trade fetch, quote fetch) are added in
3.3.2.4. Live WebSocket framing is added in 3.3.2.5.

Tests use a mocked transport (httpx.MockTransport / a fake WS
server) so the entire surface is exercised without real network
traffic. Smoke integration tests (3.3.2.6) hit the real Terminal
and are skipped without ``THETADATA_API_KEY``.

decision (transport contract via httpx.AsyncClient):
  httpx already supports MockTransport for in-process testing,
  has a clean async API, and is widely used. ThetaData publishes
  a Python SDK but adopting it would couple us to their version
  cadence; httpx + our own mapping layer keeps the dependency
  surface minimal and testable.

decision (lazy auth, never log secrets):
  The client takes a ``SecretStr`` for the API key and calls
  ``.get_secret_value()`` only when constructing the request
  headers. structlog redact_secrets (Phase 3.3.1.2) catches any
  accidental leak.

decision (rate limit = token bucket, profile-tunable):
  Acceptance doc names a token bucket. We implement it as a
  monotonically-replenishing counter — simpler than a deque-based
  sliding window, sufficient for the rps shape ThetaData
  publishes. ``rate_limit_requests_per_second`` from
  ThetaDataSettings drives the refill rate.

decision (retry policy: exponential backoff, capped attempts):
  Standard pattern: wait ``initial * 2^attempt`` seconds, capped
  at ``max_backoff_s``. Three attempts (default) before raising.
  Retryable errors: 5xx, network errors, timeouts. 4xx never
  retries (auth, malformed request).

decision (circuit breaker: trip after N consecutive failures):
  After ``circuit_breaker_threshold`` consecutive failures
  (default 5), the client refuses requests for
  ``circuit_breaker_reset_s`` (default 30s) before allowing one
  probe. This prevents thundering-herd retries when ThetaData is
  hard-down.

decision (no built-in connection pooling beyond httpx default):
  httpx.AsyncClient pools connections by default. We don't expose
  pool tunables in profile config — they're a deeper concern that
  the operator can override by passing a constructed
  AsyncClient if needed.
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
    from collections.abc import Iterable

    from pydantic import SecretStr

    from uoa_detector.calibration.profile import ThetaDataSettings


# Default Theta Terminal location — operator can override via the
# ``base_url`` constructor kwarg if running the Terminal on a
# non-default port or a different host.
DEFAULT_BASE_URL = "http://127.0.0.1:25503"  # Phase 3.3.7.3: v3 default


class ThetaDataError(RuntimeError):
    """Base exception for ThetaData client errors."""


class ThetaDataAuthError(ThetaDataError):
    """Authentication or authorisation failure (4xx)."""


class ThetaDataRateLimitError(ThetaDataError):
    """The configured token bucket would be exceeded.

    Raised when a request is attempted while the bucket is empty
    AND the client is configured to fail-fast on rate-limit
    rather than wait. Default behaviour is to wait; this exception
    exists for tests and for callers who want hard limits.
    """


class ThetaDataTransientError(ThetaDataError):
    """Retryable failure: 5xx, network error, timeout."""


class CircuitBreakerOpenError(_BaseCircuitBreakerOpenError, ThetaDataError):
    """The circuit breaker is open; refusing requests until reset.

    Inherits from both the vendor-neutral base (raised by
    CircuitBreaker itself in shared code) and ThetaDataError (so
    existing ``except ThetaDataError`` blocks still catch it). This
    dual inheritance is the back-compat seam introduced when
    Phase 3.3.3.2 extracted the breaker into a shared module.
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ThetaDataClient:
    """HTTP / WebSocket client for the ThetaData Terminal.

    Constructor injection of credentials + settings. The client is
    stateful (token bucket, circuit breaker, http client) and
    should be a singleton-per-process — one client serves the
    historical downloader and the live source.

    Lifecycle:
      client = ThetaDataClient(creds, settings)
      try:
          response = await client.request_json("/v2/hist/option/trade", params={...})
      finally:
          await client.aclose()
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        username: SecretStr | None = None,
        settings: ThetaDataSettings,
        base_url: str = DEFAULT_BASE_URL,
        retry: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_reset_s: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._username = username
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
    def settings(self) -> ThetaDataSettings:
        return self._settings

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- request building ---------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers. Called only at the request site —
        never logged, never returned."""
        # ThetaData's local Terminal forwards credentials it was
        # configured with; for direct upstream calls we'd send the
        # API key. The header name is standard X-Api-Key style; if
        # ThetaData docs specify a different name we update here
        # without changing call sites.
        headers = {"X-Api-Key": self._api_key.get_secret_value()}
        if self._username is not None:
            headers["X-Username"] = self._username.get_secret_value()
        return headers

    # -- core request loop --------------------------------------------------

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> Any:
        """Make an authenticated HTTP request and return parsed JSON.

        Phase 3.3.7.3: relaxed return type from ``dict`` to ``Any``.
        v3 endpoints return JSON arrays-of-objects (trade, quote,
        ohlc, list/symbols), not the v2 ``{header, response}``
        dict shape. Caller checks response shape per-endpoint.

        Phase 3.3.7.3: also auto-injects ``format=json`` into params
        if absent. v3 default response format is CSV; without this
        injection callers would receive un-parseable bytes. The
        injection is silent and idempotent (caller-supplied
        ``format`` value is preserved).

        Applies rate limit, retry policy, and circuit breaker. Raises
        ``ThetaDataAuthError`` on 4xx, ``ThetaDataTransientError`` on
        5xx after retries exhausted, ``CircuitBreakerOpenError`` if
        the breaker is open.
        """
        if self._breaker.is_open():
            msg = (
                f"circuit breaker is open after {self._breaker.threshold}+ "
                f"consecutive failures; cooldown {self._breaker.reset_seconds}s"
            )
            raise CircuitBreakerOpenError(msg)

        # v3 always-on format=json (Phase 3.3.7.3 J5).
        effective_params: dict[str, Any] = dict(params or {})
        effective_params.setdefault("format", "json")

        last_error: Exception | None = None
        for attempt in range(self._retry.max_attempts):
            await self._bucket.acquire()
            try:
                response = await self._http.request(
                    method, path,
                    params=effective_params,
                    headers=self._auth_headers(),
                )
                # Phase 3.3.12: ThetaData v3 uses HTTP 472 as a
                # custom "no data found for this query" code. It is
                # NOT an auth or transient error — many legitimate
                # downloads hit this on contracts with no trading
                # activity for the requested day. Treat as an empty
                # success.
                if response.status_code == 472:
                    self._breaker.record_success()
                    return {"response": []}
                if response.status_code >= 500:
                    msg = (
                        f"ThetaData {method} {path} returned "
                        f"HTTP {response.status_code}"
                    )
                    raise ThetaDataTransientError(msg)
                if response.status_code >= 400:
                    msg = (
                        f"ThetaData {method} {path} returned "
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                    raise ThetaDataAuthError(msg)
                self._breaker.record_success()
                parsed: Any = response.json()
                return parsed
            except (httpx.RequestError, ThetaDataTransientError) as exc:
                last_error = exc
                self._breaker.record_failure()
                if attempt + 1 < self._retry.max_attempts:
                    backoff = min(
                        self._retry.initial_backoff_s * (2 ** attempt),
                        self._retry.max_backoff_s,
                    )
                    await asyncio.sleep(backoff)
                continue
            except ThetaDataAuthError:
                # 4xx — don't retry, but DO record as failure for the
                # breaker (a flood of 4xx is still a sign something
                # is wrong, e.g. rotated keys).
                self._breaker.record_failure()
                raise
        # Retries exhausted.
        if last_error is not None:
            msg = (
                f"ThetaData {method} {path} failed after "
                f"{self._retry.max_attempts} attempts: {last_error}"
            )
            raise ThetaDataTransientError(msg) from last_error
        msg = "unreachable: request loop exited without success or error"
        raise ThetaDataTransientError(msg)

    # -- WebSocket helpers --------------------------------------------------

    async def stream_ws(
        self,
        ws_path: str,
        *,
        subscriptions: Iterable[dict[str, Any]],
    ) -> Any:
        """Reserved — WebSocket streaming lives in ThetaDataLiveSource.

        Phase 3.3.2.5 introduced ``ThetaDataLiveSource`` as a separate
        class (in ``thetadata.live``) rather than folding WS support
        into the HTTP client. ``ThetaDataClient`` stays HTTP-only;
        callers that want live streaming use ``ThetaDataLiveSource``
        directly. This method is kept as a typed stub so the bisectable
        history's earlier pin (3.3.2.3) still finds the symbol.
        """
        msg = (
            "WebSocket streaming is implemented in ThetaDataLiveSource "
            "(uoa_detector.sources.thetadata.live), introduced in "
            "Phase 3.3.2.5. ThetaDataClient is HTTP-only by design."
        )
        raise NotImplementedError(msg)
