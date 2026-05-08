"""Phase 3.4.3.2 tests for PriceActionProvider extension + ThetaData impl.

Pins:
  - PriceMovement DTO: frozen + extra=forbid + lookback_actual int
  - Protocol declares get_intraday_price_movement
  - NoOp returns None
  - NoOp still satisfies Protocol after extension
  - ThetaData _build_movement pure function:
    - empty bars → None
    - single bar in window → movement (open→close on same bar)
    - bars in window: earliest open vs latest close
    - bars filtered: only window-internal bars used
    - sort robustness: out-of-order bars handled
    - zero spot edge case → None (avoid div by zero)
    - signed move_pct (positive when spot rose, negative when fell)
  - ThetaData provider:
    - lookback <= 0 → None
    - cache key isolation: different ticker/lookback fetch separately
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from uoa_detector.providers.price_action import (
    NoOpPriceActionProvider,
    PriceActionProvider,
    PriceMovement,
)
from uoa_detector.sources.thetadata.providers.price_action import (
    ThetaDataPriceActionProvider,
    _build_movement,
)

# ---------------------------------------------------------------------------
# DTO shape
# ---------------------------------------------------------------------------


def test_price_movement_frozen() -> None:
    pm = PriceMovement(
        ticker="AAPL",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        spot_at=Decimal("150.50"),
        spot_lookback_ago=Decimal("150.00"),
        move_pct=0.00333,
        lookback_minutes_actual=30,
    )
    with pytest.raises(Exception):
        pm.ticker = "MSFT"  # type: ignore[misc]


def test_price_movement_extra_forbid() -> None:
    with pytest.raises(ValueError):
        PriceMovement(  # type: ignore[call-arg]
            ticker="AAPL",
            as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
            spot_at=Decimal("150"),
            spot_lookback_ago=Decimal("150"),
            move_pct=0.0,
            lookback_minutes_actual=30,
            unknown_field=1,
        )


def test_price_movement_signed_move_pct() -> None:
    """Spot rose → positive; fell → negative."""
    up = PriceMovement(
        ticker="A", as_of=datetime(2024, 1, 1, tzinfo=UTC),
        spot_at=Decimal("101"), spot_lookback_ago=Decimal("100"),
        move_pct=0.01, lookback_minutes_actual=30,
    )
    assert up.move_pct > 0
    down = PriceMovement(
        ticker="A", as_of=datetime(2024, 1, 1, tzinfo=UTC),
        spot_at=Decimal("99"), spot_lookback_ago=Decimal("100"),
        move_pct=-0.01, lookback_minutes_actual=30,
    )
    assert down.move_pct < 0


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_protocol_has_get_intraday_price_movement() -> None:
    assert hasattr(PriceActionProvider, "get_intraday_price_movement")


def test_noop_returns_none() -> None:
    p = NoOpPriceActionProvider()
    result = asyncio.run(p.get_intraday_price_movement(
        "AAPL",
        datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        30,
    ))
    assert result is None


def test_noop_satisfies_protocol() -> None:
    p = NoOpPriceActionProvider()
    assert isinstance(p, PriceActionProvider)


# ---------------------------------------------------------------------------
# _build_movement pure function
# ---------------------------------------------------------------------------


def _bar(
    ms_of_day: int, open_px: float, close_px: float, date_int: int = 20240115,
) -> list[Any]:
    """Build a positional ThetaData OHLC bar tuple."""
    return [
        ms_of_day,
        str(open_px),  # open
        str(open_px),  # high (unused)
        str(open_px),  # low (unused)
        str(close_px),  # close
        100,  # volume (unused)
        10,   # count (unused)
        date_int,
    ]


def _at(hh: int, mm: int) -> datetime:
    return datetime(2024, 1, 15, hh, mm, tzinfo=UTC)


def test_build_movement_empty_bars_returns_none() -> None:
    out = _build_movement(
        bars=[],
        ticker="AAPL",
        window_start=_at(15, 0),
        window_end=_at(15, 30),
        lookback_minutes=30,
    )
    assert out is None


def test_build_movement_single_bar_in_window_uses_open_to_close() -> None:
    # 15:00 UTC = 15*60 + 0 = 54_000_000 ms
    bar = _bar(ms_of_day=54_000_000, open_px=100.0, close_px=101.0)
    out = _build_movement(
        bars=[bar],
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is not None
    assert out.spot_lookback_ago == Decimal("100.0")
    assert out.spot_at == Decimal("101.0")
    assert out.move_pct == pytest.approx(0.01, rel=1e-3)


def test_build_movement_multiple_bars_earliest_open_latest_close() -> None:
    # 14:30 → 15:30 window. Bars at 14:30, 15:00, 15:30
    bars = [
        _bar(14 * 3_600_000 + 30 * 60_000, 100.0, 100.5),  # 14:30
        _bar(15 * 3_600_000 + 0 * 60_000, 100.5, 101.0),   # 15:00
        _bar(15 * 3_600_000 + 30 * 60_000, 101.0, 102.0),  # 15:30
    ]
    out = _build_movement(
        bars=bars,
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is not None
    # Earliest bar's open = 100.0; latest bar's close = 102.0
    assert out.spot_lookback_ago == Decimal("100.0")
    assert out.spot_at == Decimal("102.0")
    assert out.move_pct == pytest.approx(0.02, rel=1e-3)


def test_build_movement_filters_bars_outside_window() -> None:
    """Bars before window_start or after window_end are ignored."""
    bars = [
        _bar(13 * 3_600_000, 50.0, 51.0),                    # before
        _bar(15 * 3_600_000 + 0 * 60_000, 100.0, 100.5),     # in
        _bar(16 * 3_600_000 + 30 * 60_000, 200.0, 250.0),    # after
    ]
    out = _build_movement(
        bars=bars,
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is not None
    # Only the middle bar in window; that bar's open and close
    assert out.spot_lookback_ago == Decimal("100.0")
    assert out.spot_at == Decimal("100.5")


def test_build_movement_sorts_out_of_order_bars() -> None:
    """Even if bars come out of order, earliest by ts wins."""
    bars = [
        _bar(15 * 3_600_000 + 30 * 60_000, 102.0, 103.0),  # 15:30 - LATEST
        _bar(15 * 3_600_000 + 0 * 60_000, 101.0, 102.0),   # 15:00
        _bar(14 * 3_600_000 + 30 * 60_000, 100.0, 101.0),  # 14:30 - EARLIEST
    ]
    out = _build_movement(
        bars=bars,
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is not None
    assert out.spot_lookback_ago == Decimal("100.0")  # 14:30 open
    assert out.spot_at == Decimal("103.0")             # 15:30 close


def test_build_movement_zero_spot_returns_none() -> None:
    """Defensive: if earliest bar's open is zero, avoid div/0."""
    bar = _bar(15 * 3_600_000, 0.0, 100.0)
    out = _build_movement(
        bars=[bar],
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is None


def test_build_movement_negative_move_pct_when_spot_falls() -> None:
    bar = _bar(15 * 3_600_000, 100.0, 99.0)
    out = _build_movement(
        bars=[bar],
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is not None
    assert out.move_pct < 0
    assert out.move_pct == pytest.approx(-0.01, rel=1e-3)


def test_build_movement_records_lookback_minutes_actual() -> None:
    bar = _bar(15 * 3_600_000, 100.0, 101.0)
    out = _build_movement(
        bars=[bar],
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=42,  # arbitrary number
    )
    assert out is not None
    assert out.lookback_minutes_actual == 42


def test_build_movement_skips_malformed_bars() -> None:
    """A bar that fails to parse is dropped; valid bars continue."""
    bars: list[list[Any]] = [
        ["not", "a", "bar"],  # too short
        _bar(15 * 3_600_000, 100.0, 101.0),
        [99 * 3_600_000, "garbage", "x", "y", "z", 1, 1, 20240115],
    ]
    out = _build_movement(
        bars=bars,
        ticker="AAPL",
        window_start=_at(14, 30),
        window_end=_at(15, 30),
        lookback_minutes=60,
    )
    assert out is not None
    assert out.spot_lookback_ago == Decimal("100.0")


# ---------------------------------------------------------------------------
# ThetaData provider integration
# ---------------------------------------------------------------------------


def _provider_with_rows(rows: list[list[Any]]) -> ThetaDataPriceActionProvider:
    fake = MagicMock()
    fake.request_json = AsyncMock(return_value={"response": rows})
    settings = MagicMock(intraday_cache_ttl_seconds=60)
    return ThetaDataPriceActionProvider(client=fake, settings=settings)


@pytest.mark.asyncio
async def test_provider_lookback_zero_returns_none() -> None:
    p = _provider_with_rows([_bar(15 * 3_600_000, 100.0, 101.0)])
    out = await p.get_intraday_price_movement(
        "AAPL", _at(15, 30), 0,
    )
    assert out is None


@pytest.mark.asyncio
async def test_provider_lookback_negative_returns_none() -> None:
    p = _provider_with_rows([_bar(15 * 3_600_000, 100.0, 101.0)])
    out = await p.get_intraday_price_movement(
        "AAPL", _at(15, 30), -1,
    )
    assert out is None


@pytest.mark.asyncio
async def test_provider_caches_per_ticker_lookback_combo() -> None:
    """Same (ticker, lookback) → cached; different → separate fetches."""
    rows = [_bar(15 * 3_600_000, 100.0, 101.0)]
    p = _provider_with_rows(rows)
    await p.get_intraday_price_movement("AAPL", _at(15, 30), 30)
    await p.get_intraday_price_movement("AAPL", _at(15, 30), 30)  # cached
    assert p._client.request_json.await_count == 1
    await p.get_intraday_price_movement("AAPL", _at(15, 30), 60)  # diff lookback
    assert p._client.request_json.await_count == 2
    await p.get_intraday_price_movement("MSFT", _at(15, 30), 30)  # diff ticker
    assert p._client.request_json.await_count == 3


@pytest.mark.asyncio
async def test_provider_returns_movement_for_real_window() -> None:
    rows = [
        _bar(14 * 3_600_000 + 30 * 60_000, 100.0, 100.5),  # 14:30
        _bar(15 * 3_600_000 + 30 * 60_000, 101.0, 102.0),  # 15:30
    ]
    p = _provider_with_rows(rows)
    out = await p.get_intraday_price_movement("AAPL", _at(15, 30), 60)
    assert out is not None
    assert out.ticker == "AAPL"
    assert out.spot_lookback_ago == Decimal("100.0")
    assert out.spot_at == Decimal("102.0")
    assert out.move_pct == pytest.approx(0.02, rel=1e-3)
