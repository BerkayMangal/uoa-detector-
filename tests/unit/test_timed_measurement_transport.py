"""Hermetic physical-attempt/receipt guards; no SQL, secrets or vendor access."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from webapp.timed_measurement.transport import QuoteTransport

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources._http_base import RetryPolicy, TokenBucket
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesClient,
    UnusualWhalesTransientError,
)

AT = datetime(2026, 11, 5, 15, tzinfo=UTC)
KEY = "synthetic-credential-never-an-output"
BODY = {"data": {"last_trade": {"price": "100.25", "time": 1793890800000}, "market_time": "regular"}}


def native_settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings(rate_limit_requests_per_second=10)


def quote(
    handler: Callable[[httpx.Request], Any], *, timeout: float = 8,
    limit: int = 65536, clock: Callable[[], datetime] = lambda: AT,
) -> QuoteTransport:
    return QuoteTransport(
        api_key=SecretStr(KEY), settings=native_settings(), timeout_seconds=timeout,
        max_response_bytes=limit, transport=httpx.MockTransport(handler), clock=clock,
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, events: list[str] | None = None) -> None:
        self.chunks, self.events = chunks, events
        self.read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            if self.events is not None:
                self.events.append("body")
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_quote_binds_exact_path_and_receipt_after_entire_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stream = Chunks([b'{"data":', b'{"last_trade":{"price":"100.25","time":1}}}'], events=events)
    clock = iter([AT, AT + timedelta(milliseconds=20)]).__next__

    async def acquire(self: TokenBucket, tokens: float = 1) -> None:
        events.append("bucket")

    async def reserve() -> None:
        events.append("reserved")

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("wire")
        assert str(request.url) == "https://api.unusualwhales.com/api/stock/BRK.B/quote"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=stream, headers={"x-uw-daily-req-count": "156"})

    monkeypatch.setattr(TokenBucket, "acquire", acquire)
    async with quote(handler, clock=clock) as transport:
        packet = await transport.fetch("BRK.B", before_request=reserve)
        assert transport.last_daily_request_count == 156
        assert transport.last_daily_request_at is not None
    assert packet.status == "ok"
    assert packet.requested_at == AT
    assert packet.received_at == AT + timedelta(milliseconds=20)
    assert packet.payload == {"data": {"last_trade": {"price": "100.25", "time": 1}}}
    assert packet.body_sha256 == hashlib.sha256(b"".join(stream.chunks)).hexdigest()
    assert packet.raw_body == b"".join(stream.chunks)
    assert packet.body_bytes == sum(map(len, stream.chunks))
    assert packet.wire_attempted and stream.closed
    assert events == ["bucket", "reserved", "wire", "body", "body"]


@pytest.mark.parametrize("error", [RuntimeError("refused"), TimeoutError("refused"), UnusualWhalesTransientError("refused")])
async def test_reservation_failure_escapes_unchanged_without_wire(error: Exception) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=BODY)

    async def refused() -> None:
        raise error

    async with quote(handler) as transport:
        with pytest.raises(type(error)) as caught:
            await transport.fetch("SPY", before_request=refused)
        assert caught.value is error
    assert calls == 0


@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        (401, {"error": KEY}, "auth"),
        (403, {"error": KEY}, "auth"),
        (404, {}, "not_found"),
        (422, {}, "not_found"),
        (429, {"error": "daily_request_limit_hit"}, "daily_quota"),
        (429, {}, "rate_limited"),
        (503, {}, "transport"),
        (302, BODY, "invalid_payload"),
        (200, [BODY], "invalid_payload"),
        (200, {"data": []}, "invalid_payload"),
    ],
)
async def test_one_attempt_errors_are_fixed_categories_without_body_or_exception_disclosure(
    code: int, body: object, expected: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            code, json=body, headers={"x-uw-daily-req-count": "157", "location": "https://elsewhere.invalid"},
        )

    async with quote(handler) as transport:
        packet = await transport.fetch("SPY")
    assert calls == 1
    assert packet.status == expected
    assert packet.http_status == code
    assert packet.received_at == AT
    assert packet.payload is None
    assert packet.native_daily_count == 157
    assert packet.raw_body is not None
    assert hashlib.sha256(packet.raw_body).hexdigest() == packet.body_sha256
    assert KEY not in repr(packet)


async def test_invalid_json_is_not_a_transport_success() -> None:
    async with quote(lambda request: httpx.Response(200, content=b"not json")) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "invalid_payload"
    assert packet.payload is None
    assert packet.body_bytes == 8
    assert packet.raw_body == b"not json"


@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_response_closes_stream_without_full_receipt(declared: bool) -> None:
    stream = Chunks([b"x" * 9, b"y" * 9, b"never-read"])
    headers = {"x-uw-daily-req-count": "158"}
    if declared:
        headers["content-length"] = "100"
    async with quote(lambda request: httpx.Response(200, stream=stream, headers=headers), limit=16) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "body_limit"
    assert packet.payload is None and packet.received_at is None
    assert packet.body_sha256 is None
    assert packet.raw_body is None
    assert packet.native_daily_count == 158
    assert stream.closed
    assert stream.read == (0 if declared else 2)


async def test_auth_halt_identity_survives_rejected_oversize_body() -> None:
    async with quote(lambda request: httpx.Response(403, headers={"content-length": "100"}), limit=16) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "auth"
    assert packet.http_status == 403


async def test_unreadable_429_is_a_conservative_daily_halt() -> None:
    async with quote(lambda request: httpx.Response(429, headers={"content-length": "100"}), limit=16) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "quota_unknown" and packet.http_status == 429
    assert packet.raw_body is None and packet.received_at is None


async def test_nonidentity_encoding_is_rejected_before_decompression() -> None:
    stream = Chunks([b"not-valid-gzip"])
    async with quote(lambda request: httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "invalid_payload"
    assert packet.received_at is None
    assert stream.read == 0 and stream.closed


@pytest.mark.parametrize("ticker", ["spy", "SPY/quote", "SPY?x=1", "SPY#x", " SPY", "", "https://evil", "\uff33\uff30\uff39"])
async def test_invalid_path_ticker_is_rejected_before_callback_or_wire(ticker: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid ticker reached HTTP")

    async with quote(handler) as transport:
        with pytest.raises(ValueError, match="uppercase ticker"):
            await transport.fetch(ticker)


async def test_clock_regression_has_no_admissible_payload() -> None:
    clock = iter([AT, AT - timedelta(seconds=1)]).__next__
    async with quote(lambda request: httpx.Response(200, json=BODY), clock=clock) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "clock_error" and packet.payload is None


async def test_naive_request_clock_does_not_reach_wire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid clock reached HTTP")

    async with quote(handler, clock=lambda: AT.replace(tzinfo=None)) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "clock_error" and not packet.wire_attempted


async def test_deadline_is_checked_after_reservation_at_final_request_clock() -> None:
    moment = AT
    reserved = False

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("reservation crossed deadline before wire")

    async def reserve() -> None:
        nonlocal moment, reserved
        reserved = True
        moment += timedelta(seconds=5)

    async with quote(handler, clock=lambda: moment) as transport:
        packet = await transport.fetch("SPY", before_request=reserve, deadline=AT + timedelta(seconds=5))
    assert reserved
    assert packet.status == "late" and not packet.wire_attempted
    assert packet.requested_at == AT + timedelta(seconds=5)
    assert packet.received_at is None and packet.payload is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"data":{"last_trade":{"price":"1","price":"2","time":1}}}',
        b'{"data":{"x":NaN}}',
        b'{"data":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
    ],
)
async def test_ambiguous_nonfinite_and_deep_json_keep_raw_evidence_but_no_value(body: bytes) -> None:
    async with quote(lambda request: httpx.Response(200, content=body)) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "invalid_payload"
    assert packet.payload is None and packet.raw_body == body
    assert packet.body_sha256 == hashlib.sha256(body).hexdigest()


async def test_packet_does_not_retain_prior_success_after_network_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=BODY)
        raise httpx.ConnectError("synthetic failure", request=request)

    async with quote(handler) as transport:
        first = await transport.fetch("SPY")
        second = await transport.fetch("AAPL")
    assert first.status == "ok"
    assert second.status == "transport" and second.ticker == "AAPL"
    assert second.received_at is None and second.raw_body is None
    assert second.payload is None and second.body_sha256 is None
    assert calls == 2


async def test_deadline_cancels_owned_http_without_background_retry() -> None:
    cancelled = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    async with quote(handler, timeout=0.01) as transport:
        packet = await transport.fetch("SPY")
    assert packet.status == "timeout" and packet.received_at is None
    assert cancelled.is_set() and calls == 1


async def test_external_cancellation_escapes_without_manufactured_packet() -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with quote(handler) as transport:
        task = asyncio.create_task(transport.fetch("SPY"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_native_hooks_run_per_attempt_and_observe_error_response_bodies() -> None:
    events: list[str] = []
    codes = iter([503, 200])

    async def before() -> None:
        events.append("before")

    async def after(response: httpx.Response) -> None:
        assert response.content
        events.append(f"after{response.status_code}")

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("wire")
        return httpx.Response(next(codes), json=BODY)

    client = UnusualWhalesClient(
        api_key=SecretStr(KEY), settings=native_settings(), transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=2, initial_backoff_s=0, max_backoff_s=0),
        before_request=before, after_response=after,
    )
    try:
        assert await client.request_json("/api/stock/SPY/quote") == BODY
    finally:
        await client.aclose()
    assert events == ["before", "wire", "after503", "before", "wire", "after200"]


async def test_native_after_hook_refusal_is_not_retried_or_reclassified() -> None:
    calls = 0
    error = UnusualWhalesTransientError("callback failure")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=BODY)

    async def after(response: httpx.Response) -> None:
        raise error

    client = UnusualWhalesClient(
        api_key=SecretStr(KEY), settings=native_settings(), transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=3), after_response=after,
    )
    try:
        with pytest.raises(UnusualWhalesTransientError) as caught:
            await client.request_json("/api/stock/SPY/quote")
        assert caught.value is error
    finally:
        await client.aclose()
    assert calls == 1
