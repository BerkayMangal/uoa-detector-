"""Unit tests for UnusualWhalesFlowPollSource (Phase 4 live REST poller).

Drives the curated ``/api/stock/{ticker}/flow-alerts`` shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.sources.unusual_whales.flow_poll import UnusualWhalesFlowPollSource

_NOW = datetime(2026, 6, 18, 19, 0, tzinfo=UTC)


def _alert(
    chain: str,
    *,
    created_at: str,
    premium: str = "50000",
    ask_prem: str = "40000",
    bid_prem: str = "10000",
    has_sweep: bool = False,
) -> dict[str, object]:
    return {
        "option_chain": chain,
        "type": "call",
        "strike": "250",
        "expiry": "2026-07-17",
        "total_premium": premium,
        "price": "3.20",
        "bid": None,
        "ask": None,
        "underlying_price": "248.50",
        "iv_end": "0.42",
        "open_interest": 1200,
        "total_ask_side_prem": ask_prem,
        "total_bid_side_prem": bid_prem,
        "has_sweep": has_sweep,
        "alert_rule": "RepeatedHits",
        "sector": "Consumer Cyclical",
        "created_at": created_at,
    }


class _FakeClient:
    def __init__(self, data: list[dict[str, object]]) -> None:
        self._data = data
        self.calls = 0

    async def request_json(self, path: str) -> dict[str, object]:
        self.calls += 1
        return {"data": self._data}


def _stop_after(n: int, source: UnusualWhalesFlowPollSource) -> object:
    state = {"passes": 0}

    async def _sleep(_seconds: float) -> None:
        state["passes"] += 1
        if state["passes"] >= n:
            source._closed = True

    return _sleep


async def _collect(source: UnusualWhalesFlowPollSource) -> list[object]:
    return [p async for p in source.stream()]


def _source(data: list[dict[str, object]], **kw: object) -> UnusualWhalesFlowPollSource:
    passes = int(kw.pop("_passes", 1))  # type: ignore[arg-type]
    src = UnusualWhalesFlowPollSource(
        _FakeClient(data), ["TSLA"],
        backfill=timedelta(minutes=10), now=lambda: _NOW, **kw,  # type: ignore[arg-type]
    )
    src._sleep = _stop_after(passes, src)  # type: ignore[assignment]
    return src


async def test_maps_alert_to_raw_print() -> None:
    src = _source([_alert("TSLA260717C00250000", created_at="2026-06-18T18:55:00Z")],
                  min_premium=Decimal(25000))
    prints = await _collect(src)
    assert len(prints) == 1
    p = prints[0]
    assert p.ticker == "TSLA"
    assert p.strike == Decimal("250")
    assert p.premium_paid == Decimal("50000")
    assert p.spot_price == Decimal("248.50")
    assert p.fill_side == "at_ask"  # ask-side premium dominant
    assert p.open_interest == 1200
    assert p.source_event_id == "uw-TSLA-TSLA260717C00250000-2026-06-18T18:55:00Z"
    assert "uw:RepeatedHits" in p.source_tags


async def test_backfill_window_filters_old_alerts() -> None:
    src = _source([
        _alert("CH-recent", created_at="2026-06-18T18:55:00Z"),
        _alert("CH-old", created_at="2026-06-18T18:30:00Z"),  # before cutoff
    ], min_premium=Decimal(0))
    prints = await _collect(src)
    assert [p.source_event_id.split("-")[2] for p in prints] == ["CH"]


async def test_min_premium_filter() -> None:
    src = _source([
        _alert("CH-big", created_at="2026-06-18T18:55:00Z", premium="80000"),
        _alert("CH-small", created_at="2026-06-18T18:55:00Z", premium="900"),
    ], min_premium=Decimal(25000))
    prints = await _collect(src)
    assert [p.premium_paid for p in prints] == [Decimal("80000")]


async def test_sweep_flag_maps_to_is_iso() -> None:
    src = _source([_alert("CH", created_at="2026-06-18T18:55:00Z", has_sweep=True)],
                  min_premium=Decimal(0))
    prints = await _collect(src)
    assert prints[0].is_iso is True
    assert "uw:sweep" in prints[0].source_tags


async def test_dedupes_across_polls() -> None:
    src = _source([_alert("CH", created_at="2026-06-18T18:55:00Z")],
                  min_premium=Decimal(0), _passes=3)
    prints = await _collect(src)
    assert len(prints) == 1
    assert src._client.calls >= 2  # type: ignore[attr-defined]


async def test_malformed_alert_dropped_not_fatal() -> None:
    src = _source([
        {"option_chain": "bad", "created_at": "2026-06-18T18:55:00Z"},  # missing fields
        _alert("CH-good", created_at="2026-06-18T18:55:00Z"),
    ], min_premium=Decimal(0))
    prints = await _collect(src)
    assert [p.source_event_id.split("-")[2] for p in prints] == ["CH"]


@pytest.mark.parametrize(
    ("ask_prem", "bid_prem", "expected"),
    [
        ("90000", "10000", "at_ask"),
        ("10000", "90000", "at_bid"),
        ("50000", "50000", "unknown"),
    ],
)
async def test_fill_side_from_side_premium(
    ask_prem: str, bid_prem: str, expected: str,
) -> None:
    src = _source(
        [_alert("CH", created_at="2026-06-18T18:55:00Z", ask_prem=ask_prem, bid_prem=bid_prem)],
        min_premium=Decimal(0),
    )
    prints = await _collect(src)
    assert prints[0].fill_side == expected
