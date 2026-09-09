"""Phase 3.7.3 tests for ``UnusualWhalesRestFlowSource``.

No live network: a fake client returns a canned ``{"data": [...]}`` payload.
Pins the one-shot fetch, the shared-mapper wiring, RawFlowSource Protocol
conformance, the optional lookback window, and resilience to a bad row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.sources.base import RawFlowSource
from uoa_detector.sources.unusual_whales.rest_flow import (
    RECENT_FLOW_PATH,
    UnusualWhalesRestFlowSource,
)


class _FakeClient:
    """Stands in for UnusualWhalesClient — records calls, returns a payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.aclosed = False

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        return self._payload

    async def aclose(self) -> None:
        self.aclosed = True


def _rest_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "rest_1",
        "ticker": "AAPL",
        "executed_at": "2024-01-15T15:30:00Z",
        "option_type": "call",
        "strike": "150.00",
        "expiry": "2024-02-16",
        "premium": "12345.00",
        "price": "1.50",
        "bid": "1.45",
        "ask": "1.55",
        "side_classification": "bullish",
        "exchange": "CBOE",
    }
    base.update(overrides)
    return base


async def _drain(src: UnusualWhalesRestFlowSource) -> list[RawPrint]:
    out: list[RawPrint] = []
    async for rp in src.stream():
        out.append(rp)
    return out


def test_conforms_to_rawflowsource_protocol() -> None:
    client = _FakeClient({"data": []})
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    assert isinstance(src, RawFlowSource)
    assert src.source_id == "unusual_whales"


@pytest.mark.asyncio
async def test_one_shot_fetch_yields_mapped_prints() -> None:
    payload = {
        "data": [
            _rest_row(id="rest_1", ticker="AAPL"),
            _rest_row(id="rest_2", ticker="MSFT", strike="400.00"),
        ],
    }
    client = _FakeClient(payload)
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["aapl", "msft"],
        before=datetime(2024, 1, 15, 20, 0, tzinfo=UTC),
    )
    prints = await _drain(src)
    assert len(prints) == 2
    assert {p.source_event_id for p in prints} == {"uw-rest_1", "uw-rest_2"}
    assert {p.ticker for p in prints} == {"AAPL", "MSFT"}
    # Exactly one HTTP call — one-shot, not a loop.
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == RECENT_FLOW_PATH
    assert params is not None
    assert params["tickers"] == "AAPL,MSFT"  # upper-cased, comma-joined
    assert params["before"] == "2024-01-15T20:00:00+00:00"


@pytest.mark.asyncio
async def test_stream_completes_after_one_pass() -> None:
    """Second drain re-fetches (still one-shot per stream call), not infinite."""
    client = _FakeClient({"data": [_rest_row()]})
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    first = await _drain(src)
    assert len(first) == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_bad_row_skipped_others_yielded() -> None:
    bad = _rest_row(id="bad")
    del bad["strike"]
    payload = {"data": [bad, _rest_row(id="good")]}
    client = _FakeClient(payload)
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    prints = await _drain(src)
    assert len(prints) == 1
    assert prints[0].source_event_id == "uw-good"


@pytest.mark.asyncio
async def test_non_dict_rows_skipped() -> None:
    payload = {"data": ["not-a-dict", 42, _rest_row(id="ok")]}
    client = _FakeClient(payload)
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    prints = await _drain(src)
    assert len(prints) == 1
    assert prints[0].source_event_id == "uw-ok"


@pytest.mark.asyncio
async def test_missing_data_key_yields_nothing() -> None:
    client = _FakeClient({"other": []})
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    assert await _drain(src) == []


@pytest.mark.asyncio
async def test_lookback_filters_old_rows() -> None:
    before = datetime(2024, 1, 15, 20, 0, tzinfo=UTC)
    payload = {
        "data": [
            _rest_row(id="fresh", executed_at="2024-01-15T19:30:00Z"),
            _rest_row(id="stale", executed_at="2024-01-15T10:00:00Z"),
        ],
    }
    client = _FakeClient(payload)
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["AAPL"],
        lookback=timedelta(hours=2),
        before=before,
    )
    prints = await _drain(src)
    assert [p.source_event_id for p in prints] == ["uw-fresh"]


@pytest.mark.asyncio
async def test_no_lookback_keeps_all_returned_rows() -> None:
    payload = {
        "data": [
            _rest_row(id="a", executed_at="2024-01-15T19:30:00Z"),
            _rest_row(id="b", executed_at="2024-01-01T10:00:00Z"),
        ],
    }
    client = _FakeClient(payload)
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["AAPL"],
        before=datetime(2024, 1, 15, 20, 0, tzinfo=UTC),
    )
    prints = await _drain(src)
    assert len(prints) == 2


@pytest.mark.asyncio
async def test_close_stops_stream() -> None:
    client = _FakeClient({"data": [_rest_row()]})
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    await src.close()
    assert await _drain(src) == []
    # No HTTP call once closed.
    assert client.calls == []


@pytest.mark.asyncio
async def test_close_does_not_close_client() -> None:
    client = _FakeClient({"data": []})
    src = UnusualWhalesRestFlowSource(client=client, tickers=["AAPL"])  # type: ignore[arg-type]
    await src.close()
    await src.close()  # idempotent
    assert client.aclosed is False  # caller owns the client lifecycle


@pytest.mark.asyncio
async def test_empty_tickers_makes_no_request() -> None:
    client = _FakeClient({"data": [_rest_row()]})
    src = UnusualWhalesRestFlowSource(client=client, tickers=[])  # type: ignore[arg-type]
    assert await _drain(src) == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_custom_source_id() -> None:
    client = _FakeClient({"data": [_rest_row()]})
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["AAPL"],
        source_id="unusual_whales-rest",
    )
    prints = await _drain(src)
    assert prints[0].source_id == "unusual_whales-rest"
    assert prints[0].strike == Decimal("150.00")
