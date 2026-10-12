"""Phase 5.27: the flow poll source follows a ticker list that changes while it runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from uoa_detector.sources.unusual_whales.flow_poll import UnusualWhalesFlowPollSource

_NOW = datetime(2026, 6, 18, 19, 0, tzinfo=UTC)  # 15:00 ET, market open


def _alert(chain: str, created_at: str) -> dict[str, object]:
    return {
        "option_chain": chain, "type": "call", "strike": "250", "expiry": "2026-07-17",
        "total_premium": "50000", "price": "3.20", "bid": None, "ask": None,
        "underlying_price": "248.50", "iv_end": "0.42", "open_interest": 1200,
        "total_ask_side_prem": "40000", "total_bid_side_prem": "10000", "has_sweep": False,
        "alert_rule": "RepeatedHits", "sector": "Tech", "created_at": created_at,
    }


class _PerTickerClient:
    def __init__(self, data: dict[str, list[dict[str, object]]]) -> None:
        self.data = data
        self.paths: list[str] = []

    async def request_json(self, path: str) -> dict[str, object]:
        self.paths.append(path)
        ticker = path.split("/")[3]
        return {"data": self.data.get(ticker, [])}


def _run(src: UnusualWhalesFlowPollSource, passes: int) -> object:
    state = {"n": 0}

    async def _sleep(_s: float) -> None:
        state["n"] += 1
        if state["n"] >= passes:
            src._closed = True

    src._sleep = _sleep  # type: ignore[assignment]
    return src


async def _collect(src: UnusualWhalesFlowPollSource) -> list[object]:
    return [p async for p in src.stream()]


async def test_a_ticker_list_provider_is_read_on_every_pass() -> None:
    client = _PerTickerClient({})
    lists = [["TSLA"], ["TSLA", "nvda"]]
    calls = {"n": 0}

    def provider() -> list[str]:
        out = lists[min(calls["n"], 1)]
        calls["n"] += 1
        return out

    src = UnusualWhalesFlowPollSource(client, provider, backfill=timedelta(minutes=10), now=lambda: _NOW)  # type: ignore[arg-type]
    _run(src, passes=2)
    await _collect(src)
    assert client.paths == [
        "/api/stock/TSLA/flow-alerts",
        "/api/stock/TSLA/flow-alerts", "/api/stock/NVDA/flow-alerts",
    ]


async def test_a_name_added_later_gets_the_backfill_window_not_its_whole_backlog() -> None:
    client = _PerTickerClient({
        "NVDA": [_alert("NVDA-old", "2026-06-18T18:20:00Z"), _alert("NVDA-new", "2026-06-18T18:55:00Z")],
    })
    lists = [["TSLA"], ["TSLA", "NVDA"]]
    calls = {"n": 0}

    def provider() -> list[str]:
        out = lists[min(calls["n"], 1)]
        calls["n"] += 1
        return out

    src = UnusualWhalesFlowPollSource(
        client, provider, min_premium=Decimal(0), backfill=timedelta(minutes=10), now=lambda: _NOW,  # type: ignore[arg-type]
    )
    _run(src, passes=2)
    prints = await _collect(src)
    assert [p.source_event_id for p in prints] == ["uw-NVDA-NVDA-new-2026-06-18T18:55:00Z"]  # type: ignore[attr-defined]


async def test_a_name_polled_before_is_not_cut_off_again() -> None:
    client = _PerTickerClient({"TSLA": [_alert("TSLA-a", "2026-06-18T18:55:00Z")]})
    src = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(0), backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    _run(src, passes=3)
    prints = await _collect(src)
    assert len(prints) == 1  # emitted once, then recognised as seen


async def test_a_failing_provider_keeps_the_previous_list_and_the_stream_alive() -> None:
    client = _PerTickerClient({})
    calls = {"n": 0}

    def provider() -> list[str]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("database unavailable")
        return ["TSLA"]

    src = UnusualWhalesFlowPollSource(client, provider, backfill=timedelta(minutes=10), now=lambda: _NOW)  # type: ignore[arg-type]
    _run(src, passes=3)
    await _collect(src)
    assert client.paths == ["/api/stock/TSLA/flow-alerts"] * 3


async def test_blanks_and_duplicates_in_a_provider_list_are_dropped() -> None:
    client = _PerTickerClient({})
    src = UnusualWhalesFlowPollSource(client, lambda: ["tsla", " ", "TSLA", ""], now=lambda: _NOW)  # type: ignore[arg-type]
    _run(src, passes=1)
    await _collect(src)
    assert client.paths == ["/api/stock/TSLA/flow-alerts"]
