"""Phase 5.2.A2: ``UnusualWhalesClient.last_daily_request_count`` (decision P17).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("The count is
exposed read-only by an additive client attribute; client behaviour does not
change").

All tests use ``httpx.MockTransport``: no network, no environment.

Pins:
  - the last ``x-uw-daily-req-count`` header is captured and the parsed body
    is returned unchanged;
  - it starts as ``None``; a response without the header, or with a malformed
    or negative one, keeps the previous value;
  - it is captured on error responses too (daily-limit 429, 404), whose typed
    errors still raise;
  - across a retried request, the last response's value wins;
  - the attribute is read-only.
"""

from __future__ import annotations

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
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
)

_HEADER = "x-uw-daily-req-count"


def _settings() -> UnusualWhalesSettings:
    # 10 req/s, capacity 10: no test here drains the token bucket.
    return UnusualWhalesSettings(
        rate_limit_requests_per_second=10.0,
        historical_concurrency=2,
        live_reconnect_max_attempts=5,
        live_reconnect_initial_backoff_s=1.0,
        live_reconnect_max_backoff_s=60.0,
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


class _Script:
    """Replays responses in order; the last one repeats."""

    def __init__(self, responses: list[Callable[[], httpx.Response]]) -> None:
        self._responses = responses
        self.calls = 0

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]()


def _ok(count: str | None, payload: object | None = None) -> Callable[[], httpx.Response]:
    headers = {} if count is None else {_HEADER: count}
    return lambda: httpx.Response(200, json=payload if payload is not None else {"data": []}, headers=headers)


def _client(script: _Script, *, max_attempts: int = 1) -> UnusualWhalesClient:
    return UnusualWhalesClient(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        retry=RetryPolicy(max_attempts=max_attempts, initial_backoff_s=0.0, max_backoff_s=0.0),
        transport=httpx.MockTransport(script),
    )


async def test_header_is_captured_and_the_body_is_unchanged() -> None:
    client = _client(_Script([_ok("4105", {"data": [{"option_symbol": "SPY260915C00757000"}]})]))
    try:
        assert client.last_daily_request_count is None
        body = await client.request_json("/api/stock/SPY/option-contracts")
        assert body == {"data": [{"option_symbol": "SPY260915C00757000"}]}
        assert client.last_daily_request_count == 4105
    finally:
        await client.aclose()


async def test_missing_malformed_or_negative_header_keeps_the_previous_value() -> None:
    script = _Script([_ok("4105"), _ok(None), _ok("n/a"), _ok("-3"), _ok(" 4107 ")])
    client = _client(script)
    try:
        seen = []
        for _ in range(5):
            await client.request_json("/api/stock/SPY/info")
            seen.append(client.last_daily_request_count)
        assert seen == [4105, 4105, 4105, 4105, 4107]
    finally:
        await client.aclose()


async def test_stays_none_without_the_header() -> None:
    client = _client(_Script([_ok(None)]))
    try:
        await client.request_json("/api/stock/SPY/info")
        assert client.last_daily_request_count is None
    finally:
        await client.aclose()


async def test_captured_on_a_daily_limit_429_which_still_raises() -> None:
    script = _Script([
        lambda: httpx.Response(
            429, text='{"code":"daily_request_limit_hit"}', headers={_HEADER: "30000"},
        ),
    ])
    client = _client(script, max_attempts=3)
    try:
        with pytest.raises(UnusualWhalesDailyLimitError):
            await client.request_json("/api/stock/SPY/info")
        assert client.last_daily_request_count == 30000
        assert script.calls == 1  # fail-fast behaviour unchanged
    finally:
        await client.aclose()


async def test_captured_on_a_404_which_still_raises_not_found() -> None:
    script = _Script([lambda: httpx.Response(404, text="{}", headers={_HEADER: "5000"})])
    client = _client(script)
    try:
        with pytest.raises(UnusualWhalesNotFoundError):
            await client.request_json("/api/stock/ZZZZQ/info")
        assert client.last_daily_request_count == 5000
    finally:
        await client.aclose()


async def test_the_last_response_of_a_retried_request_wins() -> None:
    script = _Script([
        lambda: httpx.Response(503, text="unavailable", headers={_HEADER: "10"}),
        _ok("11", {"data": [1]}),
    ])
    client = _client(script, max_attempts=2)
    try:
        assert await client.request_json("/api/stock/SPY/info") == {"data": [1]}
        assert client.last_daily_request_count == 11
        assert script.calls == 2
    finally:
        await client.aclose()


async def test_attribute_is_read_only() -> None:
    client = _client(_Script([_ok("1")]))
    try:
        with pytest.raises(AttributeError):
            client.last_daily_request_count = 5  # type: ignore[misc]
    finally:
        await client.aclose()
