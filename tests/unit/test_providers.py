"""Tests for ``uoa_detector.providers`` — the typed Phase-3 data dependency layer.

Coverage:
  - Protocol structural conformance for each NoOp / concrete provider.
  - NoOp fallback semantics: returns ``None`` / empty so the pipeline can
    run end-to-end without any real data.
  - In-memory & CSV implementations of ``MedianTradeSizeProvider`` and
    in-memory ``SectorMapProvider`` work as documented.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from uoa_detector.providers import (
    CatalystCalendarProvider,
    CSVMedianTradeSizeProvider,
    DarkPoolPrintProvider,
    DealerPositioningProvider,
    InMemoryMedianTradeSizeProvider,
    InMemorySectorMapProvider,
    IVHistoryProvider,
    MedianTradeSizeProvider,
    NoOpCatalystCalendarProvider,
    NoOpDarkPoolPrintProvider,
    NoOpDealerPositioningProvider,
    NoOpIVHistoryProvider,
    NoOpMedianTradeSizeProvider,
    NoOpOpenInterestProvider,
    NoOpPeerFlowProvider,
    NoOpPriceActionProvider,
    NoOpSectorMapProvider,
    OpenInterestProvider,
    PeerFlowProvider,
    PriceActionProvider,
    SectorMapProvider,
)

_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Structural Protocol conformance
# ---------------------------------------------------------------------------


def test_noop_median_provider_satisfies_protocol() -> None:
    assert isinstance(NoOpMedianTradeSizeProvider(), MedianTradeSizeProvider)


def test_in_memory_median_provider_satisfies_protocol() -> None:
    p = InMemoryMedianTradeSizeProvider({"AAPL": Decimal("100")})
    assert isinstance(p, MedianTradeSizeProvider)


def test_csv_median_provider_satisfies_protocol(tmp_path: Path) -> None:
    csv_path = tmp_path / "medians.csv"
    csv_path.write_text("ticker,median_premium_usd_30d\nAAPL,1.0\n")
    p = CSVMedianTradeSizeProvider(csv_path)
    assert isinstance(p, MedianTradeSizeProvider)


def test_noop_dealer_positioning_satisfies_protocol() -> None:
    assert isinstance(NoOpDealerPositioningProvider(), DealerPositioningProvider)


def test_noop_catalyst_calendar_satisfies_protocol() -> None:
    assert isinstance(NoOpCatalystCalendarProvider(), CatalystCalendarProvider)


def test_noop_price_action_satisfies_protocol() -> None:
    assert isinstance(NoOpPriceActionProvider(), PriceActionProvider)


def test_noop_iv_history_satisfies_protocol() -> None:
    assert isinstance(NoOpIVHistoryProvider(), IVHistoryProvider)


def test_noop_sector_map_satisfies_protocol() -> None:
    assert isinstance(NoOpSectorMapProvider(), SectorMapProvider)


def test_in_memory_sector_map_satisfies_protocol() -> None:
    assert isinstance(InMemorySectorMapProvider({}), SectorMapProvider)


def test_noop_peer_flow_satisfies_protocol() -> None:
    assert isinstance(NoOpPeerFlowProvider(), PeerFlowProvider)


def test_noop_dark_pool_satisfies_protocol() -> None:
    assert isinstance(NoOpDarkPoolPrintProvider(), DarkPoolPrintProvider)


def test_noop_open_interest_satisfies_protocol() -> None:
    assert isinstance(NoOpOpenInterestProvider(), OpenInterestProvider)


# ---------------------------------------------------------------------------
# NoOp semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_median_returns_none() -> None:
    p = NoOpMedianTradeSizeProvider()
    assert await p.median_premium("AAPL", 30) is None


@pytest.mark.asyncio
async def test_noop_dealer_positioning_returns_none() -> None:
    p = NoOpDealerPositioningProvider()
    assert await p.net_gamma_at("AAPL", Decimal("200"), _TS) is None


@pytest.mark.asyncio
async def test_noop_catalyst_calendar_returns_none() -> None:
    p = NoOpCatalystCalendarProvider()
    assert await p.next_catalyst("AAPL", _TS) is None


@pytest.mark.asyncio
async def test_noop_price_action_returns_none() -> None:
    p = NoOpPriceActionProvider()
    assert await p.snapshot_at("AAPL", _TS) is None


@pytest.mark.asyncio
async def test_noop_iv_history_returns_none() -> None:
    p = NoOpIVHistoryProvider()
    out = await p.iv_rank_at(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        option_type="call",
        at=_TS,
    )
    assert out is None


@pytest.mark.asyncio
async def test_noop_sector_map_returns_none_and_empty() -> None:
    p = NoOpSectorMapProvider()
    assert await p.sector_of("AAPL") is None
    assert tuple(await p.peers_of("AAPL")) == ()


@pytest.mark.asyncio
async def test_noop_peer_flow_returns_empty() -> None:
    p = NoOpPeerFlowProvider()
    out = await p.recent_flow(("AAPL",), _TS, timedelta(minutes=30))
    assert tuple(out) == ()


@pytest.mark.asyncio
async def test_noop_dark_pool_returns_empty() -> None:
    p = NoOpDarkPoolPrintProvider()
    out = await p.recent_prints("AAPL", _TS, timedelta(minutes=30))
    assert tuple(out) == ()


@pytest.mark.asyncio
async def test_noop_open_interest_at_and_next_day_return_none() -> None:
    p = NoOpOpenInterestProvider()
    assert await p.at(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        option_type="call",
        when=_TS,
    ) is None
    assert await p.next_day(
        ticker="AAPL",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        option_type="call",
        trade_date=date(2025, 6, 11),
    ) is None


# ---------------------------------------------------------------------------
# InMemory / CSV concrete providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_median_returns_value_for_known_ticker() -> None:
    p = InMemoryMedianTradeSizeProvider({"AAPL": Decimal("4500.00")})
    assert await p.median_premium("AAPL", 30) == Decimal("4500.00")


@pytest.mark.asyncio
async def test_in_memory_median_returns_none_for_unknown_ticker() -> None:
    p = InMemoryMedianTradeSizeProvider({"AAPL": Decimal("4500")})
    assert await p.median_premium("MSFT", 30) is None


@pytest.mark.asyncio
async def test_csv_median_provider_loads_stub_file() -> None:
    """The shipped data/medians.csv has 5 tickers; loader picks them up."""
    csv_path = Path(__file__).parent.parent.parent / "data" / "medians.csv"
    p = CSVMedianTradeSizeProvider(csv_path)
    assert await p.median_premium("AAPL", 30) == Decimal("4500.00")
    assert await p.median_premium("NVDA", 30) == Decimal("7500.00")
    assert await p.median_premium("UNKNOWN", 30) is None


@pytest.mark.asyncio
async def test_csv_median_provider_loads_lazily(tmp_path: Path) -> None:
    """The CSV is read on the first ``median_premium`` call, not at construction."""
    csv_path = tmp_path / "missing.csv"
    p = CSVMedianTradeSizeProvider(csv_path)
    # Construction must not raise even if the file doesn't exist yet.
    # The error surfaces on first lookup.
    csv_path.write_text("ticker,median_premium_usd_30d\nMETA,7000.00\n")
    assert await p.median_premium("META", 30) == Decimal("7000.00")


@pytest.mark.asyncio
async def test_in_memory_sector_map_lookups() -> None:
    p = InMemorySectorMapProvider(
        {
            "AAPL": "tech",
            "MSFT": "tech",
            "NVDA": "tech",
            "JPM": "financial",
        },
    )
    assert await p.sector_of("AAPL") == "tech"
    assert await p.sector_of("UNKNOWN") is None
    peers_of_aapl = set(await p.peers_of("AAPL"))
    assert peers_of_aapl == {"MSFT", "NVDA"}  # tech sector minus AAPL itself


@pytest.mark.asyncio
async def test_in_memory_sector_map_unknown_ticker_has_no_peers() -> None:
    p = InMemorySectorMapProvider({"AAPL": "tech"})
    assert tuple(await p.peers_of("UNKNOWN")) == ()
