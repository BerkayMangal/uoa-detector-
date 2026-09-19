"""Phase 5.3.2: the spot decision frame — entry, stop, size, risked, target, R.

Contract: ``docs/phase-5.3-spot-frame-acceptance.md`` §3.2 (the arithmetic), §3.3
(the profile block) and §6.1–6.4, §6.6 (these tests).

Every test states the rule it pins. The ones claiming a guard were verified by
mutation — the guard removed, the test failed — because two guards in this
project were found this week that could not fail (P39, P40).

The rules under test, in the contract's words:
  R-SP1  no bars, no stop — and the row says why
  R-SP2  no stop, no size. Never a fallback percentage
  R-SP3  zero shares is rendered; it is a fact about the account
  R-SP6  the position cap is disclosed, never silent
  R-SP8  stale spot, no frame
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from webapp.board.settings import SizingSettings, SpotSettings, load_board_settings
from webapp.board.spot import (
    REASON_NO_STOP_DISTANCE,
    REASON_NOT_ENOUGH_BARS,
    REASON_STALE_SPOT,
    Bar,
    build_spot_frame,
    true_ranges,
    wilder_atr,
)

_REPO = Path(__file__).resolve().parents[2]
_SIZING = SizingSettings(capital_usd=10_000.0, r_usd=100.0, values_confirmed_by_owner=False)


def _spot(**over: float | int) -> SpotSettings:
    base: dict[str, float | int] = {
        "atr_period": 3, "atr_stop_multiple": 1.5,
        "atr_min_sessions": 3, "max_position_pct_of_capital": 25.0,
    }
    base.update(over)
    return SpotSettings(**base)  # type: ignore[arg-type]


def _flat(n: int, *, high: float = 101.0, low: float = 99.0, close: float = 100.0) -> list[Bar]:
    """``n`` identical sessions whose True Range is exactly ``high - low``."""
    return [Bar(high=high, low=low, close=close) for _ in range(n)]


# ---------------------------------------------------------------------------
# §6.1 True Range and Wilder ATR against hand-computed numbers
# ---------------------------------------------------------------------------


def test_true_range_takes_the_widest_of_the_three_and_needs_a_previous_close() -> None:
    bars = [Bar(101.0, 99.0, 100.0), Bar(105.0, 104.0, 104.5), Bar(103.0, 96.0, 97.0)]
    ranges = true_ranges(bars)
    # The first session has no previous close, so it yields no range at all.
    assert ranges[0] is None
    # max(105-104=1, |105-100|=5, |104-100|=4) = 5 — the gap up, not the bar's own range.
    assert ranges[1] == 5.0
    # max(103-96=7, |103-104.5|=1.5, |96-104.5|=8.5) = 8.5 — the gap down.
    assert ranges[2] == 8.5


def test_wilder_atr_seeds_with_a_mean_then_smooths() -> None:
    """Seed = mean of the first ``period`` ranges; then (prev*(n-1) + new)/n."""
    # Ranges 2,2,2 then 8, period 3: seed = 2.0, next = (2*2 + 8)/3 = 4.0
    assert wilder_atr([2.0, 2.0, 2.0], 3) == pytest.approx(2.0)
    assert wilder_atr([2.0, 2.0, 2.0, 8.0], 3) == pytest.approx(4.0)
    # 2,2,4 period 3: (2+2+4)/3
    assert wilder_atr([2.0, 2.0, 4.0], 3) == pytest.approx(8.0 / 3.0)


def test_a_gap_breaks_the_window_instead_of_spanning_it() -> None:
    """§3.2: a session missing an input yields no TR and resets the run.

    Averaging across the hole would state a range for a session nobody measured.
    """
    assert wilder_atr([2.0, 2.0, None, 2.0, 2.0], 3) is None
    assert wilder_atr([2.0, 2.0, None, 2.0, 2.0, 2.0], 3) == pytest.approx(2.0)
    # A None inside the bars themselves does the same thing, end to end.
    bars = [*_flat(2), Bar(high=None, low=99.0, close=100.0), *_flat(2)]
    assert wilder_atr(true_ranges(bars), 3) is None


# ---------------------------------------------------------------------------
# §6.2–6.4, §6.6 the binding rules
# ---------------------------------------------------------------------------


def test_r_sp1_one_session_short_yields_no_stop_and_says_why() -> None:
    """One session short of ``atr_min_sessions``, with the ATR itself computable.

    The point is the DECLARED MINIMUM, not the ATR window: 5 bars give 4 usable
    ranges, enough for a period-3 ATR, so this row is refused only because the
    profile asks for 5 sessions and there are 4. An earlier version of this test
    used 3 bars, where the ATR is None anyway — it passed with the minimum check
    deleted, which made it worthless (P39/P40).
    """
    settings = _spot(atr_period=3, atr_min_sessions=5)
    bars = _flat(5)  # 5 bars -> 4 usable ranges (the first has no previous close)
    assert wilder_atr(true_ranges(bars), settings.atr_period) is not None, (
        "the fixture must have a computable ATR, or this test cannot isolate the minimum"
    )

    frame = build_spot_frame(bars, entry=100.0, direction="up", spot=settings, sizing=_SIZING)
    assert frame.reason == REASON_NOT_ENOUGH_BARS
    assert frame.atr is None, "a refused row states no ATR at all"
    assert frame.stop is None
    assert frame.shares is None
    assert frame.entry == 100.0, "the entry is known even when the stop is not"
    assert frame.sessions_used == 4, "the row can say how many sessions it did have"

    # One more session and the same row is allowed through.
    allowed = build_spot_frame(
        _flat(6), entry=100.0, direction="up", spot=settings, sizing=_SIZING,
    )
    assert allowed.reason is None
    assert allowed.stop is not None


def test_r_sp2_a_zero_stop_distance_yields_no_count_and_no_fallback() -> None:
    """A flat session series gives ATR 0, so there is no stop distance to size against."""
    frame = build_spot_frame(
        _flat(6, high=100.0, low=100.0, close=100.0),
        entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
    )
    assert frame.reason == REASON_NO_STOP_DISTANCE
    assert frame.atr == pytest.approx(0.0)
    assert frame.shares is None
    assert frame.stop is None


def test_r_sp3_an_unaffordable_share_is_rendered_as_zero_not_hidden() -> None:
    """One share risks more than R allows: the count is 0 and the row stays."""
    bars = _flat(6, high=200.0, low=0.0, close=100.0)  # TR = 200, ATR = 200
    frame = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
    )
    assert frame.reason is None, "this is a fact about the account, not an unknown"
    assert frame.shares == 0
    assert frame.risked_usd == pytest.approx(0.0)
    assert frame.stop_distance == pytest.approx(300.0)  # 1.5 x 200


def test_r_sp6_the_position_cap_binds_and_is_disclosed() -> None:
    """25% of $10,000 is $2,500; at $10 a share that is 250 shares, not 1,000."""
    bars = _flat(6, high=100.05, low=99.95, close=100.0)  # TR = 0.1 -> ATR 0.1
    frame = build_spot_frame(
        bars, entry=10.0, direction="up",
        spot=_spot(atr_stop_multiple=1.0), sizing=_SIZING,
    )
    assert frame.capped_by_position_limit is True
    assert frame.shares == 250
    assert frame.shares * frame.entry == pytest.approx(2_500.0)


def test_the_cap_stays_silent_when_it_does_not_bind() -> None:
    bars = _flat(6, high=105.0, low=95.0, close=100.0)  # TR = 10 -> ATR 10
    frame = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
    )
    assert frame.capped_by_position_limit is False
    assert frame.shares == 6  # 100 // (1.5 x 10) = 6


def test_r_sp8_a_stale_spot_kills_the_whole_frame() -> None:
    """An entry priced off a quote that no longer exists would size a real position."""
    frame = build_spot_frame(
        _flat(30), entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
        entry_is_stale=True,
    )
    assert frame.reason == REASON_STALE_SPOT
    assert (frame.entry, frame.atr, frame.stop, frame.shares) == (None, None, None, None)


def test_a_short_puts_the_stop_above_the_entry() -> None:
    bars = _flat(6, high=105.0, low=95.0, close=100.0)
    long_frame = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING)
    short_frame = build_spot_frame(
        bars, entry=100.0, direction="down", spot=_spot(), sizing=_SIZING)
    assert long_frame.stop == pytest.approx(85.0)
    assert short_frame.stop == pytest.approx(115.0)
    assert long_frame.stop_distance == short_frame.stop_distance


def test_r_to_target_is_the_expected_move_over_the_stop_distance() -> None:
    """§3.2: the target is the move moves.py already computes. Nothing new is derived."""
    bars = _flat(6, high=105.0, low=95.0, close=100.0)  # ATR 10, distance 15
    frame = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING, target=130.0,
    )
    assert frame.r_to_target == pytest.approx(2.0)
    # With no target there is no R, and none is invented.
    assert build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
    ).r_to_target is None


def test_risked_dollars_never_exceed_the_intended_risk() -> None:
    """The floor on the share count is what keeps this true, so it is asserted."""
    bars = _flat(6, high=105.0, low=95.0, close=100.0)
    frame = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
    )
    assert frame.risked_usd is not None
    assert frame.risk_usd == 100.0
    assert frame.risked_usd <= frame.risk_usd


# ---------------------------------------------------------------------------
# §3.3 the profile block
# ---------------------------------------------------------------------------


def test_the_board_profile_carries_the_spot_block() -> None:
    settings = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
    assert settings.spot.atr_period == 14
    assert settings.spot.atr_stop_multiple == 1.5  # owner decision O1
    assert settings.spot.atr_min_sessions == 20
    assert settings.spot.max_position_pct_of_capital == 25


def test_a_window_shorter_than_the_atr_period_is_rejected() -> None:
    """Accepting it would let the ATR be computed from fewer sessions than declared."""
    with pytest.raises(ValueError, match="atr_min_sessions"):
        SpotSettings(
            atr_period=14, atr_stop_multiple=1.5,
            atr_min_sessions=10, max_position_pct_of_capital=25.0,
        )


# ---------------------------------------------------------------------------
# 5.3.7: where the entry came from
# ---------------------------------------------------------------------------


def test_a_close_based_entry_carries_its_date_and_changes_no_arithmetic() -> None:
    """The provenance travels with the frame; the stop and the count do not move.

    A close is a different kind of fact from a live quote, so the row has to be
    able to say which it used. But it is still a price, so the arithmetic on top
    of it must be identical — otherwise the closed-session frame would quietly be
    a second, differently-computed thing.
    """
    bars = _flat(6, high=105.0, low=95.0, close=100.0)  # ATR 10 -> distance 15
    quoted = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
    )
    closed = build_spot_frame(
        bars, entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
        entry_as_of=date(2026, 9, 18), entry_is_close=True,
    )
    assert (closed.entry, closed.stop, closed.shares, closed.risked_usd) == (
        quoted.entry, quoted.stop, quoted.shares, quoted.risked_usd
    )
    assert closed.entry_is_close is True
    assert closed.entry_as_of == date(2026, 9, 18)
    # A quoted entry claims nothing extra, so nothing extra is rendered for it.
    assert quoted.entry_is_close is False
    assert quoted.entry_as_of is None


def test_a_refused_frame_states_no_entry_provenance() -> None:
    """R-SP8 still refuses: a frame with no entry must not carry a date either."""
    frame = build_spot_frame(
        _flat(30), entry=None, direction="up", spot=_spot(), sizing=_SIZING,
        entry_as_of=date(2026, 9, 18), entry_is_close=True,
    )
    assert frame.reason == REASON_STALE_SPOT
    assert frame.entry is None
    assert frame.entry_as_of is None, "a refused frame carries no provenance"
    assert frame.entry_is_close is False


def test_a_row_refused_for_bars_still_says_where_its_entry_came_from() -> None:
    """The entry survives R-SP1, so its provenance has to survive with it.

    Found by the render test, not by review: on a closed session every too-few-bars
    row rendered a bare ``giriş $50.00``, which reads as a live price. An entry the
    row displays must carry its date whenever it came from a close.
    """
    frame = build_spot_frame(
        _flat(4), entry=50.0, direction="up",
        spot=_spot(atr_period=3, atr_min_sessions=20), sizing=_SIZING,
        entry_as_of=date(2026, 9, 18), entry_is_close=True,
    )
    assert frame.reason == REASON_NOT_ENOUGH_BARS
    assert frame.entry == 50.0
    assert frame.entry_is_close is True
    assert frame.entry_as_of == date(2026, 9, 18)


def test_a_zero_stop_distance_keeps_the_entry_provenance_too() -> None:
    """R-SP2 also renders an entry, so it is the second place a bare price could hide."""
    frame = build_spot_frame(
        _flat(6, high=100.0, low=100.0, close=100.0),
        entry=100.0, direction="up", spot=_spot(), sizing=_SIZING,
        entry_as_of=date(2026, 9, 18), entry_is_close=True,
    )
    assert frame.reason == REASON_NO_STOP_DISTANCE
    assert frame.entry == 100.0
    assert frame.entry_is_close is True
    assert frame.entry_as_of == date(2026, 9, 18)
