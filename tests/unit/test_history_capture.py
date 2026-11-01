"""No live API calls: exercise budget, native dates, seals and cursor guards."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from email.utils import format_datetime

import httpx
import pytest
from webapp.history_capture.capture import CaptureStoppedError, Client, flow, read, seal, settings


def response(payload, *, count=100, stamp=None):
    return httpx.Response(
        200,
        json={"data": payload},
        headers={
            "date": stamp or format_datetime(datetime.now(UTC)),
            "x-uw-daily-req-count": str(count),
            "x-uw-token-req-limit": "30000",
        },
    )


@pytest.mark.asyncio
async def test_budget_and_quota_reserve_stop_before_a_second_call():
    calls = []

    def handler(request):
        calls.append(request)
        return response([], count=29999)

    cfg = settings().model_copy(update={"spacing_seconds": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = Client(http, cfg)
        await client.get("/api/test", {})
        with pytest.raises(CaptureStoppedError, match="quota_reserve"):
            await client.get("/api/test", {})
        assert client.calls == len(calls) == 1


@pytest.mark.asyncio
async def test_request_limit_and_native_quota_date():
    cfg = settings().model_copy(update={"spacing_seconds": 0, "max_requests": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response([]))) as http:
        client = Client(http, cfg)
        await client.get("/api/test", {})
        with pytest.raises(CaptureStoppedError, match="request_budget"):
            await client.get("/api/test", {})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response([], stamp="Wed, 01 Jan 2020 12:00:00 GMT"))
    ) as http:
        with pytest.raises(CaptureStoppedError, match="unverified_quota"):
            await Client(http, cfg).get("/api/test", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403, 429, 500])
async def test_no_http_retry_or_sensitive_error_body(code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, text="secret-must-not-escape")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(CaptureStoppedError, match=f"http_{code}") as error:
            await Client(http, settings()).get("/api/test", {})
    assert len(calls) == 1 and "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_flow_overlap_deduplication_and_short_page_completeness():
    rows = [
        {"id": "a", "created_at": "2026-06-24T15:00:00+00:00"},
        {"id": "b", "created_at": "2026-06-24T14:59:00+00:00"},
    ]
    pages = [rows, [rows[1], {"id": "c", "created_at": "2026-06-24T14:58:00+00:00"}], []]

    def handler(request):
        return response(pages.pop(0))

    cfg = settings().model_copy(update={"spacing_seconds": 0, "flow_page_limit": 2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await flow(Client(http, cfg), "NFLX", date(2026, 6, 24))
    assert result["complete"] and len(result["payload"]["data"]) == 3 and len(result["pages"]) == 3


@pytest.mark.asyncio
async def test_flow_stall_is_incomplete_not_quiet_zero():
    rows = [
        {"id": "a", "created_at": "2026-06-24T15:00:00+00:00"},
        {"id": "b", "created_at": "2026-06-24T15:00:00+00:00"},
    ]
    cfg = settings().model_copy(update={"spacing_seconds": 0, "flow_page_limit": 2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response(rows))) as http:
        result = await flow(Client(http, cfg), "NFLX", date(2026, 6, 24))
    assert not result["complete"] and result["stop_reason"] == "cursor_stall"
    assert len(result["payload"]["data"]) == 2


def test_seal_checksum_and_overwrite_protection(tmp_path):
    path = tmp_path / "packet.json.gz"
    seal(path, {"payload": {"data": [{"id": 1}]}, "complete": True})
    assert read(path)["payload"]["data"] == [{"id": 1}]
    with pytest.raises(CaptureStoppedError, match="overwrite"):
        seal(path, {})
    body = json.loads(gzip.decompress(path.read_bytes()))
    body["complete"] = False
    path.write_bytes(gzip.compress(json.dumps(body).encode()))
    with pytest.raises(CaptureStoppedError, match="checksum"):
        read(path)


@pytest.mark.asyncio
async def test_corrected_option_chain_probe_route(monkeypatch):
    from webapp.event_calibration.probe import probe

    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-only")
    calls = []

    def handler(request):
        calls.append(request)
        if "/historic" in request.url.path:
            pytest.fail("stock /historic is not the option-chain route")
        if "option-chains" in request.url.path:
            assert request.url.params["greeks"] == "true"
        return response(
            [
                {
                    "date": request.url.params.get("date", "2026-06-24"),
                    "created_at": "2026-06-24T15:00:00+00:00",
                }
            ]
        )

    result = await probe(
        "NFLX", (date(2026, 6, 24), date(2026, 9, 18)), transport=httpx.MockTransport(handler)
    )
    assert result["requests"] == 8
    assert sum("option-chains" in r.url.path for r in calls) == 2
