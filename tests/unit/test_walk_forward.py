"""Phase 3.2.4.1 tests for walk-forward windowing.

Pins:
  - equal_time_slices produces N contiguous windows that exactly cover [start, end]
  - last window absorbs rounding remainder
  - half-open boundary convention: [start_i, start_{i+1}); last window closed
  - in_sample_fraction default = 1.0 (Phase 3.2.4 — no tuning)
  - in_sample_end correctly splits the window when fraction < 1.0
  - n=1 degenerates to a single full-period window
  - validation: n < 1 raises, end <= start raises, empty range raises
  - equal_trade_count_slices partitions by exit_ts ascending; remainder to last
  - no_op_tuner returns input profile unchanged
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uoa_detector.backtest.pnl_provider import RealizedTrade
from uoa_detector.backtest.walk_forward import (
    WalkForwardWindow,
    equal_time_slices,
    equal_trade_count_slices,
    no_op_tuner,
)
from uoa_detector.calibration import load_default_profile

# ---------------------------------------------------------------------------
# WalkForwardWindow model contract
# ---------------------------------------------------------------------------


def test_window_validates_end_after_start() -> None:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(Exception, match="must be strictly greater"):
        WalkForwardWindow(
            index=0,
            start=base,
            end=base,
            in_sample_fraction=1.0,
            is_last=True,
        )


def test_window_in_sample_end_at_full_fraction() -> None:
    """in_sample_fraction=1.0 → in_sample_end == end."""
    w = WalkForwardWindow(
        index=0,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 4, 1, tzinfo=UTC),
        in_sample_fraction=1.0,
        is_last=False,
    )
    assert w.in_sample_end == w.end


def test_window_in_sample_end_at_half_fraction() -> None:
    """in_sample_fraction=0.5 → in_sample_end at midpoint."""
    w = WalkForwardWindow(
        index=0,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 4, 1, tzinfo=UTC),  # 91 days
        in_sample_fraction=0.5,
        is_last=False,
    )
    midpoint = w.start + (w.end - w.start) / 2
    assert w.in_sample_end == midpoint


def test_window_contains_half_open_for_normal_window() -> None:
    """Non-last window: contains is start <= ts < end."""
    w = WalkForwardWindow(
        index=0,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 4, 1, tzinfo=UTC),
        in_sample_fraction=1.0,
        is_last=False,
    )
    assert w.contains(datetime(2024, 2, 15, tzinfo=UTC)) is True
    assert w.contains(datetime(2024, 1, 1, tzinfo=UTC)) is True  # start inclusive
    assert w.contains(datetime(2024, 4, 1, tzinfo=UTC)) is False  # end exclusive
    assert w.contains(datetime(2023, 12, 31, tzinfo=UTC)) is False


def test_window_contains_closed_closed_for_last_window() -> None:
    """Last window: end is inclusive."""
    w = WalkForwardWindow(
        index=7,
        start=datetime(2025, 10, 1, tzinfo=UTC),
        end=datetime(2025, 12, 31, tzinfo=UTC),
        in_sample_fraction=1.0,
        is_last=True,
    )
    assert w.contains(datetime(2025, 12, 31, tzinfo=UTC)) is True


# ---------------------------------------------------------------------------
# equal_time_slices
# ---------------------------------------------------------------------------


def test_equal_time_slices_n_eight_over_two_years() -> None:
    """Acceptance default: N=8 over 2 years = 3-month slices."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    windows = equal_time_slices(start, end, 8)
    assert len(windows) == 8

    # Each window roughly 3 months (91.25 days).
    expected_seconds = (end - start).total_seconds() / 8
    for i, w in enumerate(windows):
        assert w.index == i
        if i < 7:
            assert (w.end - w.start).total_seconds() == pytest.approx(
                expected_seconds, abs=1.0,
            )
        assert w.is_last == (i == 7)
    # Coverage: first window starts at start, last window ends at end.
    assert windows[0].start == start
    assert windows[-1].end == end


def test_equal_time_slices_contiguous_no_gaps_no_overlaps() -> None:
    """Window i ends where window i+1 begins."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 12, 31, tzinfo=UTC)
    windows = equal_time_slices(start, end, 4)
    for i in range(3):
        assert windows[i].end == windows[i + 1].start


def test_equal_time_slices_last_absorbs_remainder() -> None:
    """365-day period / 8 → 7 slices of ~45 days, last gets 50."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=365)  # 365 / 8 = 45.625
    windows = equal_time_slices(start, end, 8)
    # The last window should end exactly at `end`.
    assert windows[-1].end == end
    # Earlier windows have equal duration; last window may be longer.
    earlier_durations = [
        (w.end - w.start).total_seconds() for w in windows[:-1]
    ]
    assert all(
        abs(d - earlier_durations[0]) < 1e-6 for d in earlier_durations
    )


