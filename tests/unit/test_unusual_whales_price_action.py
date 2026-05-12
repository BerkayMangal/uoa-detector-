"""Phase 3.3.8.2 tests for ``UnusualWhalesPriceActionProvider``.

Pins:
  - Protocol conformance (runtime_checkable isinstance check)
  - Happy-path canned response → PriceMovement DTO with signed
    move_pct over the requested window
  - Cache hit on second identical call (one fetch, two reads)
  - Cache miss when ttl=0 (every call fetches)
  - Empty / malformed response → None, no crash
  - lookback_minutes <= 0 short-circuits before any HTTP
  - Window entirely outside the returned bars → None
  - URL path + params match the documented UW endpoint shape
  - ET trading-date conversion when ``at`` is in UTC

All tests use a ``_FakeClient`` that intercepts request_json and
returns canned responses — no real network. The smoke integration
test (updated in Phase 3.3.8.3) hits the real UW endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.providers.price_action import PriceActionProvider
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)

# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Captures (path, params) and returns scripted responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, Any] = {}

    def stub(self, path: str, response: Any) -> None:
        self.responses[path] = response

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> Any:
        del method
        self.calls.append((path, params))
        return self.responses.get(path, {"data": []})


def _settings(*, ttl_seconds: int = 60) -> UnusualWhalesSettings:
    return UnusualWhalesSettings(
        cache_ttl=UnusualWhalesProviderCacheTTL(
            intraday_price_seconds=ttl_seconds,
        ),
    )


def _bar(
    *,
    start_iso: str,
    open_: float,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: int = 1000,
) -> dict[str, Any]:
    """Build a UW-shaped OHLC bar dict."""
    return {
        "start_time": start_iso,
        "end_time": start_iso,  # endpoint also publishes; not used by parser
        "open": open_,
        "high": high if high is not None else max(open_, close),
        "low": low if low is not None else min(open_, close),
        "close": close,
        "volume": volume,
        "total_volume": volume,
        "market_time": "po",
    }


# ===========================================================================
# Protocol conformance
# ===========================================================================


def test_price_action_implements_protocol() -> None:
    """Runtime-checkable Protocol conformance."""
    client = _FakeClient()
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, PriceActionProvider)


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_returns_movement_for_window_covered_by_bars() -> None:
    """Bars cover the window → spot_lookback_ago/spot_at picked from edges."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                _bar(start_iso="2024-01-15T15:00:00+00:00", open_=190.0, close=190.5),
                _bar(start_iso="2024-01-15T15:15:00+00:00", open_=191.0, close=191.5),
                _bar(start_iso="2024-01-15T15:29:00+00:00", open_=191.8, close=192.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        ticker="AAPL",
        at=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        lookback_minutes=30,
    )
    assert out is not None
    assert out.ticker == "AAPL"
    assert out.spot_lookback_ago == Decimal("190.0")  # earliest in-window open
    assert out.spot_at == Decimal("192.0")             # latest in-window close
    # (192.0 - 190.0) / 190.0 ≈ 0.01053
    assert out.move_pct == pytest.approx(0.01053, abs=1e-4)
    assert out.lookback_minutes_actual == 30


@pytest.mark.asyncio
async def test_url_uses_candle_size_and_et_date() -> None:
    """Path includes /ohlc/1m; params carry the ET trading date."""
    client = _FakeClient()
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    # 03:00 UTC on 16 Jan = 22:00 ET on 15 Jan → trading date is 2024-01-15
    await provider.get_intraday_price_movement(
        ticker="aapl",
        at=datetime(2024, 1, 16, 3, 0, tzinfo=UTC),
        lookback_minutes=30,
    )
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == "/api/stock/AAPL/ohlc/1m"
    assert params == {"date": "2024-01-15"}


# ===========================================================================
# Cache behaviour
# ===========================================================================


