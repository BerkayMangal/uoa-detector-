"""The downloadable Mac connector stays standalone, bounded, and credential safe."""

from __future__ import annotations

import ast
import copy
import io
import json
import time
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from scripts import theta_mac_connector as connector

START = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
CLOSE = datetime(2026, 11, 9, 21, 0, tzinfo=UTC)
SYMBOL = "AAPL261120C00300000"
JOB = {
    "request_id": "job_123", "request_hash": "a" * 64,
    "option_symbol": SYMBOL, "planned_entry_at": START.isoformat(),
    "session_close_at": CLOSE.isoformat(),
}


def payload(stamp="2026-11-09T10:00:03.000", **changes):
    return {"response": [{
        "contract": {"symbol": "AAPL", "expiration": "2026-11-20", "strike": 300.0, "right": "CALL"},
        "data": [{"timestamp": stamp, "bid": 1.0, "ask": 1.1,
                  "bid_size": 4, "ask_size": 5, **changes}],
    }]}


def packet(data=None, start=START, label="entry", code=200):
    return {
        "label": label, "window_start": start.isoformat(),
        "window_end": (start + timedelta(seconds=120)).isoformat(),
        "received_at": datetime(2026, 11, 10, 20, tzinfo=UTC).isoformat(),
        "payload": payload() if data is None else data,
        "http_status": code, "requested_interval": "tick",
    }


def test_standalone_script_uses_only_stdlib_and_python39_syntax():
    source = Path(connector.__file__).read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    imports = {
        (node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {item.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
         for item in node.names}
    assert imports <= {
        "__future__", "argparse", "base64", "getpass", "json", "os", "re", "socket", "ssl",
        "sys", "time", "dataclasses", "datetime", "decimal", "typing", "urllib", "zoneinfo",
        "signal", "contextlib", "http",
    }
    assert "datetime.UTC" not in source  # unavailable in Python 3.9


@pytest.mark.parametrize("value", ["http://example.com", "https://user:secret@example.com",
                                    "https://example.com/path", "https://example.com?token=secret"])
def test_insecure_or_credential_embedded_uoa_urls_are_rejected(value):
    with pytest.raises(connector.ConnectorError, match="invalid_request") as error:
        connector.origin(value)
    assert "secret" not in str(error.value)


def test_terminal_must_remain_local_and_origin_has_no_credentials():
    assert connector.origin("http://127.0.0.1:25503/", terminal=True) == "http://127.0.0.1:25503"
    with pytest.raises(connector.ConnectorError):
        connector.origin("https://another-host.example", terminal=True)


def test_native_eastern_timestamp_preserved_and_converted_with_dst():
    assert connector.native_time("2026-11-09T10:00:03.123") == START + timedelta(seconds=3, milliseconds=123)
    assert connector.native_time("2026-07-09T10:00:00.000").hour == 14
    assert connector.native_time("2026-11-09T15:00:03Z") == START + timedelta(seconds=3)
    with pytest.raises(connector.ConnectorError, match="unsupported_payload"):
        connector.native_time("2026-11-01T01:30:00")  # ambiguous DST fold


def test_first_quote_skips_zero_liquidity_and_deduplicates_identical_ticks():
    data = payload("2026-11-09T10:00:00", bid=0, bid_size=0)
    later = payload()["response"][0]["data"][0]
    data["response"][0]["data"] += [later, copy.deepcopy(later)]
    assert connector.first_quote(packet(data), connector.Contract.from_occ(SYMBOL), CLOSE) == START + timedelta(seconds=3)


@pytest.mark.parametrize("envelope", ["list", "response", "data"])
def test_supported_flat_v3_rows_require_their_own_exact_contract_identity(envelope):
    group = payload()["response"][0]
    flat = [{**group["contract"], **group["data"][0]}]
    data = flat if envelope == "list" else {envelope: flat}
    assert connector.first_quote(packet(data), connector.Contract.from_occ(SYMBOL), CLOSE) == START + timedelta(seconds=3)


def test_nested_continuation_is_not_mistaken_for_complete_quote_coverage():
    data = payload()
    data["header"] = {"pagination": {"next_page_url": "http://local/more"}}
    with pytest.raises(connector.ConnectorError, match="unsupported_payload"):
        connector.first_quote(packet(data), connector.Contract.from_occ(SYMBOL), CLOSE)


@pytest.mark.parametrize("changes", [
    {"bid": 2, "ask": 1}, {"bid": "NaN"}, {"ask_size": -1}, {"bid_size": 1.5},
    {"timestamp": "2026-11-09T09:59:59"}, {"timestamp": "2026-11-09T10:02:01"},
    {"quote_timestamp": "2026-11-09T10:00:04"}, {"strike": 305},
])
def test_any_bad_native_row_invalidates_the_entire_window(changes):
    data = payload()
    data["response"][0]["data"].append(payload(**changes)["response"][0]["data"][0])
    with pytest.raises(connector.ConnectorError, match="unsupported_payload"):
        connector.first_quote(packet(data), connector.Contract.from_occ(SYMBOL), CLOSE)


def test_wrong_group_identity_and_conflicting_same_timestamp_quotes_are_rejected():
    data = payload()
    data["response"][0]["contract"]["symbol"] = "MSFT"
    with pytest.raises(connector.ConnectorError):
        connector.first_quote(packet(data), connector.Contract.from_occ(SYMBOL), CLOSE)
    data = payload()
    data["response"][0]["data"].append(payload(bid=0.9)["response"][0]["data"][0])
    with pytest.raises(connector.ConnectorError):
        connector.first_quote(packet(data), connector.Contract.from_occ(SYMBOL), CLOSE)


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, body=b"{}", error=None, headers=None):
        self.body = body
        self.error = error
        self.headers = headers
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.body, headers=self.headers)


