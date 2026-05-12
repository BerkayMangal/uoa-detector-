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

Phase 3.3.9.4 update: stub fixtures use the new earnings-endpoint
shape (report_date / report_time); the multi-source aggregation is
exercised in test_unusual_whales_providers_part2.py. These tests
focus on the windowing/filtering/sort semantics of
``catalysts_in_window`` over a single source's output.
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
    earnings_rows: list[dict[str, Any]] | None = None,
) -> UnusualWhalesCatalystCalendarProvider:
    """Build a UW provider whose earnings endpoint returns ``earnings_rows``.

    FDA and economic-calendar endpoints return empty by default; tests that
    care about those sources should construct their own stubs (see
    test_unusual_whales_providers_part2.py).
    """
    fake_client = MagicMock()

    async def fake_request_json(
        path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        if path.startswith("/api/earnings/"):
            return {"data": earnings_rows or []}
        return {"data": []}

    fake_client.request_json = AsyncMock(side_effect=fake_request_json)
    settings = MagicMock(cache_ttl=MagicMock(catalyst_calendar_seconds=3600))
    return UnusualWhalesCatalystCalendarProvider(
        client=fake_client, settings=settings,
    )


# ---------------------------------------------------------------------------
# UW catalysts_in_window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_returns_events_inside_range() -> None:
    p = _provider([
        {"report_date": "2024-01-25", "report_time": "after-hours"},
        {"report_date": "2024-02-15", "report_time": "after-hours"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_window_filters_events_before_start() -> None:
    p = _provider([
        {"report_date": "2023-12-15", "report_time": "after-hours"},
        {"report_date": "2024-02-15", "report_time": "after-hours"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].when.date().isoformat() == "2024-02-15"


@pytest.mark.asyncio
async def test_window_filters_events_after_end() -> None:
    p = _provider([
        {"report_date": "2024-01-25", "report_time": "after-hours"},
        {"report_date": "2024-04-15", "report_time": "after-hours"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].when.date().isoformat() == "2024-01-25"


@pytest.mark.asyncio
async def test_window_includes_boundary_events() -> None:
    """Events exactly at start or end are included (inclusive bounds).

    Earnings report_time=after-hours maps to 21:00 UTC; window endpoints
    at 22:00 UTC bracket the 21:00 events.
    """
    p = _provider([
        {"report_date": "2024-01-15", "report_time": "after-hours"},
        {"report_date": "2024-02-28", "report_time": "after-hours"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, 21, 0, tzinfo=UTC),
        datetime(2024, 2, 28, 21, 0, tzinfo=UTC),
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_window_returns_ascending_sort() -> None:
    """Output is sorted ascending by when, regardless of input order."""
    p = _provider([
        {"report_date": "2024-02-15", "report_time": "after-hours"},
        {"report_date": "2024-01-25", "report_time": "after-hours"},
        {"report_date": "2024-02-20", "report_time": "after-hours"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    dates = [e.when.date().isoformat() for e in result]
    assert dates == ["2024-01-25", "2024-02-15", "2024-02-20"]


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
    """A row missing 'report_date' or with garbage date is dropped."""
    p = _provider([
        {"report_date": "2024-01-25", "report_time": "after-hours"},
        {"report_time": "after-hours"},                       # missing report_date
        {"report_date": "garbage", "report_time": "unknown"}, # bad date
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].when.date().isoformat() == "2024-01-25"


# ---------------------------------------------------------------------------
# Cache sharing with next_catalyst
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_and_next_share_cache() -> None:
    """Calling both methods triggers each endpoint's HTTP fetch only once."""
    p = _provider([
        {"report_date": "2024-01-25", "report_time": "after-hours"},
    ])
    await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    )
    await p.next_catalyst("AAPL", datetime(2024, 1, 1, tzinfo=UTC))
    # Three endpoints (earnings, fda, econ), each fetched once across the
    # two calls; second invocation hits the per-endpoint TTL cache.
    assert p._client.request_json.await_count == 3
