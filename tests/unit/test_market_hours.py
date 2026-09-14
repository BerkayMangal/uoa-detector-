"""Unit tests for the RTH gate used by the live UW poll loops (Phase 4.28)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from uoa_detector.sources.market_hours import is_market_open

# 2026-06-18 is a Thursday; 2026-06-20 a Saturday, 2026-06-21 a Sunday.
_THU = "2026-06-18"
_SAT = "2026-06-20"
_SUN = "2026-06-21"


def _utc(day: str, hh: int, mm: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00+00:00")


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (_utc(_THU, 19, 0), True),    # mid-session
        (_utc(_THU, 13, 30), True),   # open boundary (inclusive)
        (_utc(_THU, 21, 0), True),    # close boundary (inclusive)
        (_utc(_THU, 13, 29), False),  # one minute before open
        (_utc(_THU, 21, 1), False),   # one minute after close
        (_utc(_THU, 3, 0), False),    # overnight
        (_utc(_SAT, 19, 0), False),   # Saturday, in-window time
        (_utc(_SUN, 19, 0), False),   # Sunday, in-window time
    ],
)
def test_is_market_open(when: datetime, expected: bool) -> None:
    assert is_market_open(when) is expected


def test_naive_datetime_treated_as_utc() -> None:
    # A naive timestamp is assumed UTC, not rejected.
    assert is_market_open(datetime(2026, 6, 18, 19, 0)) is True
    assert is_market_open(datetime(2026, 6, 18, 3, 0)) is False


def test_non_utc_tz_is_normalised() -> None:
    # 15:00 at UTC-4 == 19:00 UTC == open; 09:00 at UTC-4 == 13:00 UTC == closed.
    et = timezone(timedelta(hours=-4))
    assert is_market_open(datetime(2026, 6, 18, 15, 0, tzinfo=et)) is True
    assert is_market_open(datetime(2026, 6, 18, 9, 0, tzinfo=et)) is False


def test_utc_alias_import() -> None:
    # Sanity: the module's UTC is usable for callers passing datetime.now(UTC).
    assert is_market_open(datetime(2026, 6, 18, 19, 0, tzinfo=UTC)) is True