def transport():
    return connector.Transport("https://uoa.example", "http://127.0.0.1:25503", "private-user", "private-password")


def test_http_credentials_only_go_to_uoa_and_terminal_request_has_exact_contract_window():
    client = transport()
    client._remote = FakeOpener(b'{"jobs":[]}')
    client._local = FakeOpener(json.dumps(payload()).encode())
    client.jobs()
    result = client.window(connector.Contract.from_occ(SYMBOL), START, "entry")
    remote, timeout = client._remote.calls[0]
    local, local_timeout = client._local.calls[0]
    assert remote.get_header("Authorization").startswith("Basic ")
    assert local.get_header("Authorization") is None
    assert timeout == local_timeout == 20
    params = parse_qs(urlsplit(local.full_url).query)
    assert params == {
        "symbol": ["AAPL"], "expiration": ["20261120"], "strike": ["300"], "right": ["call"],
        "date": ["20261109"], "interval": ["tick"], "format": ["json"],
        "start_time": ["10:00:00.000000"], "end_time": ["10:02:00.000000"],
    }
    assert result["payload"] == payload()
    client.upload("job_123", {"request_hash": "a" * 64, "packets": []})
    uploaded = client._remote.calls[-1][0]
    assert uploaded.get_header("Origin") == "https://uoa.example"
    assert uploaded.get_method() == "POST"


@pytest.mark.parametrize("headers,body", [({"Content-Length": "1000"}, b"{}"), ({}, b"x" * 101)])
def test_http_response_limit_is_checked_before_or_during_body_read(monkeypatch, headers, body):
    monkeypatch.setattr(connector, "MAX_BYTES", 100)
    client = transport()
    client._local = FakeOpener(body, headers=headers)
    with pytest.raises(connector.ConnectorError, match="response_too_large"):
        client.window(connector.Contract.from_occ(SYMBOL), START, "entry")
    assert len(client._local.calls) == 1


def test_terminal_472_remains_472_and_is_not_fabricated_as_success():
    client = transport()
    client._local = FakeOpener(error=HTTPError("secret-url", 472, "secret-message", {}, io.BytesIO(b"no data")))
    result = client.window(connector.Contract.from_occ(SYMBOL), START, "entry")
    assert result["http_status"] == 472 and result["payload"] is None
    assert connector.first_quote(result, connector.Contract.from_occ(SYMBOL), CLOSE) is None


@pytest.mark.parametrize("error,status", [
    (URLError("private-password and secret-url"), "terminal_unreachable"),
    (HTTPError("secret-url", 401, "private-password", {}, io.BytesIO(b"secret")), "authentication_failed"),
])
def test_failures_do_not_retry_or_expose_remote_error_text(error, status):
    client = transport()
    client._local = FakeOpener(error=error)
    with pytest.raises(connector.ConnectorError, match=status) as caught:
        client.window(connector.Contract.from_occ(SYMBOL), START, "entry")
    assert str(caught.value) == status and len(client._local.calls) == 1


