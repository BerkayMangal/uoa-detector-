"""Phase 3.3.3.5 tests for the second three UW providers.

  - UnusualWhalesCatalystCalendarProvider
  - UnusualWhalesOpenInterestProvider (with at + next_day)
  - UnusualWhalesSectorMapProvider + UnusualWhalesPeerFlowProvider

Pins:
  - Protocol conformance for each
  - Happy-path canned response → typed DTO
  - Cache hit on second identical call
  - Empty / malformed → None or empty seq
  - OI: next_day returns the first row after trade_date from /historic
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
    # Phase 3.9.8: live /api/earnings/{t} row shape (the retired
    # /api/stock/{t}/upcoming-events path returned 404).
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {
            "data": [
                {"source": "company", "report_date": "2024-05-02",
                 "report_time": "postmarket"},
                {"source": "company", "report_date": "2024-02-01",
                 "report_time": "postmarket"},
                {"source": "company", "report_date": "2023-11-02",
                 "report_time": "postmarket"},
            ],
        },
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 22, 0, 0, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    # Should pick the Feb 1 report (next event >= after); Nov 2 is before.
    assert event is not None
    assert event.kind == "earnings"
    assert event.title == "AAPL earnings 2024-02-01 (postmarket, company)"


@pytest.mark.asyncio
async def test_catalyst_calendar_returns_none_when_no_future_event() -> None:
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {"data": [{"source": "company", "report_date": "2023-11-02",
                    "report_time": "postmarket"}]},
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is None


@pytest.mark.asyncio
async def test_catalyst_calendar_non_fomc_econ_row_is_not_a_catalyst() -> None:
    # Phase 3.9.8: the vendor "kind" field existed only on the retired
    # upcoming-events payload. Sources are now typed by endpoint; the
    # remaining vendor category filter is the economic calendar's
    # ``type``: only "fomc" rows are catalysts (contract §3.5).
    client = _FakeClient()
    client.stub(
        "/api/market/economic-calendar",
        {"data": [{"type": "report", "time": "2024-02-01T13:30:00Z",
                    "event": "Philadelphia Fed Business Outlook Survey"}]},
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", after)
    assert event is None


@pytest.mark.asyncio
async def test_catalyst_calendar_caches() -> None:
    client = _FakeClient()
    client.stub(
        "/api/earnings/AAPL",
        {"data": [{"source": "company", "report_date": "2024-02-01",
                    "report_time": "postmarket"}]},
    )
    provider = UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    after = datetime(2024, 1, 1, tzinfo=UTC)
    await provider.next_catalyst("AAPL", after)
    await provider.next_catalyst("AAPL", after)
    # Phase 3.9.8: three endpoints replace the single retired one; each is
    # fetched once across both calls (no refetch within the TTL).
    paths = sorted(path for path, _ in client.calls)
    assert paths == [
        "/api/earnings/AAPL",
        "/api/market/economic-calendar",
        "/api/market/fda-calendar",
    ]


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
    expected_path = "/api/option-contract/AAPL240216C00150000/historic"
    client.stub(
        expected_path,
        {"chains": [{"date": "2024-01-15", "open_interest": 12345,
                     "volume": 800,
                     "last_tape_time": "2024-01-15T21:30:00Z"}],
         "etf_holdings": []},
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
    """Rows under ``data`` (the client's wrap of a top-level array) are read."""
    client = _FakeClient()
    expected_path = "/api/option-contract/AAPL240216C00150000/historic"
    client.stub(
        expected_path,
        {"data": [{"date": "2024-01-15", "open_interest": 999}]},
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
    expected_path = "/api/option-contract/AAPL240216C00150000/historic"
    client.stub(
        expected_path,
        {"chains": [{"date": "2024-01-17", "open_interest": 23000},
                    {"date": "2024-01-16", "open_interest": 22000},
                    {"date": "2024-01-15", "open_interest": 21000}]},
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
    # The 2024-01-16 row (first after trade_date) comes from one fetch;
    # the historic endpoint ignores ``date``, so no params are sent.
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == expected_path
    assert params is None


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
async def test_open_interest_at_and_next_day_share_one_cache() -> None:
    """Same symbol: one cached historic fetch serves at() and next_day()."""
    client = _FakeClient()
    client.stub(
        "/api/option-contract/AAPL240216C00150000/historic",
        {"chains": [{"date": "2024-01-16", "open_interest": 200},
                    {"date": "2024-01-15", "open_interest": 100}]},
    )
    provider = UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    args: dict[str, Any] = {
        "ticker": "AAPL", "strike": Decimal("150.00"),
        "expiry": date(2024, 2, 16), "option_type": "call",
    }
    at_snap = await provider.at(
        **args,
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    next_snap = await provider.next_day(**args, trade_date=date(2024, 1, 15))
    assert at_snap is not None
    assert at_snap.open_interest == 100
    assert next_snap is not None
    assert next_snap.open_interest == 200
    # One fetch: the endpoint returns every day of the contract
    assert len(client.calls) == 1
    # Repeat both — neither hits the network
    await provider.at(
        **args,
        when=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    await provider.next_day(**args, trade_date=date(2024, 1, 15))
    assert len(client.calls) == 1


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
    # Phase 3.9.10 (D10): peers come from /api/screener/stocks (live), not
    # a guessed ``peers`` list on /info. Assertions unchanged.
    client.stub("/api/stock/AAPL/info", {"data": {"sector": "Technology"}})
    client.stub(
        "/api/screener/stocks",
        {"data": [{"ticker": "MSFT"}, {"ticker": "GOOGL"},
                  {"ticker": "AAPL"}, {"ticker": "aapl"}]},
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
    # Phase 3.9.10 (D10): /api/option-flow/recent never existed (HTTP 404).
    # Rows are now in the live /api/option-trades/flow-alerts shape
    # (created_at, type, side premiums, alert_rule). Assertions unchanged.
    client.stub(
        "/api/option-trades/flow-alerts",
        {
            "data": [
                {"id": "a1", "ticker": "GOOGL", "type": "call",
                 "created_at": "2024-01-15T14:45:00Z",
                 "total_ask_side_prem": "0",
                 "total_bid_side_prem": "150000",
                 "has_multileg": False, "alert_rule": "RepeatedHits"},
                {"id": "a2", "ticker": "MSFT", "type": "call",
                 "created_at": "2024-01-15T14:30:00Z",
                 "total_ask_side_prem": "250000",
                 "total_bid_side_prem": "0",
                 "has_multileg": False, "alert_rule": "RepeatedHits"},
                # Outside window:
                {"id": "a3", "ticker": "MSFT", "type": "call",
                 "created_at": "2024-01-15T13:00:00Z",
                 "total_ask_side_prem": "250000",
                 "total_bid_side_prem": "0",
                 "has_multileg": False, "alert_rule": "RepeatedHits"},
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
    # Phase 3.9.10 (D10): stub path moved off the non-existent
    # /api/option-flow/recent. Assertion unchanged.
    client.stub(
        "/api/option-trades/flow-alerts",
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
    # Phase 3.9.10 (D10): live flow-alerts row shape; the unrecognised value
    # moved from the guessed ``side_classification`` to the option ``type``.
    # Assertions unchanged.
    client.stub(
        "/api/option-trades/flow-alerts",
        {"data": [{"id": "a1", "ticker": "MSFT", "type": "wibble",
                    "created_at": "2024-01-15T14:30:00Z",
                    "total_ask_side_prem": "250000",
                    "total_bid_side_prem": "0",
                    "has_multileg": False,
                    "alert_rule": "RepeatedHits"}]},
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
