"""Chain exhaustion, incomplete evidence, and versioned archive preservation."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from email.utils import format_datetime

import httpx
import pytest
from webapp.history_capture.capture import CaptureStoppedError, Client, read
from webapp.history_capture.capture_v2 import capture, chain, settings

DAY = date(2026, 6, 24)


def response(records):
    return httpx.Response(
        200,
        json={"data": records},
        headers={
            "date": format_datetime(datetime.now(UTC)),
            "x-uw-daily-req-count": "100",
            "x-uw-token-req-limit": "30000",
        },
    )


def row(symbol, **values):
    return {"option_symbol": symbol, "nbbo_bid": "1.00", **values}


@pytest.mark.asyncio
async def test_full_500_row_page_is_not_misclassified_as_the_complete_chain():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["limit"] == "500"
        assert request.url.params["date"] == DAY.isoformat()
        page = int(request.url.params["page"])
        return response([row(str(i)) for i in range(500)] if page == 0 else [row("500")])

    cfg = settings().model_copy(update={"spacing_seconds": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await chain(Client(http, cfg), "AAPL", DAY, cfg)
    assert result["complete"] and result["stop_reason"] == "pages_exhausted"
    assert len(result["payload"]["data"]) == 501
    assert [r.url.params["page"] for r in calls] == ["0", "1"]
    assert [p["row_count"] for p in result["pages"]] == [500, 1]
    assert all(p["payload_hash"] and p["quota"] and p["received_at"] for p in result["pages"])


@pytest.mark.asyncio
async def test_identical_boundary_contracts_are_deduplicated_until_empty_page():
    pages = [[row("a"), row("b")], [row("b"), row("c")], []]
    cfg = settings().model_copy(update={"spacing_seconds": 0, "chain_page_limit": 2})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(pages.pop(0)))
    ) as http:
        result = await chain(Client(http, cfg), "AAPL", DAY, cfg)
    assert result["complete"]
    assert [r["option_symbol"] for r in result["payload"]["data"]] == ["a", "b", "c"]
    assert len(result["pages"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_page", [[row("a"), row("b")], [row("b")]])
async def test_no_progress_page_remains_incomplete_even_when_short(terminal_page):
    pages = [[row("a"), row("b")], terminal_page]
    cfg = settings().model_copy(update={"spacing_seconds": 0, "chain_page_limit": 2})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(pages.pop(0)))
    ) as http:
        result = await chain(Client(http, cfg), "AAPL", DAY, cfg)
    assert not result["complete"] and result["stop_reason"] == "page_stall"
    assert len(result["payload"]["data"]) == 2 and len(result["pages"]) == 2


@pytest.mark.asyncio
async def test_page_cap_does_not_assert_exhaustion():
    cfg = settings().model_copy(update={
        "spacing_seconds": 0, "chain_page_limit": 2, "max_chain_pages": 1,
    })
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response([row("a"), row("b")]))
    ) as http:
        result = await chain(Client(http, cfg), "AAPL", DAY, cfg)
    assert not result["complete"] and result["stop_reason"] == "page_limit"
    assert len(result["payload"]["data"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("last_page, reason", [
    ([row("b", nbbo_bid="9.00")], "conflicting_contract_identity"),
    ([{"nbbo_bid": "1"}], "missing_contract_identity"),
])
async def test_conflicting_or_unidentified_contracts_fail_closed(last_page, reason):
    pages = [[row("a"), row("b")], last_page]
    cfg = settings().model_copy(update={"spacing_seconds": 0, "chain_page_limit": 2})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(pages.pop(0)))
    ) as http:
        result = await chain(Client(http, cfg), "AAPL", DAY, cfg)
    assert not result["complete"] and result["stop_reason"] == reason
    assert len(result["pages"]) == 2


@pytest.mark.asyncio
async def test_request_budget_preserves_received_page_as_incomplete_without_retry():
    cfg = settings().model_copy(update={
        "spacing_seconds": 0, "chain_page_limit": 2, "max_requests": 1,
    })
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response([row("a"), row("b")]))
    ) as http:
        client = Client(http, cfg)
        result = await chain(client, "AAPL", DAY, cfg)
    assert client.calls == 1
    assert not result["complete"] and result["stop_reason"] == "request_budget"
    assert len(result["pages"]) == 1 and len(result["payload"]["data"]) == 2


def capture_config(**updates):
    return settings().model_copy(update={
        "start": DAY, "end": DAY, "tickers": ("AAPL",),
        "spacing_seconds": 0, "chain_page_limit": 2, **updates,
    })


def capture_response(request):
    if request.url.path.endswith("/ohlc/1d"):
        return response([{"date": DAY.isoformat()}])
    if request.url.path.endswith("/ohlc/1m"):
        return response([{"start_time": "2026-06-24T14:00:00+00:00"}])
    if request.url.path.endswith("/flow-alerts"):
        return response([])
    if request.url.path.endswith("/option-chains"):
        return response([row("a"), row("b")] if request.url.params["page"] == "0" else [])
    pytest.fail("unexpected route")


@pytest.mark.asyncio
async def test_capture_seals_incomplete_chain_and_stops_instead_of_claiming_full_archive(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-only")
    result = await capture(
        tmp_path, cfg=capture_config(max_chain_pages=1),
        transport=httpx.MockTransport(capture_response),
    )
    assert result["status"] == "stopped"
    assert result["reason"] == "incomplete_chain_page_limit"
    packet = read(tmp_path / DAY.isoformat() / "AAPL-chain.json.gz")
    assert not packet["complete"] and len(packet["payload"]["data"]) == 2
    assert not (tmp_path / DAY.isoformat() / "SPY-minute.json.gz").exists()


@pytest.mark.asyncio
async def test_completed_v2_capture_resumes_without_network_and_preserves_seals(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-only")
    cfg = capture_config()
    first = await capture(tmp_path, cfg=cfg, transport=httpx.MockTransport(capture_response))
    assert first["status"] == "complete" and first["calls_this_run"] == 7
    packet_path = tmp_path / DAY.isoformat() / "AAPL-chain.json.gz"
    original = packet_path.read_bytes()

    def no_network(request):
        pytest.fail("complete sealed capture must not call the provider again")

    second = await capture(tmp_path, cfg=cfg, transport=httpx.MockTransport(no_network))
    assert second["status"] == "complete" and second["calls_this_run"] == 0
    assert second["resumed"] == 6
    assert packet_path.read_bytes() == original


@pytest.mark.asyncio
async def test_old_profile_archive_cannot_be_silently_upgraded_or_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-only")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"profile_hash": "legacy-v1"}))
    original = state.read_bytes()
    with pytest.raises(CaptureStoppedError, match="new_destination"):
        await capture(tmp_path, cfg=capture_config())
    assert state.read_bytes() == original


@pytest.mark.asyncio
async def test_unknown_calendar_year_cannot_report_complete_with_no_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-only")
    monkeypatch.setattr("webapp.history_capture.capture_v2.is_session_day", lambda day, cal: None)
    result = await capture(
        tmp_path, cfg=capture_config(), transport=httpx.MockTransport(capture_response),
    )
    assert result["status"] == "stopped"
    assert result["reason"] == "calendar_year_unknown"
    assert not (tmp_path / DAY.isoformat()).exists()
