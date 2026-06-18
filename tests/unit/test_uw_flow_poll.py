"""Unit tests for UnusualWhalesFlowPollSource (Phase 4 live REST poller)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.sources.unusual_whales.flow_poll import UnusualWhalesFlowPollSource

_NOW = datetime(2026, 6, 18, 19, 0, tzinfo=UTC)


def _record(rid: str, *, executed_at: str, premium: str = "50000") -> dict[str, object]:
    return {
        "id": rid,
        "executed_at": executed_at,
        "option_type": "call",
        "strike": "250",
        "expiry": "2026-07-17",
        "premium": premium,
        "price": "3.20",
        "nbbo_bid": "3.10",
        "nbbo_ask": "3.30",
        "underlying_price": "248.50",
        "implied_volatility": "0.42",
        "open_interest": 1200,
        "tags": ["ask_side", "bullish"],
        "exchange": "OPRA",
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


async def test_maps_record_to_raw_print() -> None:
    client = _FakeClient([_record("A", executed_at="2026-06-18T18:55:00Z")])
    source = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(25000),
        backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    source._sleep = _stop_after(1, source)  # type: ignore[assignment]
    prints = await _collect(source)
    assert len(prints) == 1
    p = prints[0]
    assert p.ticker == "TSLA"
    assert p.strike == Decimal("250")
    assert p.premium_paid == Decimal("50000")
    assert p.spot_price == Decimal("248.50")
    assert p.fill_side == "at_ask"
    assert p.open_interest == 1200
    assert p.source_event_id == "uw-A"


async def test_backfill_window_filters_old_prints() -> None:
    client = _FakeClient([
        _record("recent", executed_at="2026-06-18T18:55:00Z"),
        _record("old", executed_at="2026-06-18T18:30:00Z"),  # before cutoff
    ])
    source = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(0),
        backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    source._sleep = _stop_after(1, source)  # type: ignore[assignment]
    prints = await _collect(source)
    assert [p.source_event_id for p in prints] == ["uw-recent"]


async def test_min_premium_filter() -> None:
    client = _FakeClient([
        _record("big", executed_at="2026-06-18T18:55:00Z", premium="80000"),
        _record("small", executed_at="2026-06-18T18:55:00Z", premium="900"),
    ])
    source = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(25000),
        backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    source._sleep = _stop_after(1, source)  # type: ignore[assignment]
    prints = await _collect(source)
    assert [p.source_event_id for p in prints] == ["uw-big"]


async def test_dedupes_across_polls() -> None:
    client = _FakeClient([_record("A", executed_at="2026-06-18T18:55:00Z")])
    source = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(0),
        backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    source._sleep = _stop_after(3, source)  # type: ignore[assignment]
    prints = await _collect(source)
    # Same record returned on every poll, emitted exactly once.
    assert len(prints) == 1
    assert client.calls >= 2


async def test_malformed_record_dropped_not_fatal() -> None:
    client = _FakeClient([
        {"id": "bad"},  # missing required fields
        _record("good", executed_at="2026-06-18T18:55:00Z"),
    ])
    source = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(0),
        backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    source._sleep = _stop_after(1, source)  # type: ignore[assignment]
    prints = await _collect(source)
    assert [p.source_event_id for p in prints] == ["uw-good"]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["ask_side"], "at_ask"),
        (["bid_side"], "at_bid"),
        (["bullish"], "unknown"),
    ],
)
async def test_fill_side_from_tags(tags: list[str], expected: str) -> None:
    rec = _record("t", executed_at="2026-06-18T18:55:00Z")
    rec["tags"] = tags
    client = _FakeClient([rec])
    source = UnusualWhalesFlowPollSource(
        client, ["TSLA"], min_premium=Decimal(0),
        backfill=timedelta(minutes=10), now=lambda: _NOW,
    )
    source._sleep = _stop_after(1, source)  # type: ignore[assignment]
    prints = await _collect(source)
    assert prints[0].fill_side == expected
