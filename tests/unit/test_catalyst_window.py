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


def _provider(rows: list[dict[str, Any]]) -> UnusualWhalesCatalystCalendarProvider:
    """Build a UW provider with stubbed HTTP returning ``rows``."""
    fake_client = MagicMock()
    fake_client.request_json = AsyncMock(return_value={"data": rows})
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
        {"kind": "earnings", "when": "2024-01-25T21:00:00Z", "title": "Q1"},
        {"kind": "fda", "when": "2024-02-15T15:00:00Z", "title": "FDA"},
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
        {"kind": "earnings", "when": "2023-12-15T21:00:00Z", "title": "old"},
        {"kind": "fda", "when": "2024-02-15T15:00:00Z", "title": "FDA"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].title == "FDA"


@pytest.mark.asyncio
async def test_window_filters_events_after_end() -> None:
    p = _provider([
        {"kind": "earnings", "when": "2024-01-25T21:00:00Z", "title": "Q1"},
        {"kind": "fda", "when": "2024-04-15T15:00:00Z", "title": "future"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].title == "Q1"


@pytest.mark.asyncio
async def test_window_includes_boundary_events() -> None:
    """Events exactly at start or end are included (inclusive bounds)."""
    p = _provider([
        {"kind": "earnings", "when": "2024-01-15T00:00:00Z", "title": "start"},
        {"kind": "fda", "when": "2024-02-28T00:00:00Z", "title": "end"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 2, 28, tzinfo=UTC),
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_window_returns_ascending_sort() -> None:
    """Output is sorted ascending by when, regardless of input order."""
    p = _provider([
        {"kind": "fda", "when": "2024-02-15T15:00:00Z", "title": "B"},
        {"kind": "earnings", "when": "2024-01-25T21:00:00Z", "title": "A"},
        {"kind": "fomc", "when": "2024-02-20T18:00:00Z", "title": "C"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    titles = [e.title for e in result]
    assert titles == ["A", "B", "C"]


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
    """A row missing 'when' is dropped; valid rows continue."""
    p = _provider([
        {"kind": "earnings", "when": "2024-01-25T21:00:00Z", "title": "good"},
        {"kind": "fda", "title": "no_when"},  # malformed
        {"kind": "fomc", "when": "garbage", "title": "bad_when"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].title == "good"


@pytest.mark.asyncio
async def test_window_unknown_kind_normalised_to_other() -> None:
    """An unrecognised 'kind' is mapped to 'other' (not dropped)."""
    p = _provider([
        {"kind": "weird_unknown", "when": "2024-01-25T21:00:00Z", "title": "x"},
    ])
    result = await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].kind == "other"


# ---------------------------------------------------------------------------
# Cache sharing with next_catalyst
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_and_next_share_cache() -> None:
    """Calling both methods triggers the HTTP fetch only once."""
    p = _provider([
        {"kind": "earnings", "when": "2024-01-25T21:00:00Z", "title": "Q1"},
    ])
    await p.catalysts_in_window(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    )
    await p.next_catalyst("AAPL", datetime(2024, 1, 1, tzinfo=UTC))
    # Inspect the underlying mock; only one .request_json call expected
    assert p._client.request_json.await_count == 1
