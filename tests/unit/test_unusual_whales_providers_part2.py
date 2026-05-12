"""Phase 3.3.3.5 tests for the second three UW providers.

  - UnusualWhalesCatalystCalendarProvider
  - UnusualWhalesOpenInterestProvider (with at + next_day)
  - UnusualWhalesSectorMapProvider + UnusualWhalesPeerFlowProvider

Pins:
  - Protocol conformance for each
  - Happy-path canned response → typed DTO
  - Cache hit on second identical call
  - Empty / malformed → None or empty seq
  - OI: next_day computes trade_date+1 and uses EOD endpoint
  - SectorMap: peers_of excludes the source ticker itself
  - PeerFlow: cache key is sorted-tickers-tuple (caller-order-stable)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.providers.catalyst_calendar import CatalystCalendarProvider
from uoa_detector.providers.open_interest import OpenInterestProvider
from uoa_detector.providers.sector_map import (
    PeerFlowProvider,
    SectorMapProvider,
)
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)

# ---------------------------------------------------------------------------
# Fake client (same shape as part 1)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def stub(self, path: str, response: dict[str, Any]) -> None:
        self.responses[path] = response

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path in self.responses:
            return self.responses[path]
        return {"data": []}


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings(
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


# ===========================================================================
# CatalystCalendarProvider
# ===========================================================================


def test_catalyst_calendar_implements_protocol() -> None:
    client = _FakeClient()
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, CatalystCalendarProvider)


@pytest.mark.asyncio
async def test_catalyst_calendar_returns_next_event() -> None:
    """Phase 3.3.9.4: earnings endpoint feeds the primary catalyst stream."""
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {
            "data": [
                {"report_date": "2024-01-20", "report_time": "after-hours"},
                {"report_date": "2024-01-25", "report_time": "after-hours"},
            ],
        },
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 22, 0, 0, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    # Picks Jan 25; Jan 20 is before ``after``.
    assert event is not None
    assert event.kind == "earnings"
    assert event.when.date().isoformat() == "2024-01-25"


@pytest.mark.asyncio
async def test_catalyst_calendar_returns_none_when_no_future_event() -> None:
    """All three sources empty or past-only → next_catalyst returns None."""
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {"data": [{"report_date": "2023-12-01", "report_time": "after-hours"}]},
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is None


@pytest.mark.asyncio
async def test_catalyst_calendar_econ_event_falls_back_to_other() -> None:
    """Phase 3.3.9.4: econ-calendar events with no 'fed' keyword → other."""
    client = _FakeClient()
    client.stub(
        "/api/market/economic-calendar",
        {
            "data": [
                {"type": "report", "time": "2024-02-01T00:00:00Z",
                 "event": "Empire State manufacturing survey"},
            ],
        },
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is not None
    assert event.kind == "other"


@pytest.mark.asyncio
async def test_catalyst_calendar_caches() -> None:
    """Two next_catalyst calls → at most one fetch per endpoint."""
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {"data": [{"report_date": "2024-02-01", "report_time": "after-hours"}]},
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    await provider.next_catalyst("AAPL", after)
    await provider.next_catalyst("AAPL", after)
    # 3 endpoints, each fetched once across the two next_catalyst calls.
    paths = [call[0] for call in client.calls]
    assert paths.count("/api/earnings/AAPL") == 1
    assert paths.count("/api/market/fda-calendar") == 1
    assert paths.count("/api/market/economic-calendar") == 1


@pytest.mark.asyncio
async def test_catalyst_calendar_fda_event_filtered_by_ticker() -> None:
    """Phase 3.3.9.4: global FDA feed; provider filters by ticker."""
    client = _FakeClient()
    client.stub(
        "/api/market/fda-calendar",
        {
            "data": [
                {"ticker": "GERN", "event_type": "Top-line Data",
                 "target_date": "2024-02-15"},
                {"ticker": "AAPL", "event_type": "FDA approval",
                 "target_date": "2024-02-20"},
            ],
        },
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is not None
    assert event.kind == "fda"
    assert event.when.date().isoformat() == "2024-02-20"


@pytest.mark.asyncio
async def test_catalyst_calendar_fda_unparseable_target_falls_back_to_start() -> None:
    """Phase 3.3.9.4: text target_date ('2024-MID') → fall back to start_date."""
    client = _FakeClient()
    client.stub(
        "/api/market/fda-calendar",
        {
            "data": [
                {"ticker": "AAPL", "event_type": "FDA",
                 "target_date": "2024-MID",
                 "start_date": "2024-03-10"},
            ],
        },
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is not None
    assert event.when.date().isoformat() == "2024-03-10"


@pytest.mark.asyncio
async def test_catalyst_calendar_econ_fed_event_kind_is_fomc() -> None:
    """Phase 3.3.9.4: econ event mentioning 'Fed' → kind=fomc."""
    client = _FakeClient()
    client.stub(
        "/api/market/economic-calendar",
        {
            "data": [
                {"type": "fed-speech", "time": "2024-02-05T13:00:00Z",
                 "event": "Federal Reserve Chair Powell speech"},
            ],
        },
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is not None
    assert event.kind == "fomc"


@pytest.mark.asyncio
async def test_catalyst_calendar_earnings_premarket_time_maps_to_morning() -> None:
    """Phase 3.3.9.4: report_time='pre-market' → 13:30 UTC (~08:30 ET DST)."""
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {"data": [{"report_date": "2024-02-01", "report_time": "pre-market"}]},
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is not None
    assert event.when.hour == 13
    assert event.when.minute == 30


# ===========================================================================
# OpenInterestProvider
# ===========================================================================


def test_open_interest_implements_protocol() -> None:
    client = _FakeClient()
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, OpenInterestProvider)


@pytest.mark.asyncio
async def test_open_interest_at_returns_snapshot() -> None:
    client = _FakeClient()
    expected_path = "/api/option-contract/AAPL240216C00150000/open-interest"
    client.stub(
        expected_path,
        {"data": {"as_of": "2024-01-15T15:30:00Z",
                   "open_interest": 12345}},
    )
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    snap = await provider.at(
        ticker="AAPL", strike=Decimal("150.00"),
        expiry=date(2024, 2, 16), option_type="call",
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 12345
    assert snap.ticker == "AAPL"


@pytest.mark.asyncio
async def test_open_interest_at_handles_list_data_shape() -> None:
    """Some UW endpoints return data as a list with one element."""
    client = _FakeClient()
    expected_path = "/api/option-contract/AAPL240216C00150000/open-interest"
    client.stub(
        expected_path,
        {"data": [{"as_of": "2024-01-15T15:30:00Z",
                    "open_interest": 999}]},
    )
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    snap = await provider.at(
        ticker="AAPL", strike=Decimal("150.00"),
        expiry=date(2024, 2, 16), option_type="call",
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 999


@pytest.mark.asyncio
async def test_open_interest_next_day_uses_trade_date_plus_one() -> None:
    client = _FakeClient()
    expected_path = "/api/option-contract/AAPL240216C00150000/open-interest/eod"
    client.stub(
        expected_path,
        {"data": {"as_of": "2024-01-16T21:00:00Z",
                   "open_interest": 22000}},
    )
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    snap = await provider.next_day(
        ticker="AAPL", strike=Decimal("150.00"),
        expiry=date(2024, 2, 16), option_type="call",
        trade_date=date(2024, 1, 15),
    )
    assert snap is not None
    assert snap.open_interest == 22000
    # Verify the request used date=2024-01-16 (trade_date+1)
    assert len(client.calls) == 1
    _path, params = client.calls[0]
    assert params is not None
    assert params["date"] == "2024-01-16"


@pytest.mark.asyncio
async def test_open_interest_returns_none_on_empty() -> None:
    client = _FakeClient()
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    snap = await provider.at(
        ticker="AAPL", strike=Decimal("150.00"),
        expiry=date(2024, 2, 16), option_type="call",
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    assert snap is None


@pytest.mark.asyncio
async def test_open_interest_at_and_next_day_use_separate_caches() -> None:
    """Same symbol: at() and next_day() cache independently."""
    client = _FakeClient()
    client.stub(
        "/api/option-contract/AAPL240216C00150000/open-interest",
        {"data": {"as_of": "2024-01-15T15:30:00Z", "open_interest": 100}},
    )
    client.stub(
        "/api/option-contract/AAPL240216C00150000/open-interest/eod",
        {"data": {"as_of": "2024-01-16T21:00:00Z", "open_interest": 200}},
    )
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    args: dict[str, Any] = {
        "ticker": "AAPL", "strike": Decimal("150.00"),
        "expiry": date(2024, 2, 16), "option_type": "call",
    }
    await provider.at(
        **args,
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    await provider.next_day(**args, trade_date=date(2024, 1, 15))
    # Two distinct fetches (different endpoints)
    assert len(client.calls) == 2
    # Repeat both — neither hits the network
    await provider.at(
        **args,
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    await provider.next_day(**args, trade_date=date(2024, 1, 15))
    assert len(client.calls) == 2


# ===========================================================================
# SectorMapProvider
# ===========================================================================


def test_sector_map_implements_protocol() -> None:
    client = _FakeClient()
    provider = UnusualWhalesSectorMapProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, SectorMapProvider)


@pytest.mark.asyncio
async def test_sector_map_returns_sector() -> None:
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/info",
        {"data": {"sector": "Technology",
                   "peers": ["MSFT", "GOOGL", "AAPL"]}},
    )
    provider = UnusualWhalesSectorMapProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    sector = await provider.sector_of("AAPL")
    assert sector == "Technology"


@pytest.mark.asyncio
async def test_sector_map_peers_excludes_self() -> None:
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/info",
        {"data": {"sector": "Technology",
                   "peers": ["MSFT", "GOOGL", "AAPL", "aapl"]}},
    )
    provider = UnusualWhalesSectorMapProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    peers = await provider.peers_of("AAPL")
    # AAPL excluded (case-insensitive)
    assert "AAPL" not in peers
    assert "MSFT" in peers
    assert "GOOGL" in peers


@pytest.mark.asyncio
async def test_sector_map_returns_none_when_unknown() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", {"data": {}})
    provider = UnusualWhalesSectorMapProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    sector = await provider.sector_of("AAPL")
    assert sector is None


@pytest.mark.asyncio
async def test_sector_map_one_fetch_serves_sector_and_peers() -> None:
    """sector_of and peers_of share a single fetch via the cache."""
    client = _FakeClient()
    client.stub(
        "/api/stock/AAPL/info",
        {"data": {"sector": "Tech", "peers": ["MSFT"]}},
    )
    provider = UnusualWhalesSectorMapProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    await provider.sector_of("AAPL")
    await provider.peers_of("AAPL")
    assert len(client.calls) == 1


# ===========================================================================
# PeerFlowProvider
# ===========================================================================


def test_peer_flow_implements_protocol() -> None:
    client = _FakeClient()
    provider = UnusualWhalesPeerFlowProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, PeerFlowProvider)


@pytest.mark.asyncio
async def test_peer_flow_returns_events_in_window() -> None:
    client = _FakeClient()
    client.stub(
        "/api/option-flow/recent",
        {
            "data": [
                {"ticker": "MSFT",
                 "executed_at": "2024-01-15T14:30:00Z",
                 "side_classification": "bullish",
                 "label": "CONVEXITY_CLUSTER"},
                {"ticker": "GOOGL",
                 "executed_at": "2024-01-15T14:45:00Z",
                 "side_classification": "bearish"},
                # Outside window:
                {"ticker": "MSFT",
                 "executed_at": "2024-01-15T13:00:00Z",
                 "side_classification": "bullish"},
            ],
        },
    )
    provider = UnusualWhalesPeerFlowProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    events = await provider.recent_flow(
        tickers=["MSFT", "GOOGL"],
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=45),
    )
    assert len(events) == 2
    assert {e.ticker for e in events} == {"MSFT", "GOOGL"}


@pytest.mark.asyncio
async def test_peer_flow_cache_key_caller_order_stable() -> None:
    """Same ticker set in different order → same cache entry."""
    client = _FakeClient()
    client.stub(
        "/api/option-flow/recent",
        {"data": []},
    )
    provider = UnusualWhalesPeerFlowProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    before = datetime(2024, 1, 15, 15, 0, tzinfo=UTC)
    await provider.recent_flow(
        tickers=["MSFT", "GOOGL"],
        before=before, window=timedelta(minutes=30),
    )
    await provider.recent_flow(
        tickers=["GOOGL", "MSFT"],  # different order
        before=before, window=timedelta(minutes=30),
    )
    # Single fetch — sorted-tuple key collides
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_peer_flow_empty_tickers_returns_empty() -> None:
    """Defensive: empty input returns empty without an HTTP call."""
    client = _FakeClient()
    provider = UnusualWhalesPeerFlowProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    events = await provider.recent_flow(
        tickers=[],
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=30),
    )
    assert events == ()
    assert client.calls == []


@pytest.mark.asyncio
async def test_peer_flow_unknown_direction_falls_back_to_neutral() -> None:
    client = _FakeClient()
    client.stub(
        "/api/option-flow/recent",
        {"data": [{"ticker": "MSFT",
                    "executed_at": "2024-01-15T14:30:00Z",
                    "side_classification": "wibble"}]},
    )
    provider = UnusualWhalesPeerFlowProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    events = await provider.recent_flow(
        tickers=["MSFT"],
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=60),
    )
    assert len(events) == 1
    assert events[0].direction == "neutral"
