"""Phase 3.9.8 tests: M22 catalyst calendar against live-shaped UW rows.

Fixtures are trimmed rows captured live on 2026-09-14:
  - ``/api/earnings/AAPL``
  - ``/api/market/fda-calendar?ticker=GERN`` and one ACAD row from the
    unfiltered feed
  - ``/api/market/economic-calendar``. That feed carried only
    ``type=report`` rows, so the fomc and fed-speaker rows are synthetic.

Pins (contract ``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.5):
  - earnings timing: premarket -> 09:30 ET, anything else -> 16:00 ET,
    with the EDT/EST offset applied per report_date
  - the upcoming ``source=estimation`` row is the next catalyst
  - earnings dedupe by report_date prefers a known timing
  - FDA: ticker + limit params, precise dates only
  - FOMC: ``type == "fomc"`` only, applied to every ticker, fetched once
  - per-source isolation: NotFound / empty empties that source only;
    every other UW error propagates and is not cached
  - the parsed tuple is cached per ticker across both methods
  - M22 same-day blackout holds with these stamps (real pure function)
  - M24's window sees no same-day postmarket report for RTH flow
  - ``build_live_stage_pipeline`` shares one provider between M22 and M24
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.pipeline.stages import build_live_stage_pipeline
from uoa_detector.pipeline.stages.m22_event_calendar import (
    EventCalendarStage,
    _score_from_catalysts,
)
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.providers.catalyst_calendar import CatalystEvent
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)

_EARNINGS = "/api/earnings/AAPL"
_FDA = "/api/market/fda-calendar"
_ECON = "/api/market/economic-calendar"

# ---------------------------------------------------------------------------
# Live-shaped fixtures (trimmed)
# ---------------------------------------------------------------------------

_EARNINGS_AAPL: list[dict[str, Any]] = [
    {"source": "estimation", "report_date": "2026-10-29", "report_time": "unknown",
     "ending_fiscal_quarter": "2026-09-30", "street_mean_est": "1.98",
     "actual_eps": None, "expected_move": None},
    {"source": "company", "report_date": "2026-07-30", "report_time": "postmarket",
     "ending_fiscal_quarter": "2026-06-30", "street_mean_est": "1.88",
     "actual_eps": "1.91", "expected_move": "10.56"},
    {"source": "company", "report_date": "2026-01-29", "report_time": "postmarket",
     "ending_fiscal_quarter": "2025-12-31", "street_mean_est": "2.65",
     "actual_eps": "2.84"},
    {"source": "company", "report_date": "2001-07-18", "report_time": "premarket"},
]

_FDA_GERN: list[dict[str, Any]] = [
    {"ticker": "GERN", "catalyst": "Top-line Data Due", "event_type": "Top-line Data Due",
     "drug": "Imetelstat (IMpactMF)", "status": "Phase 3", "start_date": "2021-04-13",
     "end_date": None, "target_date": "2025-MID", "date_string": "2021-04-13"},
    {"ticker": "GERN", "catalyst": "Initial Data Due", "event_type": None,
     "drug": "Imetelstat", "status": "Phase 3", "start_date": "2023-09-01",
     "end_date": "2023-12-31", "target_date": None, "date_string": "2023-LATE"},
    {"ticker": "GERN", "catalyst": "Interim Data", "event_type": "Interim Data",
     "drug": "IMpactMF", "status": "Unknown", "start_date": "2023-12-06",
     "end_date": None, "target_date": "2026-H1", "date_string": "2023-12-06"},
    {"ticker": "GERN", "catalyst": None, "event_type": None, "drug": "Imetelstat",
     "status": "PDUFA", "start_date": "2024-03-14", "end_date": "2024-03-14",
     "target_date": None, "date_string": "March 14, 2024"},
    {"ticker": "GERN", "catalyst": "", "event_type": None, "drug": "Imetelstat",
     "status": "NDA", "start_date": "2024-06-16", "end_date": "2024-06-16",
     "target_date": None, "date_string": "06/16/2024"},
    {"ticker": "GERN", "catalyst": "Data Presentation", "event_type": "Data Presentation",
     "drug": "Imetelstat", "status": "Unknown", "start_date": "2025-12-08",
     "end_date": None, "target_date": "", "date_string": "2025-12-08"},
    {"ticker": "GERN", "catalyst": "Interim Data", "event_type": None, "drug": "IMpactMF",
     "status": "", "start_date": "2026-01-01", "end_date": "2026-06-30",
     "target_date": None, "date_string": "2026-H1"},
]

_FDA_ACAD: dict[str, Any] = {
    "ticker": "ACAD", "catalyst": "Data Presentation", "event_type": "Data Presentation",
    "drug": "NUPLAZID (Pimavanserin)", "status": "Phase 2b", "start_date": "2021-10-25",
    "end_date": "2021-11-09", "target_date": "2021-11-09", "date_string": "2021-10-25",
}

_ECON_LIVE: list[dict[str, Any]] = [
    {"type": "report", "time": "2026-09-17T12:30:00Z", "prev": "47.4",
     "event": "Philadelphia Fed Business Outlook Survey", "forecast": "27.5",
     "reported_period": "September"},
    {"type": "report", "time": "2026-09-16T18:00:00Z", "prev": "3.8",
     "event": "U.S. interest rate decision", "forecast": None, "reported_period": None},
    {"type": "report", "time": "2026-09-16T12:30:00Z", "prev": "-0.6%",
     "event": "Retail Sales", "forecast": "0.8%", "reported_period": "August"},
    {"type": "report", "time": "2026-09-24T12:00:00Z", "prev": None,
     "event": ("Economic Club of Washington, DC event with Federal Reserve Bank "
               "of Richmond President Thomas Barkin"),
     "forecast": None, "reported_period": None},
]

_SYNTHETIC_FOMC: dict[str, Any] = {
    "type": "fomc", "time": "2026-10-28T18:00:00Z", "prev": None,
    "event": "FOMC rate decision", "forecast": None, "reported_period": None,
}

_SYNTHETIC_FED_SPEAKER: dict[str, Any] = {
    "type": "fed-speaker", "time": "2026-09-22T16:00:00Z", "prev": None,
    "event": "Fed Chair press conference", "forecast": None, "reported_period": None,
}


# ---------------------------------------------------------------------------
# Fake client + helpers
# ---------------------------------------------------------------------------


class _RoutingClient:
    """Path-routed stand-in for ``UnusualWhalesClient.request_json``."""

    def __init__(
        self,
        routes: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.routes: dict[str, dict[str, Any]] = dict(routes or {})
        self.errors: dict[str, Exception] = dict(errors or {})
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path in self.errors:
            raise self.errors[path]
        return self.routes.get(path, {"data": []})

    def count(self, path: str) -> int:
        return sum(1 for called, _ in self.calls if called == path)


def _provider(client: _RoutingClient) -> UnusualWhalesCatalystCalendarProvider:
    return UnusualWhalesCatalystCalendarProvider(
        client=client,  # type: ignore[arg-type]
        settings=UnusualWhalesSettings(cache_ttl=UnusualWhalesProviderCacheTTL()),
    )


def _earnings_client(*rows: dict[str, Any]) -> _RoutingClient:
    return _RoutingClient({_EARNINGS: {"data": list(rows)}})


def _all_sources_client(
    errors: dict[str, Exception] | None = None,
) -> _RoutingClient:
    """AAPL with one event per source: FDA 10-20, FOMC 10-28, earnings 10-29."""
    return _RoutingClient(
        routes={
            _EARNINGS: {"data": [_EARNINGS_AAPL[0]]},
            _FDA: {"data": [{
                "ticker": "AAPL", "catalyst": "PDUFA", "drug": "X",
                "start_date": "2026-10-20", "end_date": "2026-10-20",
                "target_date": None,
            }]},
            _ECON: {"data": [_SYNTHETIC_FOMC]},
        },
        errors=errors,
    )


_WIDE_START = datetime(1990, 1, 1, tzinfo=UTC)
_WIDE_END = datetime(2030, 12, 31, tzinfo=UTC)
_OCT_START = datetime(2026, 10, 1, tzinfo=UTC)
_NOV_END = datetime(2026, 11, 30, tzinfo=UTC)


async def _all_events(
    provider: UnusualWhalesCatalystCalendarProvider, ticker: str = "AAPL",
) -> tuple[CatalystEvent, ...]:
    return await provider.catalysts_in_window(ticker, _WIDE_START, _WIDE_END)


# ===========================================================================
# Earnings
# ===========================================================================


@pytest.mark.asyncio
async def test_live_earnings_rows_map_one_event_per_report() -> None:
    client = _earnings_client(*_EARNINGS_AAPL)
    events = await _provider(client).catalysts_in_window(
        "AAPL", datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC),
    )
    assert [(e.when, e.title) for e in events] == [
        (datetime(2026, 1, 29, 21, 0, tzinfo=UTC),
         "AAPL earnings 2026-01-29 (postmarket, company)"),
        (datetime(2026, 7, 30, 20, 0, tzinfo=UTC),
         "AAPL earnings 2026-07-30 (postmarket, company)"),
        (datetime(2026, 10, 29, 20, 0, tzinfo=UTC),
         "AAPL earnings 2026-10-29 (unknown, estimation)"),
    ]
    assert all(e.kind == "earnings" and e.ticker == "AAPL" for e in events)


@pytest.mark.parametrize(
    ("report_date", "report_time", "expected"),
    [
        # postmarket / unknown -> 16:00 ET
        ("2026-07-30", "postmarket", datetime(2026, 7, 30, 20, 0, tzinfo=UTC)),  # EDT
        ("2026-01-29", "postmarket", datetime(2026, 1, 29, 21, 0, tzinfo=UTC)),  # EST
        ("2026-10-29", "unknown", datetime(2026, 10, 29, 20, 0, tzinfo=UTC)),  # EDT, pre Nov-1 switch
        ("2026-11-05", "unknown", datetime(2026, 11, 5, 21, 0, tzinfo=UTC)),  # EST, post switch
        # premarket -> 09:30 ET
        ("2001-07-18", "premarket", datetime(2001, 7, 18, 13, 30, tzinfo=UTC)),  # EDT (live row)
        ("2026-02-05", "premarket", datetime(2026, 2, 5, 14, 30, tzinfo=UTC)),  # EST
        ("2026-03-06", "premarket", datetime(2026, 3, 6, 14, 30, tzinfo=UTC)),  # last EST Friday
        ("2026-03-09", "premarket", datetime(2026, 3, 9, 13, 30, tzinfo=UTC)),  # first EDT Monday
    ],
)
@pytest.mark.asyncio
async def test_earnings_timing_maps_to_et_session_boundary_per_dst_regime(
    report_date: str, report_time: str, expected: datetime,
) -> None:
    client = _earnings_client(
        {"source": "company", "report_date": report_date, "report_time": report_time},
    )
    events = await _all_events(_provider(client))
    assert len(events) == 1
    assert events[0].when == expected
    assert events[0].when.tzinfo is UTC
    assert events[0].when.date().isoformat() == report_date


@pytest.mark.parametrize(
    ("raw_time", "expected", "label"),
    [
        ("PREMARKET", datetime(2026, 7, 30, 13, 30, tzinfo=UTC), "premarket"),
        (" Premarket ", datetime(2026, 7, 30, 13, 30, tzinfo=UTC), "premarket"),
        ("PostMarket", datetime(2026, 7, 30, 20, 0, tzinfo=UTC), "postmarket"),
        ("unknown", datetime(2026, 7, 30, 20, 0, tzinfo=UTC), "unknown"),
        (None, datetime(2026, 7, 30, 20, 0, tzinfo=UTC), "unknown"),
        ("", datetime(2026, 7, 30, 20, 0, tzinfo=UTC), "unknown"),
        # Vocabulary never observed live is "anything else" -> close.
        ("pre-market", datetime(2026, 7, 30, 20, 0, tzinfo=UTC), "pre-market"),
        ("after-hours", datetime(2026, 7, 30, 20, 0, tzinfo=UTC), "after-hours"),
    ],
)
@pytest.mark.asyncio
async def test_earnings_report_time_is_case_insensitive_and_defaults_to_close(
    raw_time: str | None, expected: datetime, label: str,
) -> None:
    client = _earnings_client(
        {"source": "company", "report_date": "2026-07-30", "report_time": raw_time},
    )
    events = await _all_events(_provider(client))
    assert len(events) == 1
    assert events[0].when == expected
    assert events[0].title == f"AAPL earnings 2026-07-30 ({label}, company)"


@pytest.mark.asyncio
async def test_missing_source_is_labelled_unknown_in_title() -> None:
    client = _earnings_client({"report_date": "2026-07-30", "report_time": "postmarket"})
    events = await _all_events(_provider(client))
    assert events[0].title == "AAPL earnings 2026-07-30 (postmarket, unknown)"


@pytest.mark.asyncio
async def test_upcoming_estimation_row_is_next_catalyst() -> None:
    client = _earnings_client(*_EARNINGS_AAPL)
    # Probe time of the live capture (Monday pre-market).
    event = await _provider(client).next_catalyst(
        "AAPL", datetime(2026, 9, 14, 11, 44, tzinfo=UTC),
    )
    assert event is not None
    assert event.kind == "earnings"
    assert event.when == datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    assert event.title == "AAPL earnings 2026-10-29 (unknown, estimation)"


@pytest.mark.asyncio
async def test_next_catalyst_after_is_inclusive_and_none_past_last_event() -> None:
    client = _earnings_client(*_EARNINGS_AAPL)
    provider = _provider(client)
    at_stamp = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
    event = await provider.next_catalyst("AAPL", at_stamp)
    assert event is not None
    assert event.when == at_stamp
    assert await provider.next_catalyst("AAPL", at_stamp + timedelta(seconds=1)) is None


@pytest.mark.asyncio
async def test_earnings_rows_without_iso_report_date_are_skipped() -> None:
    client = _earnings_client(
        {"source": "company", "report_date": None, "report_time": "postmarket"},
        {"source": "company", "report_time": "postmarket"},
        {"source": "company", "report_date": "garbage", "report_time": "postmarket"},
        {"source": "company", "report_date": "2026/07/30", "report_time": "postmarket"},
        {"source": "company", "report_date": "20260730", "report_time": "postmarket"},
        {"source": "company", "report_date": 20260730, "report_time": "postmarket"},
        {"source": "company", "report_date": "2026-02-30", "report_time": "postmarket"},
        {"source": "company", "report_date": "2026-07-30", "report_time": "postmarket"},
    )
    events = await _all_events(_provider(client))
    assert [e.when.date().isoformat() for e in events] == ["2026-07-30"]


@pytest.mark.parametrize("known_first", [True, False])
@pytest.mark.asyncio
async def test_earnings_dedupe_by_report_date_prefers_known_timing(
    known_first: bool,
) -> None:
    unknown = {"source": "estimation", "report_date": "2026-10-29", "report_time": "unknown"}
    known = {"source": "company", "report_date": "2026-10-29", "report_time": "premarket"}
    rows = [known, unknown] if known_first else [unknown, known]
    events = await _all_events(_provider(_earnings_client(*rows)))
    assert len(events) == 1
    assert events[0].when == datetime(2026, 10, 29, 13, 30, tzinfo=UTC)
    assert events[0].title == "AAPL earnings 2026-10-29 (premarket, company)"


@pytest.mark.asyncio
async def test_earnings_dedupe_same_knownness_keeps_first_row() -> None:
    client = _earnings_client(
        {"source": "estimation", "report_date": "2026-10-29", "report_time": "unknown"},
        {"source": "company", "report_date": "2026-10-29", "report_time": None},
        {"source": "company", "report_date": "2026-07-30", "report_time": "postmarket"},
        {"source": "company", "report_date": "2026-07-30", "report_time": "premarket"},
    )
    events = await _all_events(_provider(client))
    assert [e.title for e in events] == [
        "AAPL earnings 2026-07-30 (postmarket, company)",
        "AAPL earnings 2026-10-29 (unknown, estimation)",
    ]


@pytest.mark.asyncio
async def test_lowercase_ticker_is_uppercased_in_requests_and_events() -> None:
    client = _earnings_client(_EARNINGS_AAPL[0])
    events = await _all_events(_provider(client), ticker="aapl")
    assert client.count(_EARNINGS) == 1
    assert (_FDA, {"ticker": "AAPL", "limit": 200}) in client.calls
    assert [e.ticker for e in events] == ["AAPL"]


# ===========================================================================
# FDA
# ===========================================================================


@pytest.mark.asyncio
async def test_fda_request_is_ticker_filtered_at_api_max_limit() -> None:
    client = _RoutingClient()
    await _all_events(_provider(client), ticker="GERN")
    fda_calls = [params for path, params in client.calls if path == _FDA]
    assert fda_calls == [{"ticker": "GERN", "limit": 200}]


@pytest.mark.asyncio
async def test_fda_only_precise_dates_become_catalysts() -> None:
    """Quarter/half/mid targets, empty targets and date ranges are dropped."""
    client = _RoutingClient({_FDA: {"data": _FDA_GERN}})
    events = await _all_events(_provider(client), ticker="GERN")
    assert [(e.kind, e.when, e.title) for e in events] == [
        ("fda", datetime(2024, 3, 14, 20, 0, tzinfo=UTC),
         "GERN FDA 2024-03-14: PDUFA (Imetelstat)"),
        ("fda", datetime(2024, 6, 16, 20, 0, tzinfo=UTC),
         "GERN FDA 2024-06-16: NDA (Imetelstat)"),
    ]


@pytest.mark.asyncio
async def test_fda_precise_target_date_wins_over_range() -> None:
    client = _RoutingClient({_FDA: {"data": [_FDA_ACAD]}})
    events = await _all_events(_provider(client), ticker="ACAD")
    assert len(events) == 1
    # 2021-11-09 is after the Nov-7 switch: 16:00 EST = 21:00 UTC.
    assert events[0].when == datetime(2021, 11, 9, 21, 0, tzinfo=UTC)
    assert events[0].title == "ACAD FDA 2021-11-09: Data Presentation (NUPLAZID (Pimavanserin))"


@pytest.mark.parametrize(
    ("row", "expected_day"),
    [
        # text target, single-day window -> start_date
        ({"target_date": "2026-Q4", "start_date": "2026-11-20", "end_date": "2026-11-20"},
         "2026-11-20"),
        # no target, single-day window -> start_date
        ({"target_date": None, "start_date": "2026-11-20", "end_date": "2026-11-20"},
         "2026-11-20"),
        # start only (end missing / null) -> dropped
        ({"target_date": None, "start_date": "2026-11-20", "end_date": None}, None),
        ({"start_date": "2026-11-20"}, None),
        # text target with a range -> dropped
        ({"target_date": "2026-LATE", "start_date": "2026-09-01", "end_date": "2026-12-31"},
         None),
        # nothing parseable -> dropped
        ({"target_date": "2026-H2", "start_date": None, "end_date": None}, None),
    ],
)
@pytest.mark.asyncio
async def test_fda_date_rule(row: dict[str, Any], expected_day: str | None) -> None:
    full_row = {"ticker": "GERN", "catalyst": "PDUFA", "drug": "X", **row}
    client = _RoutingClient({_FDA: {"data": [full_row]}})
    events = await _all_events(_provider(client), ticker="GERN")
    assert [e.when.date().isoformat() for e in events] == (
        [expected_day] if expected_day else []
    )


@pytest.mark.asyncio
async def test_fda_rows_for_other_or_missing_tickers_are_dropped() -> None:
    client = _RoutingClient({_FDA: {"data": [
        *_FDA_GERN,
        {**_FDA_ACAD, "ticker": "acad"},
        {**_FDA_ACAD, "ticker": None},
        {k: v for k, v in _FDA_ACAD.items() if k != "ticker"},
    ]}})
    events = await _all_events(_provider(client), ticker="ACAD")
    assert [(e.ticker, e.when.date().isoformat()) for e in events] == [("ACAD", "2021-11-09")]


@pytest.mark.parametrize(
    ("row", "title"),
    [
        ({"catalyst": "PDUFA", "drug": "X"}, "GERN FDA 2026-11-20: PDUFA (X)"),
        ({"catalyst": None, "status": "NDA", "drug": "X"}, "GERN FDA 2026-11-20: NDA (X)"),
        ({"catalyst": "", "status": "", "drug": ""}, "GERN FDA 2026-11-20: FDA event"),
        ({}, "GERN FDA 2026-11-20: FDA event"),
    ],
)
@pytest.mark.asyncio
async def test_fda_title_from_catalyst_and_drug(row: dict[str, Any], title: str) -> None:
    full_row = {"ticker": "GERN", "target_date": "2026-11-20", **row}
    client = _RoutingClient({_FDA: {"data": [full_row]}})
    events = await _all_events(_provider(client), ticker="GERN")
    assert [e.title for e in events] == [title]


# ===========================================================================
# FOMC (economic calendar)
# ===========================================================================


@pytest.mark.asyncio
async def test_only_fomc_typed_econ_rows_become_catalysts() -> None:
    client = _RoutingClient({_ECON: {"data": [
        *_ECON_LIVE, _SYNTHETIC_FED_SPEAKER, _SYNTHETIC_FOMC,
    ]}})
    events = await _all_events(_provider(client))
    assert [(e.kind, e.when, e.title, e.ticker) for e in events] == [
        ("fomc", datetime(2026, 10, 28, 18, 0, tzinfo=UTC), "FOMC rate decision", "AAPL"),
    ]


@pytest.mark.asyncio
async def test_live_rate_decision_row_typed_report_is_not_a_catalyst() -> None:
    """Contract §3.5 filters on ``type == "fomc"``.

    Live on 2026-09-14 the 2026-09-16 rate decision arrived as
    ``type=report`` ("U.S. interest rate decision"), so under the contract
    it is not a catalyst. Flagged for Berkay: changing this is a contract
    decision, not a parser fix.
    """
    client = _RoutingClient({_ECON: {"data": _ECON_LIVE}})
    event = await _provider(client).next_catalyst(
        "AAPL", datetime(2026, 9, 14, 11, 44, tzinfo=UTC),
    )
    assert event is None


@pytest.mark.asyncio
async def test_fomc_applies_to_every_ticker_and_calendar_is_fetched_once() -> None:
    client = _RoutingClient({
        _ECON: {"data": [_SYNTHETIC_FOMC]},
        _EARNINGS: {"data": [_EARNINGS_AAPL[0]]},
    })
    provider = _provider(client)
    by_ticker = {t: await _all_events(provider, ticker=t) for t in ("AAPL", "MSFT", "GERN")}
    for ticker, events in by_ticker.items():
        fomc = [e for e in events if e.kind == "fomc"]
        assert [(e.ticker, e.when) for e in fomc] == [
            (ticker, datetime(2026, 10, 28, 18, 0, tzinfo=UTC)),
        ]
    assert client.count(_ECON) == 1
    for ticker in ("AAPL", "MSFT", "GERN"):
        assert client.count(f"/api/earnings/{ticker}") == 1
    assert [e.kind for e in by_ticker["AAPL"]] == ["fomc", "earnings"]


@pytest.mark.parametrize(
    ("raw_time", "expected"),
    [
        ("2026-10-28T18:00:00Z", datetime(2026, 10, 28, 18, 0, tzinfo=UTC)),
        ("2026-10-28 18:00:00+00:00", datetime(2026, 10, 28, 18, 0, tzinfo=UTC)),
        ("2026-10-28T14:00:00-04:00", datetime(2026, 10, 28, 18, 0, tzinfo=UTC)),
        ("2026-10-28T18:00:00", datetime(2026, 10, 28, 18, 0, tzinfo=UTC)),  # naive = UTC
        ("garbage", None),
        ("", None),
        (None, None),
        (1_793_210_400, None),
    ],
)
@pytest.mark.asyncio
async def test_fomc_time_parsing(raw_time: object, expected: datetime | None) -> None:
    client = _RoutingClient({_ECON: {"data": [{**_SYNTHETIC_FOMC, "time": raw_time}]}})
    events = await _all_events(_provider(client))
    assert [e.when for e in events] == ([expected] if expected else [])
    assert all(e.when.utcoffset() == timedelta(0) for e in events)


@pytest.mark.parametrize(("raw_type", "is_catalyst"), [
    ("fomc", True), ("FOMC", True), ("report", False), ("fed-speaker", False),
    (None, False),
])
@pytest.mark.asyncio
async def test_fomc_type_filter(raw_type: str | None, is_catalyst: bool) -> None:
    client = _RoutingClient({_ECON: {"data": [{**_SYNTHETIC_FOMC, "type": raw_type}]}})
    events = await _all_events(_provider(client))
    assert bool(events) is is_catalyst


# ===========================================================================
# Per-source isolation and caching
# ===========================================================================


@pytest.mark.parametrize(
    ("failing_path", "surviving"),
    [
        (_EARNINGS, ["fda", "fomc"]),
        (_FDA, ["fomc", "earnings"]),
        (_ECON, ["fda", "earnings"]),
    ],
)
@pytest.mark.parametrize("status", [404, 422])
@pytest.mark.asyncio
async def test_not_found_on_one_source_empties_only_that_source(
    failing_path: str, surviving: list[str], status: int,
) -> None:
    client = _all_sources_client(errors={
        failing_path: UnusualWhalesNotFoundError(f"HTTP {status}", status_code=status),
    })
    events = await _provider(client).catalysts_in_window("AAPL", _OCT_START, _NOV_END)
    assert [e.kind for e in events] == surviving


@pytest.mark.parametrize(
    "payload",
    [{"data": []}, {"data": None}, {"data": {"x": 1}}, {}, {"data": ["row", 7]}],
)
@pytest.mark.parametrize(
    ("empty_path", "surviving"),
    [
        (_EARNINGS, ["fda", "fomc"]),
        (_FDA, ["fomc", "earnings"]),
        (_ECON, ["fda", "earnings"]),
    ],
)
@pytest.mark.asyncio
async def test_empty_or_non_list_payload_empties_only_that_source(
    payload: dict[str, Any], empty_path: str, surviving: list[str],
) -> None:
    client = _all_sources_client()
    client.routes[empty_path] = payload
    events = await _provider(client).catalysts_in_window("AAPL", _OCT_START, _NOV_END)
    assert [e.kind for e in events] == surviving


_PROPAGATING_ERRORS: list[Callable[[], Exception]] = [
    lambda: UnusualWhalesAuthError("HTTP 401: bad key"),
    lambda: UnusualWhalesRateLimitError("HTTP 429 after retries"),
    lambda: UnusualWhalesTransientError("HTTP 503 after retries"),
    lambda: CircuitBreakerOpenError("breaker open"),
]


@pytest.mark.parametrize(
    "make_error", _PROPAGATING_ERRORS, ids=["auth", "rate_limit", "transient", "breaker"],
)
@pytest.mark.parametrize("failing_path", [_EARNINGS, _FDA, _ECON])
@pytest.mark.asyncio
async def test_other_uw_errors_propagate(
    failing_path: str, make_error: Callable[[], Exception],
) -> None:
    error = make_error()
    client = _all_sources_client(errors={failing_path: error})
    with pytest.raises(type(error)):
        await _provider(client).catalysts_in_window("AAPL", _OCT_START, _NOV_END)


@pytest.mark.asyncio
async def test_propagated_error_is_not_cached() -> None:
    client = _all_sources_client(errors={_ECON: UnusualWhalesTransientError("HTTP 503")})
    provider = _provider(client)
    with pytest.raises(UnusualWhalesTransientError):
        await provider.next_catalyst("AAPL", _OCT_START)
    del client.errors[_ECON]
    events = await provider.catalysts_in_window("AAPL", _OCT_START, _NOV_END)
    assert [e.kind for e in events] == ["fda", "fomc", "earnings"]
    assert client.count(_ECON) == 2


@pytest.mark.asyncio
async def test_not_found_source_is_cached_with_the_ticker_tuple() -> None:
    client = _all_sources_client(errors={
        _EARNINGS: UnusualWhalesNotFoundError("HTTP 404", status_code=404),
    })
    provider = _provider(client)
    await provider.catalysts_in_window("AAPL", _OCT_START, _NOV_END)
    await provider.next_catalyst("AAPL", _OCT_START)
    assert client.count(_EARNINGS) == 1


@pytest.mark.asyncio
async def test_parsed_tuple_is_cached_per_ticker_across_both_methods() -> None:
    client = _all_sources_client()
    provider = _provider(client)
    first = await provider.catalysts_in_window("AAPL", _OCT_START, _NOV_END)
    nxt = await provider.next_catalyst("AAPL", _OCT_START)
    second = await provider.catalysts_in_window("aapl", _OCT_START, _NOV_END)
    assert first == second
    assert nxt == first[0]
    assert (client.count(_EARNINGS), client.count(_FDA), client.count(_ECON)) == (1, 1, 1)


# ===========================================================================
# Stage semantics with the new stamps
# ===========================================================================


@pytest.mark.parametrize(
    ("report_date", "report_time", "flow_ts"),
    [
        ("2026-07-30", "postmarket", datetime(2026, 7, 30, 15, 0, tzinfo=UTC)),  # 11:00 EDT
        ("2026-07-30", "premarket", datetime(2026, 7, 30, 15, 0, tzinfo=UTC)),
        ("2026-01-29", "postmarket", datetime(2026, 1, 29, 20, 30, tzinfo=UTC)),  # 15:30 EST
        ("2026-10-29", "unknown", datetime(2026, 10, 29, 14, 0, tzinfo=UTC)),  # estimation row
        ("2026-07-30", "postmarket", datetime(2026, 7, 31, 15, 0, tzinfo=UTC)),  # reaction day
    ],
)
@pytest.mark.asyncio
async def test_m22_report_on_or_just_before_flow_day_is_post_event_blackout(
    report_date: str, report_time: str, flow_ts: datetime,
) -> None:
    m22 = load_default_profile().scoring.modules.m22
    client = _earnings_client(
        {"source": "company", "report_date": report_date, "report_time": report_time},
    )
    catalysts = await _provider(client).catalysts_in_window(
        "AAPL",
        flow_ts - timedelta(days=m22.post_event_blackout_days),
        flow_ts + timedelta(days=m22.pre_event_window_days),
    )
    score, branch = _score_from_catalysts(
        catalysts=catalysts, event_ts=flow_ts, expiry=date(2026, 12, 18), settings=m22,
    )
    assert branch == "post_event_blackout"
    assert score == m22.post_event_score


@pytest.mark.parametrize(
    ("expiry", "expected_branch"),
    [
        (date(2026, 8, 21), "pre_event_dte_survives"),
        (date(2026, 7, 30), "pre_event_dte_expires_before"),  # expiry == report date
    ],
)
@pytest.mark.asyncio
async def test_m22_day_before_report_is_pre_event_keyed_on_report_date(
    expiry: date, expected_branch: str,
) -> None:
    m22 = load_default_profile().scoring.modules.m22
    flow_ts = datetime(2026, 7, 29, 15, 0, tzinfo=UTC)
    client = _earnings_client(
        {"source": "company", "report_date": "2026-07-30", "report_time": "postmarket"},
    )
    catalysts = await _provider(client).catalysts_in_window(
        "AAPL",
        flow_ts - timedelta(days=m22.post_event_blackout_days),
        flow_ts + timedelta(days=m22.pre_event_window_days),
    )
    _, branch = _score_from_catalysts(
        catalysts=catalysts, event_ts=flow_ts, expiry=expiry, settings=m22,
    )
    assert branch == expected_branch


@pytest.mark.parametrize(
    ("report_date", "report_time", "flow_ts", "in_window"),
    [
        ("2026-07-30", "postmarket", datetime(2026, 7, 30, 19, 59, tzinfo=UTC), False),  # 15:59 EDT
        ("2026-01-29", "postmarket", datetime(2026, 1, 29, 20, 59, tzinfo=UTC), False),  # 15:59 EST
        ("2026-10-29", "unknown", datetime(2026, 10, 29, 19, 59, tzinfo=UTC), False),
        ("2026-07-30", "premarket", datetime(2026, 7, 30, 13, 30, tzinfo=UTC), True),  # 09:30 EDT
        ("2026-02-05", "premarket", datetime(2026, 2, 5, 14, 30, tzinfo=UTC), True),  # 09:30 EST
        ("2026-07-30", "postmarket", datetime(2026, 7, 31, 14, 0, tzinfo=UTC), True),  # next session
    ],
)
@pytest.mark.asyncio
async def test_m24_window_has_no_same_day_look_ahead(
    report_date: str, report_time: str, flow_ts: datetime, in_window: bool,
) -> None:
    m24 = load_default_profile().scoring.modules.m24
    client = _earnings_client(
        {"source": "company", "report_date": report_date, "report_time": report_time},
    )
    catalysts = await _provider(client).catalysts_in_window(
        "AAPL", flow_ts - timedelta(days=m24.post_earnings_session_days), flow_ts,
    )
    assert bool(catalysts) is in_window


# ===========================================================================
# live_stages wiring
# ===========================================================================


def test_live_stages_share_one_catalyst_provider_between_m22_and_m24() -> None:
    client = UnusualWhalesClient(
        api_key=SecretStr("test-key-not-used"), settings=UnusualWhalesSettings(),
    )
    profile = load_default_profile()
    stages = {type(s): s for s in build_live_stage_pipeline(client, profile)}
    m22 = stages[EventCalendarStage]
    m24 = stages[IVExhaustionStage]
    assert isinstance(m22, EventCalendarStage)
    assert isinstance(m24, IVExhaustionStage)
    assert isinstance(m22._provider, UnusualWhalesCatalystCalendarProvider)
    assert m22._provider is m24._catalyst_provider

    rebuilt = {type(s): s for s in build_live_stage_pipeline(client, profile)}
    rebuilt_m22 = rebuilt[EventCalendarStage]
    assert isinstance(rebuilt_m22, EventCalendarStage)
    assert rebuilt_m22._provider is not m22._provider