def test_redirects_are_never_followed_with_credentials():
    with pytest.raises(connector.ConnectorError, match="request_failed"):
        connector.NoRedirect().redirect_request(None, None, 302, "secret", {}, "https://evil.example")


def test_overall_deadline_interrupts_a_slow_open_without_waiting_for_socket_timeout(monkeypatch):
    class SlowOpener:
        def open(self, request, timeout):
            time.sleep(1)
            pytest.fail("absolute deadline must interrupt this operation")

    monkeypatch.setattr(connector, "TIMEOUT_SECONDS", 0.02)
    client = transport()
    client._local = SlowOpener()
    started = time.monotonic()
    with pytest.raises(connector.ConnectorError, match="terminal_unreachable"):
        client.window(connector.Contract.from_occ(SYMBOL), START, "entry")
    assert time.monotonic() - started < 0.5


def test_truncated_http_response_never_exposes_partial_body_in_traceback():
    class TruncatedResponse(FakeResponse):
        def read(self, size=-1):
            raise IncompleteRead(b"private-password server-body", 100)

    class TruncatedOpener:
        def open(self, request, timeout):
            return TruncatedResponse(b"")

    client = transport()
    client._local = TruncatedOpener()
    with pytest.raises(connector.ConnectorError) as error:
        client.window(connector.Contract.from_occ(SYMBOL), START, "entry")
    assert str(error.value) == "terminal_unreachable"
    assert error.value.__suppress_context__


class JobTransport:
    def __init__(self, entry_data=None, fail=None):
        self.calls = []
        self.uploaded = []
        self.entry_data = entry_data
        self.fail = fail

    def window(self, contract, start, label):
        self.calls.append((start, label))
        if self.fail:
            raise connector.ConnectorError(self.fail)
        data = self.entry_data if label == "entry" and self.entry_data is not None else payload(
            (start + timedelta(seconds=3)).astimezone(connector.ET).replace(tzinfo=None).isoformat())
        return packet(data, start, label)

    def upload(self, request_id, upload):
        self.uploaded.append((request_id, upload))


def test_job_uses_native_entry_time_for_two_horizons_and_uploads_raw_packets_without_pnl():
    client = JobTransport()
    assert connector.run_job(client, JOB) == "ok"
    assert client.calls == [
        (START, "entry"), (START + timedelta(minutes=15, seconds=3), "m15"),
        (START + timedelta(minutes=60, seconds=3), "m60"),
    ]
    request_id, result = client.uploaded[0]
    assert request_id == JOB["request_id"] and result["request_hash"] == JOB["request_hash"]
    assert len(result["packets"]) == 3 and result["attempted_at"]
    assert "pnl" not in json.dumps(result).lower()
    assert result["packets"][0]["payload"] == payload("2026-11-09T10:00:03")


def test_early_close_prevents_fetching_an_out_of_session_horizon():
    client = JobTransport()
    early = START + timedelta(minutes=30)
    assert connector.run_job(client, {**JOB, "session_close_at": early.isoformat()}) == "ok"
    assert [label for _, label in client.calls] == ["entry", "m15"]


@pytest.mark.parametrize("reason", ["terminal_unreachable", "authentication_failed", "response_too_large"])
def test_whole_terminal_failure_uploads_typed_unknown_attempt_without_packets(reason):
    client = JobTransport(fail=reason)
    assert connector.run_job(client, JOB) == reason
    assert len(client.calls) == 1
    assert client.uploaded[0][1]["packets"] == []
    assert client.uploaded[0][1]["status"] == reason


def test_invalid_entry_does_not_trigger_exit_fetches_and_uploads_unknown():
    client = JobTransport(entry_data=payload(strike=301))
    assert connector.run_job(client, JOB) == "unsupported_payload"
    assert len(client.calls) == 1 and client.uploaded[0][1]["packets"] == []


def test_no_entry_quotes_uploads_only_native_empty_entry():
    client = JobTransport(entry_data={"response": []})
    assert connector.run_job(client, JOB) == "ok"
    assert len(client.calls) == 1 and len(client.uploaded[0][1]["packets"]) == 1


def test_job_count_and_watch_poll_are_bounded():
    client = transport()
    client._remote = FakeOpener(json.dumps({"jobs": [JOB] * 5}).encode())
    with pytest.raises(connector.ConnectorError):
        client.jobs()
    with pytest.raises(SystemExit):
        connector.main(["--watch", "--poll-seconds", "59"])