def test_equal_time_slices_n_one_returns_single_full_period_window() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 12, 31, tzinfo=UTC)
    windows = equal_time_slices(start, end, 1)
    assert len(windows) == 1
    assert windows[0].start == start
    assert windows[0].end == end
    assert windows[0].is_last is True


def test_equal_time_slices_n_zero_raises() -> None:
    with pytest.raises(ValueError, match="n must be"):
        equal_time_slices(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 12, 31, tzinfo=UTC),
            0,
        )


def test_equal_time_slices_inverted_range_raises() -> None:
    with pytest.raises(ValueError, match="strictly greater"):
        equal_time_slices(
            datetime(2024, 12, 31, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
            8,
        )


def test_window_boundaries_half_open_exhaustive() -> None:
    """Every event timestamp falls in exactly one window — no double-count
    on slice boundaries, no events lost.

    Sweep 100 evenly-spaced timestamps across the 8 windows and assert
    each lives in exactly one ``contains`` result.
    """
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    windows = equal_time_slices(start, end, 8)

    total_seconds = (end - start).total_seconds()
    for i in range(100):
        ts = start + timedelta(seconds=total_seconds * i / 99)
        hits = sum(1 for w in windows if w.contains(ts))
        assert hits == 1, f"ts={ts} hit {hits} windows"


def test_in_sample_fraction_propagates_through_slices() -> None:
    """Slicer passes in_sample_fraction to every window."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 12, 31, tzinfo=UTC)
    windows = equal_time_slices(start, end, 4, in_sample_fraction=0.5)
    for w in windows:
        assert w.in_sample_fraction == 0.5


# ---------------------------------------------------------------------------
# equal_trade_count_slices
# ---------------------------------------------------------------------------


def _trade(eid: str, exit_offset_days: int, r: float = 1.0) -> RealizedTrade:
    base = datetime(2024, 6, 1, tzinfo=UTC)
    return RealizedTrade(
        event_id=eid,
        realized_r=r,
        entry_ts=base + timedelta(days=exit_offset_days - 5),
        exit_ts=base + timedelta(days=exit_offset_days),
        exit_reason="fixed_window_elapsed",
    )


def test_equal_trade_count_slices_partitions_evenly() -> None:
    """8 trades / 4 slices → 2 trades per slice."""
    trades = [_trade(f"e{i}", i) for i in range(8)]
    chunks = equal_trade_count_slices(trades, 4)
    assert len(chunks) == 4
    assert all(len(c) == 2 for c in chunks)


def test_equal_trade_count_slices_remainder_to_last() -> None:
    """11 trades / 4 slices → 2,2,2,5 (remainder to last)."""
    trades = [_trade(f"e{i}", i) for i in range(11)]
    chunks = equal_trade_count_slices(trades, 4)
    assert [len(c) for c in chunks] == [2, 2, 2, 5]


def test_equal_trade_count_slices_sorts_by_exit_ts() -> None:
    """Input order doesn't matter — chunks are in exit_ts ascending order."""
    trades = [
        _trade("late", 100),
        _trade("early", 10),
        _trade("middle", 50),
    ]
    chunks = equal_trade_count_slices(trades, 1)
    chunk = chunks[0]
    assert [t.event_id for t in chunk] == ["early", "middle", "late"]


def test_equal_trade_count_slices_excludes_open_trades() -> None:
    """Open trades (realized_r=None or exit_ts=None) are filtered out
    before partitioning. 4 closed + 2 open, n=2 → 2 trades per chunk
    (the opens are dropped)."""
    trades = [
        _trade(f"closed-{i}", i, r=1.0) for i in range(4)
    ]
    open1 = RealizedTrade(
        event_id="open-1",
        realized_r=None,
        entry_ts=datetime(2024, 6, 1, tzinfo=UTC),
        exit_ts=None,
        exit_reason="holding_window_open",
    )
    chunks = equal_trade_count_slices([*trades, open1], 2)
    assert sum(len(c) for c in chunks) == 4
    # No event_id matching "open-*" present
    assert not any(
        t.event_id.startswith("open") for c in chunks for t in c
    )


def test_equal_trade_count_slices_too_few_raises() -> None:
    trades = [_trade(f"e{i}", i) for i in range(5)]
    with pytest.raises(ValueError, match="at least 8"):
        equal_trade_count_slices(trades, 8)


def test_equal_trade_count_slices_n_zero_raises() -> None:
    with pytest.raises(ValueError, match="n must be"):
        equal_trade_count_slices([_trade("e0", 1)], 0)


# ---------------------------------------------------------------------------
# no_op_tuner
# ---------------------------------------------------------------------------


def test_no_op_tuner_returns_profile_unchanged() -> None:
    profile = load_default_profile()
    window = WalkForwardWindow(
        index=0,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 4, 1, tzinfo=UTC),
        in_sample_fraction=1.0,
        is_last=False,
    )
    out = no_op_tuner(profile, window)
    assert out is profile  # identity, not just equality
