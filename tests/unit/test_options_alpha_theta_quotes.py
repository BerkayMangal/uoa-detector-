"""Native Theta quote timestamps stay distinct from UW trades and side updates."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.options_alpha.theta_quotes import (
    ThetaQuoteBatch,
    ThetaQuoteClient,
    evaluate_theta_long_option,
    parse_theta_quote_payload,
    select_first_theta_quote,
)

_SYMBOL = "AAPL261120C00300000"
_SESSION = date(2026, 11, 9)
_START = datetime(2026, 11, 9, 15, tzinfo=UTC)
_RECEIVED = _START + timedelta(days=3)
_CONTRACT = {"symbol": "AAPL", "expiration": "2026-11-20", "strike": "300", "right": "CALL"}


def _row(offset: int = 0, **changes: object) -> dict[str, object]:
    stamp = (_START + timedelta(seconds=offset)).astimezone(ZoneInfo("America/New_York"))
    row: dict[str, object] = {
        "timestamp": stamp.replace(tzinfo=None).isoformat(),
        "bid": "1.00", "ask": "1.10", "bid_size": 4, "ask_size": 5,
    }
    row.update(changes)
    return row


def _payload(*rows: dict[str, object]) -> dict[str, Any]:
    return {"response": [{"contract": dict(_CONTRACT), "data": list(rows)}]}


def _parse(payload: object, *, offset: int = 0, **changes: Any) -> ThetaQuoteBatch:
    inputs: dict[str, Any] = {
        "option_symbol": _SYMBOL, "session": _SESSION,
        "window_start": _START + timedelta(seconds=offset),
        "window_end": _START + timedelta(seconds=offset + 120),
        "received_at": _RECEIVED, "requested_interval": "tick", "http_status": 200,
    }
    inputs.update(changes)
    return parse_theta_quote_payload(payload, **inputs)


def test_native_wrapper_preserves_quote_time_size_and_original_timestamp() -> None:
    batch = _parse(_payload(_row(10)))
    assert batch.status == "ok" and batch.coverage_complete
    quote = batch.quotes[0]
    assert quote.quote_at == _START + timedelta(seconds=10)
    assert quote.source_timestamp == "2026-11-09T10:00:10"
    assert quote.bid_size == 4 and quote.ask_size == 5
    wire = quote.as_dict()
    assert wire["source"] == "thetadata" and wire["event_type"] == "quote"
    assert wire["quote_time_basis"] == "composite_quote_snapshot"
    assert wire["bid_update_age_seconds"] is None
    assert "executed_at" not in wire and "canceled" not in wire
    assert batch.as_dict(include_raw=True)["raw_payload"] == _payload(_row(10))


def test_flat_rows_and_explicit_utc_time_are_supported() -> None:
    row = {**_CONTRACT, **_row(timestamp="2026-11-09T15:00:00Z")}
    batch = _parse([row])
    assert batch.status == "ok"
    assert batch.quotes[0].quote_at == _START


def test_summer_new_york_timestamp_is_converted_with_dst() -> None:
    start = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    batch = _parse(_payload(_row(timestamp="2026-07-06T09:30:00")),
                   session=start.date(), window_start=start, window_end=start + timedelta(seconds=120))
    assert batch.status == "ok"
    assert batch.quotes[0].quote_at == start


@pytest.mark.parametrize("changes", [
    {"bid": "NaN"}, {"ask": "Infinity"}, {"bid": "2"},
    {"bid_size": True}, {"ask_size": "1.5"}, {"ask_size": None},
    {"timestamp": "not-a-date"}, {"timestamp": "2026-11-09T09:59:59"},
    {"timestamp": "2026-11-10T10:00:00"},
    {"quote_timestamp": "2026-11-09T10:00:01"},
    {"symbol": "MSFT"},
])
def test_invalid_rows_make_coverage_incomplete_not_cash_zero(changes: dict[str, object]) -> None:
    batch = _parse(_payload(_row(**changes), _row(30)))
    assert batch.status == "partial"
    assert not batch.coverage_complete
    assert sum(batch.rejected_rows.values()) == 1
    quote = select_first_theta_quote(batch, target_at=_START,
                                     calendar=load_live_alpha_settings().calendar)
    assert quote is None


def test_conflicting_same_timestamp_quotes_are_not_arbitrarily_ordered() -> None:
    batch = _parse(_payload(_row(), _row(bid="0.90", ask="1.00")))
    assert batch.status == "unsupported_payload"
    assert batch.rejected_rows == {"ambiguous_same_timestamp_quotes": 2}


def test_duplicate_identical_quotes_are_collapsed() -> None:
    assert len(_parse(_payload(_row(), _row())).quotes) == 1


@pytest.mark.parametrize("payload", [
    {"response": [[1, 2, 3]]}, {"unknown": []},
    {"response": [], "next_cursor": "another-page"},
    {"response": [{"contract": {**_CONTRACT, "symbol": "MSFT"}, "data": [_row()]}]},
    {"response": [{"contract": _CONTRACT, "data": [0]}]},
    [_row()],
])
def test_unsupported_shapes_mismatched_contracts_and_continuations_fail_closed(payload: object) -> None:
    assert _parse(payload).status == "unsupported_payload"


def test_empty_native_response_is_no_quotes() -> None:
    batch = _parse({"response": []})
    assert batch.status == "no_quotes" and not batch.quotes


@pytest.mark.parametrize("interval", [None, "1s", "1m", ""])
def test_sampled_or_unrecorded_interval_cannot_produce_first_tick_markout(interval: str | None) -> None:
    batch = _parse(_payload(_row()), requested_interval=interval)
    assert batch.status == "unsupported_payload"
    assert batch.reason == "missing_or_unsupported_request_interval"
    assert not batch.coverage_complete


@pytest.mark.parametrize("http_status", [None, 401, 472, 500, True])
def test_good_looking_payload_requires_successful_transport_evidence(http_status: object) -> None:
    batch = _parse(_payload(_row()), http_status=http_status)
    assert batch.status == "unsupported_payload"
    assert batch.reason == "missing_or_unsuccessful_http_status"
    assert not batch.coverage_complete


@pytest.mark.parametrize("location", ["wrapper", "header", "contract", "row"])
def test_nested_continuation_never_becomes_complete_window(location: str) -> None:
    payload = _payload(_row())
    wrapper = payload["response"][0]
    if location == "wrapper":
        wrapper["next_cursor"] = "next"
    elif location == "header":
        payload["header"] = {"pagination": {"has_more": True}}
    elif location == "contract":
        wrapper["contract"]["next_page"] = 2
    else:
        wrapper["data"][0]["next_page_url"] = "next"
    batch = _parse(payload)
    assert batch.status == "unsupported_payload"
    assert batch.reason == "continuation_not_supported"
    assert not batch.coverage_complete


def test_selection_also_checks_transport_proof_on_supplied_batch() -> None:
    good = _parse(_payload(_row()))
    for altered in (replace(good, requested_interval="1s"), replace(good, http_status=None)):
        assert select_first_theta_quote(altered, target_at=_START,
                                       calendar=load_live_alpha_settings().calendar) is None


def test_row_and_window_limits_are_enforced() -> None:
    assert _parse(_payload(_row(), _row(1)), max_rows=1).status == "response_limit_exceeded"
    assert _parse(_payload(_row()), window_end=_START + timedelta(seconds=121)).status == "invalid_request"
    assert _parse(_payload(_row()), window_start=_START.replace(tzinfo=None)).status == "invalid_request"


def test_evaluation_uses_native_option_quotes_and_alternative_holds() -> None:
    entry = _parse(_payload(_row(10)))
    exit_15 = _parse(_payload(_row(910, bid="1.20", ask="1.30")), offset=910)
    exit_60 = _parse(_payload(_row(3610, bid="0.90", ask="1.00")), offset=3610)
    result = evaluate_theta_long_option(entry_batch=entry, exit_batches={15: exit_15, 60: exit_60},
                                        calendar=load_live_alpha_settings().calendar)
    assert result["status"] == "EVALUATED"
    assert result["markouts"][0]["modeled_net_pnl_usd"] == "6.70"
    assert result["markouts"][1]["modeled_net_pnl_usd"] == "-23.30"
    assert result["entry_delay_seconds"] == 10
    assert result["entry_same_quote_roundtrip_friction_usd"] == "13.30"
    assert result["side_update_age_seconds"] is None
    assert result["executable_fill_proven"] is False
    assert "executed_at" not in result["entry_quote"]


def test_exit_window_must_start_from_selected_entry_not_original_target() -> None:
    entry = _parse(_payload(_row(10)))
    early_batch = _parse(_payload(_row(900, bid="3", ask="4")), offset=900)
    result = evaluate_theta_long_option(entry_batch=entry, exit_batches={15: early_batch},
                                        calendar=load_live_alpha_settings().calendar)
    assert result["status"] == "PARTIAL"
    assert result["markouts"][0]["modeled_net_pnl_usd"] is None
    assert result["markouts"][1]["reason"] == "exit_batch_missing"


def test_zero_size_and_zero_bid_are_preserved_but_not_selected() -> None:
    batch = _parse(_payload(_row(bid_size=0), _row(1, bid="0")))
    assert batch.status == "ok"
    assert select_first_theta_quote(batch, target_at=_START,
                                     calendar=load_live_alpha_settings().calendar) is None


@pytest.mark.asyncio
async def test_client_sends_exact_bounded_tick_request_without_credentials() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/option/history/quote"
        assert dict(request.url.params) == {
            "symbol": "AAPL", "expiration": "20261120", "strike": "300", "right": "call",
            "date": "20261109", "interval": "tick", "format": "json",
            "start_time": "10:00:00.000000", "end_time": "10:02:00.000000",
        }
        assert "x-api-key" not in request.headers
        assert "x-username" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, json=_payload(_row()))

    client = ThetaQuoteClient(transport=httpx.MockTransport(handle))
    batch = await client.fetch_window(option_symbol=_SYMBOL, session=_SESSION,
                                      window_start=_START, window_end=_START + timedelta(seconds=120))
    assert batch.status == "ok" and batch.raw_sha256
    assert batch.requested_interval == "tick"


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "status"), [
    (401, "authentication_failed"), (403, "authentication_failed"),
    (472, "no_quotes"), (429, "rate_limited"), (500, "terminal_error"), (302, "terminal_error"),
])
async def test_http_errors_are_typed_and_do_not_expose_server_body(code: int, status: str) -> None:
    client = ThetaQuoteClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(code, text="private-server-error")))
    batch = await client.fetch_window(option_symbol=_SYMBOL, session=_SESSION,
                                      window_start=_START, window_end=_START + timedelta(seconds=120))
    assert batch.status == status and batch.http_status == code
    assert "private-server-error" not in json.dumps(batch.as_dict(include_raw=True))


@pytest.mark.asyncio
async def test_unreachable_terminal_and_malformed_json_do_not_fake_empty_quotes() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-connection-details")

    for transport, expected in [
        (httpx.MockTransport(unavailable), "terminal_unreachable"),
        (httpx.MockTransport(lambda request: httpx.Response(200, text="not-json")), "unsupported_payload"),
    ]:
        batch = await ThetaQuoteClient(transport=transport).fetch_window(
            option_symbol=_SYMBOL, session=_SESSION,
            window_start=_START, window_end=_START + timedelta(seconds=120))
        assert batch.status == expected
        assert "private-connection-details" not in json.dumps(batch.as_dict(include_raw=True))


@pytest.mark.asyncio
async def test_body_limit_stops_large_responses() -> None:
    client = ThetaQuoteClient(max_body_bytes=32, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 33)))
    batch = await client.fetch_window(option_symbol=_SYMBOL, session=_SESSION,
                                      window_start=_START, window_end=_START + timedelta(seconds=120))
    assert batch.status == "response_limit_exceeded"


def test_terminal_origin_must_not_contain_credentials_or_redirect_path() -> None:
    for url in ("http://username:password@localhost:25503", "file:///tmp/a", "https://example.com/elsewhere"):
        with pytest.raises(ValueError):
            ThetaQuoteClient(url)
