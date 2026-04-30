"""Unit tests for ``SyntheticRawFlowSource`` and the ``to_raw_print`` helper."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.domain.events import OptionsPrint
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.sources.base import RawFlowSource
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def _ops_print(event_id: str = "abc-1", ticker: str = "AAPL") -> OptionsPrint:
    """Phase-1 style canonical print for testing the converter."""
    from uoa_detector.domain.agreement import single_source_agreement

    return OptionsPrint(
        event_id=event_id,
        timestamp=_TS,
        ticker=ticker,
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal("100"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        implied_volatility=0.45,
        open_interest=1500,
        source_agreement=single_source_agreement("legacy"),
    )


def _raw_print(source_id: str = "synthetic", suffix: str = "0") -> RawPrint:
    return RawPrint(
        source_id=source_id,
        source_event_id=f"{source_id}-{suffix}",
        timestamp=_TS,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal("100"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        implied_volatility=0.45,
        open_interest=1500,
    )


# ---------------------------------------------------------------------------
# to_raw_print converter
# ---------------------------------------------------------------------------

def test_to_raw_print_inherits_event_id_as_source_event_id() -> None:
    """Critical for scenario-override stage continuity: the OptionsPrint
    event_id becomes the RawPrint source_event_id, and single-source
    fusion preserves it on the canonical OptionsPrint.
    """
    p = _ops_print(event_id="scenario-step-3")
    raw = to_raw_print(p, source_id="synthetic")
    assert raw.source_event_id == "scenario-step-3"
    assert raw.source_id == "synthetic"


def test_to_raw_print_copies_all_fields() -> None:
    """RawPrint must contain every field the OptionsPrint had."""
    p = _ops_print()
    raw = to_raw_print(p)
    assert raw.timestamp == p.timestamp
    assert raw.ticker == p.ticker
    assert raw.option_type == p.option_type
    assert raw.strike == p.strike
    assert raw.expiry == p.expiry
    assert raw.dte == p.dte
    assert raw.spot_price == p.spot_price
    assert raw.premium_paid == p.premium_paid
    assert raw.option_price == p.option_price
    assert raw.bid == p.bid
    assert raw.ask == p.ask
    assert raw.fill_side == p.fill_side
    assert raw.exchange == p.exchange
    assert raw.is_iso == p.is_iso
    assert raw.implied_volatility == p.implied_volatility
    assert raw.open_interest == p.open_interest


def test_to_raw_print_default_source_id_is_synthetic() -> None:
    p = _ops_print()
    raw = to_raw_print(p)
    assert raw.source_id == "synthetic"


# ---------------------------------------------------------------------------
# SyntheticRawFlowSource
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthetic_raw_emits_events_in_order() -> None:
    src = SyntheticRawFlowSource(
        "synthetic",
        [_raw_print(suffix=str(i)) for i in range(3)],
    )
    out = [r async for r in src.stream()]
    assert len(out) == 3
    assert [r.source_event_id for r in out] == ["synthetic-0", "synthetic-1", "synthetic-2"]


@pytest.mark.asyncio
async def test_synthetic_raw_close_stops_streaming_mid_run() -> None:
    src = SyntheticRawFlowSource(
        "synthetic",
        [_raw_print(suffix=str(i)) for i in range(5)],
    )
    out: list[RawPrint] = []
    async for ev in src.stream():
        out.append(ev)
        if len(out) == 2:
            await src.close()
    assert len(out) == 2


@pytest.mark.asyncio
async def test_synthetic_raw_empty_stream_terminates() -> None:
    src = SyntheticRawFlowSource("synthetic", [])
    out = [r async for r in src.stream()]
    assert out == []


def test_synthetic_raw_satisfies_protocol() -> None:
    """Structural conformance to ``RawFlowSource``."""
    src = SyntheticRawFlowSource("synthetic", [])
    assert isinstance(src, RawFlowSource)


def test_source_id_attribute_is_publicly_readable() -> None:
    src = SyntheticRawFlowSource("polygon", [])
    assert src.source_id == "polygon"
