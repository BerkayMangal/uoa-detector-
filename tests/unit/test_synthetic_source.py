"""Unit tests for ``SyntheticFlowSource``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import OptionsPrint
from uoa_detector.sources.base import FlowDataSource
from uoa_detector.sources.synthetic import SyntheticFlowSource


def _make_print(idx: int) -> OptionsPrint:
    ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC) + timedelta(seconds=idx)
    return OptionsPrint(
        event_id=f"evt-{idx}",
        timestamp=ts,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=ts.date() + timedelta(days=14),
        dte=14,
        spot_price=Decimal("198"),
        premium_paid=Decimal("250000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.45,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1500,
        source_agreement=single_source_agreement("synthetic", "CBOE"),
    )


@pytest.mark.asyncio
async def test_emits_scripted_events_in_order() -> None:
    events = [_make_print(i) for i in range(5)]
    src = SyntheticFlowSource(events)

    seen: list[str] = []
    async for ev in src.stream():
        seen.append(ev.event_id)

    assert seen == ["evt-0", "evt-1", "evt-2", "evt-3", "evt-4"]


@pytest.mark.asyncio
async def test_close_short_circuits_remaining_events() -> None:
    events = [_make_print(i) for i in range(5)]
    src = SyntheticFlowSource(events)

    seen: list[str] = []
    async for ev in src.stream():
        seen.append(ev.event_id)
        if len(seen) == 2:
            await src.close()

    # After close, no further events from a fresh stream call.
    fresh: list[str] = []
    async for ev in src.stream():
        fresh.append(ev.event_id)
    assert fresh == []
    assert seen == ["evt-0", "evt-1"]


@pytest.mark.asyncio
async def test_empty_source_yields_nothing() -> None:
    src = SyntheticFlowSource([])
    out = [ev async for ev in src.stream()]
    assert out == []


def test_implements_protocol_structurally() -> None:
    src = SyntheticFlowSource([])
    assert isinstance(src, FlowDataSource)


@pytest.mark.asyncio
async def test_inter_event_delay_is_honored() -> None:
    """A small delay must produce strictly increasing wall-clock spacing."""
    import time

    events = [_make_print(i) for i in range(3)]
    src = SyntheticFlowSource(events, inter_event_delay=0.01)

    timestamps: list[float] = []
    async for _ in src.stream():
        timestamps.append(time.monotonic())

    # 3 events with 10ms delay each → at least ~20ms total span.
    assert timestamps[-1] - timestamps[0] >= 0.015
