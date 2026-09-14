"""Phase 3.4.2.2 tests for ``catalysts_in_window`` Protocol extension.

Pins:
  - Protocol declares the new method
  - NoOp returns empty tuple
  - NoOp still satisfies the Protocol after extension
  - UW provider returns events with when in [start, end]
  - UW filters out events before window
  - UW filters out events after window
  - UW returns sorted ascending by when
  - UW handles empty rows
  - UW handles malformed rows (skipped)
  - UW shares cache with next_catalyst (single-fetch optimisation)

Phase 3.9.8: the UW fixtures moved from the retired
``/api/stock/{t}/upcoming-events`` shape (kind/when/title) to the live
``/api/earnings/{t}``, ``/api/market/fda-calendar`` and
``/api/market/economic-calendar`` row shapes. Stamps are synthesized:
postmarket earnings and precise FDA dates land on 16:00 ET, which is
21:00 UTC on these January/February (EST) dates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from uoa_detector.providers.catalyst_calendar import (
    CatalystCalendarProvider,
    NoOpCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)

# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_protocol_has_catalysts_in_window() -> None:
    assert hasattr(CatalystCalendarProvider, "catalysts_in_window")


def test_noop_returns_empty_tuple() -> None:
    p = NoOpCatalystCalendarProvider()
    result = asyncio.run(p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    ))
    assert result == ()


def test_noop_satisfies_protocol() -> None:
    p = NoOpCatalystCalendarProvider()
    assert isinstance(p, CatalystCalendarProvider)


# ---------------------------------------------------------------------------
# UW provider — fixtures
# ---------------------------------------------------------------------------


def _provider(
    earnings_rows: list[dict[str, Any]],
    fda_rows: list[dict[str, Any]] | None = None,
    econ_rows: list[dict[str, Any]] | None = None,
) -> UnusualWhalesCatalystCalendarProvider:
    """Build a UW provider whose stubbed HTTP routes by path."""
    routes: dict[str, list[dict[str, Any]]] = {
        "/api/market/fda-calendar": fda_rows or [],
        "/api/market/economic-calendar": econ_rows or [],
    }

    async def fake_request_json(
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        if path.startswith("/api/earnings/"):
            return {"data": earnings_rows}
        return {"data": routes.get(path, [])}

    fake_client = MagicMock()
    fake_client.request_json = AsyncMock(side_effect=fake_request_json)
    settings = MagicMock(cache_ttl=MagicMock(catalyst_calendar_seconds=3600))
    return UnusualWhalesCatalystCalendarProvider(
        client=fake_client, settings=settings,
    )


def _earnings(day: str, report_time: str = "postmarket") -> dict[str, Any]:
    return {"source": "company", "report_date": day, "report_time": report_time}


def _fda(day: str) -> dict[str, Any]:
    return {
        "ticker": "AAPL", "catalyst": "PDUFA", "drug": "X",
        "start_date": day, "end_date": None, "target_date": day,
    }


# ---------------------------------------------------------------------------
# UW catalysts_in_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_returns_events_inside_range() -> None:
    p = _provider([_earnings("2024-01-25")], [_fda("2024-02-15")])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_window_filters_events_before_start() -> None:
    p = _provider([_earnings("2023-12-15")], [_fda("2024-02-15")])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].kind == "fda"
    assert result[0].when.date().isoformat() == "2024-02-15"


@pytest.mark.asyncio
async def test_window_filters_events_after_end() -> None:
    p = _provider([_earnings("2024-01-25")], [_fda("2024-04-15")])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].kind == "earnings"
    assert result[0].when.date().isoformat() == "2024-01-25"


@pytest.mark.asyncio
async def test_window_includes_boundary_events() -> None:
    """Events exactly at start or end are included (inclusive bounds).

    Postmarket earnings and precise FDA dates stamp 16:00 ET = 21:00 UTC
    (EST), so the window bounds sit exactly on those stamps.
    """
    p = _provider([_earnings("2024-01-15")], [_fda("2024-02-28")])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, 21, 0, tzinfo=UTC),
        datetime(2024, 2, 28, 21, 0, tzinfo=UTC),
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_window_returns_ascending_sort() -> None:
    """Output is sorted ascending by when, regardless of input order."""
    p = _provider(
        [_earnings("2024-01-25")],
        [_fda("2024-02-15")],
        [{"type": "fomc", "time": "2024-02-20T18:00:00Z", "event": "FOMC"}],
    )
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    kinds = [e.kind for e in result]
    assert kinds == ["earnings", "fda", "fomc"]


@pytest.mark.asyncio
async def test_window_empty_rows_returns_empty_tuple() -> None:
    p = _provider([])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    )
    assert result == ()


@pytest.mark.asyncio
async def test_window_skips_malformed_rows() -> None:
    """Rows without a usable date are dropped; valid rows continue."""
    p = _provider(
        [
            _earnings("2024-01-25"),
            {"source": "company", "report_time": "postmarket"},  # no date
            _earnings("garbage"),  # bad date
        ],
        [{"ticker": "AAPL", "catalyst": "PDUFA"}],  # no date
        [{"type": "fomc", "time": "garbage", "event": "bad_time"}],
    )
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].when.date().isoformat() == "2024-01-25"


@pytest.mark.asyncio
async def test_window_unknown_report_time_normalised_to_close() -> None:
    """An unrecognised report_time maps to the 16:00 ET close (not dropped)."""
    p = _provider([_earnings("2024-01-25", report_time="weird_unknown")])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].when == datetime(2024, 1, 25, 21, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Cache sharing with next_catalyst
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_and_next_share_cache() -> None:
    """Calling both methods fetches each endpoint only once."""
    p = _provider([_earnings("2024-01-25")])
    await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    )
    await p.next_catalyst("AAPL", datetime(2024, 1, 1, tzinfo=UTC))
    # Three endpoints (earnings, FDA, economic calendar), one call each.
    assert p._client.request_json.await_count == 3