@pytest.mark.asyncio
async def test_cache_hit_on_second_call_same_key() -> None:
    """Same (ticker, date, lookback) → one HTTP request, two reads."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                _bar(start_iso="2024-01-15T15:00:00+00:00", open_=190.0, close=192.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(ttl_seconds=300),
    )
    at = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    out1 = await provider.get_intraday_price_movement("AAPL", at, 30)
    out2 = await provider.get_intraday_price_movement("AAPL", at, 30)
    assert out1 is not None and out2 is not None
    assert len(client.calls) == 1  # only one fetch despite two calls


@pytest.mark.asyncio
async def test_cache_disabled_when_ttl_zero() -> None:
    """ttl=0 → every call hits the API."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {"data": [_bar(start_iso="2024-01-15T15:00:00+00:00", open_=1.0, close=1.0)]},
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(ttl_seconds=0),
    )
    at = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    await provider.get_intraday_price_movement("AAPL", at, 30)
    await provider.get_intraday_price_movement("AAPL", at, 30)
    assert len(client.calls) == 2


# ===========================================================================
# Empty / malformed
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_data_returns_none() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/ohlc/1m", {"data": []})
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), 30,
    )
    assert out is None


@pytest.mark.asyncio
async def test_non_dict_response_returns_none() -> None:
    """If response isn't a dict with 'data', provider yields no bars → None."""
    client = _FakeClient()
    client.stub("/api/stock/AAPL/ohlc/1m", [1, 2, 3])  # garbage shape
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), 30,
    )
    assert out is None


@pytest.mark.asyncio
async def test_malformed_row_skipped() -> None:
    """Rows missing required fields are skipped; valid rows still parsed."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                {"start_time": "2024-01-15T15:00:00+00:00"},  # missing open/close
                _bar(start_iso="2024-01-15T15:15:00+00:00", open_=190.0, close=192.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), 30,
    )
    assert out is not None
    assert out.spot_lookback_ago == Decimal("190.0")
    assert out.spot_at == Decimal("192.0")


@pytest.mark.asyncio
async def test_window_outside_returned_bars_returns_none() -> None:
    """Bars exist but none fall inside the [at-lookback, at] window."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                _bar(start_iso="2024-01-15T10:00:00+00:00", open_=1.0, close=1.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 20, 0, tzinfo=UTC), 30,
    )
    assert out is None


# ===========================================================================
# Edge cases
# ===========================================================================


@pytest.mark.asyncio
async def test_lookback_zero_short_circuits() -> None:
    client = _FakeClient()
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), 0,
    )
    assert out is None
    assert len(client.calls) == 0  # no HTTP


@pytest.mark.asyncio
async def test_lookback_negative_short_circuits() -> None:
    client = _FakeClient()
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), -1,
    )
    assert out is None
    assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_zero_lookback_open_returns_none() -> None:
    """Degenerate bar with open=0 → avoid div-by-zero, return None."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                _bar(start_iso="2024-01-15T15:15:00+00:00", open_=0.0, close=100.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), 30,
    )
    assert out is None


@pytest.mark.asyncio
async def test_negative_move_signed_correctly() -> None:
    """Spot fell → move_pct negative."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                _bar(start_iso="2024-01-15T15:00:00+00:00", open_=200.0, close=199.0),
                _bar(start_iso="2024-01-15T15:29:00+00:00", open_=199.5, close=198.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC), 30,
    )
    assert out is not None
    # (198.0 - 200.0) / 200.0 = -0.01
    assert out.move_pct == pytest.approx(-0.01, abs=1e-6)


@pytest.mark.asyncio
async def test_naive_timestamp_treated_as_et() -> None:
    """If UW drops the offset, naive timestamps are read as ET-naive."""
    client = _FakeClient()
    # 10:30 ET-naive = 15:30 UTC; window [14:00, 14:30] UTC = [09:00, 09:30] ET
    client.stub(
        "/api/stock/AAPL/ohlc/1m",
        {
            "data": [
                _bar(start_iso="2024-01-15T09:00:00", open_=100.0, close=100.0),
                _bar(start_iso="2024-01-15T09:25:00", open_=101.0, close=102.0),
            ],
        },
    )
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.get_intraday_price_movement(
        "AAPL", datetime(2024, 1, 15, 14, 30, tzinfo=UTC), 30,
    )
    assert out is not None
    # both bars in window (09:00 ET, 09:25 ET both < 09:30 ET = 14:30 UTC)
    assert out.spot_lookback_ago == Decimal("100.0")
    assert out.spot_at == Decimal("102.0")


@pytest.mark.asyncio
async def test_snapshot_at_returns_none() -> None:
    """snapshot_at is not implemented in this phase (M23 doesn't need it)."""
    client = _FakeClient()
    provider = UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    out = await provider.snapshot_at(
        "AAPL", datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    assert out is None
