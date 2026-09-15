"""Phase 3.6.6 — CSV catalyst calendar provider (M22) tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from uoa_detector.providers.catalyst_calendar import CatalystCalendarProvider
from uoa_detector.sources.thetadata_derived.catalyst_calendar import (
    CSVCatalystCalendarProvider,
)


def _csv(tmp_path: Path) -> Path:
    p = tmp_path / "earnings.csv"
    p.write_text(
        "ticker,when,kind,title\n"
        "JNJ,2025-07-16T21:00:00+00:00,earnings,JNJ earnings 2025-07-16\n"
        "JNJ,2025-10-14T21:00:00+00:00,earnings,JNJ earnings 2025-10-14\n"
        "TSLA,2025-07-23T21:00:00+00:00,earnings,TSLA earnings 2025-07-23\n",
        encoding="utf-8",
    )
    return p


def _at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


@pytest.mark.asyncio
async def test_next_catalyst(tmp_path: Path) -> None:
    p = CSVCatalystCalendarProvider(_csv(tmp_path))
    nxt = await p.next_catalyst("JNJ", _at(2025, 8, 1))
    assert nxt is not None
    assert nxt.when == datetime(2025, 10, 14, 21, 0, tzinfo=UTC)
    # on/after the last → None
    assert await p.next_catalyst("JNJ", _at(2025, 11, 1)) is None
    # unknown ticker → None
    assert await p.next_catalyst("NOPE", _at(2025, 8, 1)) is None


@pytest.mark.asyncio
async def test_catalysts_in_window(tmp_path: Path) -> None:
    p = CSVCatalystCalendarProvider(_csv(tmp_path))
    win = await p.catalysts_in_window("JNJ", _at(2025, 7, 1), _at(2025, 8, 1))
    assert len(win) == 1
    assert win[0].when == datetime(2025, 7, 16, 21, 0, tzinfo=UTC)
    # window spanning both → sorted, both
    both = await p.catalysts_in_window("JNJ", _at(2025, 7, 1), _at(2025, 11, 1))
    assert [e.when.month for e in both] == [7, 10]
    # empty window → ()
    assert await p.catalysts_in_window("JNJ", _at(2026, 1, 1), _at(2026, 2, 1)) == ()


@pytest.mark.asyncio
async def test_kind_and_ticker_parsed(tmp_path: Path) -> None:
    p = CSVCatalystCalendarProvider(_csv(tmp_path))
    ev = await p.next_catalyst("tsla", _at(2025, 7, 1))  # case-insensitive
    assert ev is not None
    assert ev.kind == "earnings"
    assert ev.ticker == "TSLA"


def test_provider_satisfies_protocol(tmp_path: Path) -> None:
    p = CSVCatalystCalendarProvider(_csv(tmp_path))
    assert isinstance(p, CatalystCalendarProvider)
