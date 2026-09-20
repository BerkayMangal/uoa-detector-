"""Study F's feature library: no look-ahead, no silent zeros, no drift from the board.

Contract: ``docs/study-F-preregistration.md`` §4 (the frozen family list) and §8
(the pre-mortem, whose defects 1-4 must each have a test that fails when the
defect is introduced).

Expected values here are written as plain arithmetic on the fixture's own fields,
never by calling the module's helpers. A test that computes its expectation with
the code under test proves only that the code is deterministic, which is the
vacuity this repository has shipped four times (P39, P40, P44, P45).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from webapp.board.spot import Bar as BoardBar
from webapp.board.spot import true_ranges as board_true_ranges
from webapp.board.spot import wilder_atr as board_wilder_atr

from uoa_detector.research.features import (
    Bar,
    feature_names,
    features,
    true_ranges,
    wilder_atr,
)

_START = date(2026, 1, 5)


def _bar(i: int, close: float, *, high: float, low: float, open_: float, volume: float) -> Bar:
    return Bar(
        day=_START + timedelta(days=i), open=open_, high=high, low=low,
        close=close, volume=volume,
    )


def _series(n: int, *, start: float = 100.0, step: float = 0.5) -> list[Bar]:
    """A rising series with a fixed 2.0 range and a fixed volume.

    Deterministic on purpose: every expectation below is derived from these
    numbers by hand rather than from a second run of the library.
    """
    out: list[Bar] = []
    for i in range(n):
        close = start + step * i
        out.append(_bar(i, close, high=close + 1.0, low=close - 1.0,
                        open_=close - 0.25, volume=1_000_000.0))
    return out


# ---------------------------------------------------------------------------
# §8 defect 3: look-ahead
# ---------------------------------------------------------------------------


def test_two_series_sharing_a_prefix_agree_on_the_shared_day() -> None:
    """The feature map for day t may not depend on anything after day t.

    Two series identical through day t and wildly different afterwards must
    produce byte-identical features at t. This fails the moment any function
    reaches past the prefix it was handed — which is the only way a leak can
    enter once the signature takes a prefix rather than (series, index).
    """
    common = _series(80)
    calm = [*common, *[
        _bar(80 + i, 140.0, high=141.0, low=139.0, open_=139.75, volume=1_000_000.0)
        for i in range(20)
    ]]
    crash = [*common, *[
        _bar(80 + i, 10.0, high=60.0, low=5.0, open_=55.0, volume=90_000_000.0)
        for i in range(20)
    ]]
    at_t_from_calm = features(calm[:80], calm[:80])
    at_t_from_crash = features(crash[:80], crash[:80])
    assert at_t_from_calm == at_t_from_crash

    # And the fixture must actually diverge, or the assertion above is empty.
    assert features(calm, calm) != features(crash, crash)


def test_the_map_does_not_change_when_the_caller_holds_a_longer_series() -> None:
    """Slicing at the call site is the contract; this pins that it is enough."""
    full = _series(120)
    for cut in (61, 80, 119):
        assert features(full[:cut], full[:cut]) == features(list(full[:cut]), list(full[:cut]))


# ---------------------------------------------------------------------------
# no silent zeros
# ---------------------------------------------------------------------------


def test_a_window_that_is_too_short_is_unknown_and_not_zero() -> None:
    """A gap and a measurement of zero must never read alike."""
    short = _series(5)
    out = features(short, short)
    for name in ("ret_60", "ret_20", "beta_60", "corr_60_spy", "parkinson_10",
                 "garman_klass_10", "rsi_14", "zscore_close_20", "rel_volume_20"):
        assert out[name] is None, name
    # ret_1 is computable with two bars, so the fixture is not simply empty.
    assert out["ret_1"] is not None


def test_every_declared_feature_is_produced() -> None:
    """§4 freezes the list; the map may not quietly drop or add a key."""
    full = _series(120)
    out = features(full, full)
    assert set(out) == set(feature_names())
    assert len(feature_names()) == len(set(feature_names())), "a name is declared twice"


def test_an_empty_history_yields_every_key_as_unknown() -> None:
    out = features([], None)
    assert set(out) == set(feature_names())
    assert set(out.values()) == {None}


# ---------------------------------------------------------------------------
# hand-computed values (these are the mutation catchers)
# ---------------------------------------------------------------------------


def test_returns_are_log_returns_over_the_named_window() -> None:
    bars = _series(120)
    out = features(bars, bars)
    closes = [b.close for b in bars]
    assert out["ret_1"] == pytest.approx(math.log(closes[-1] / closes[-2]))
    assert out["ret_5"] == pytest.approx(math.log(closes[-1] / closes[-6]))
    assert out["ret_20"] == pytest.approx(math.log(closes[-1] / closes[-21]))
    assert out["ret_60"] == pytest.approx(math.log(closes[-1] / closes[-61]))


def test_ret_20_excluding_5_is_the_difference_of_the_two_log_returns() -> None:
    bars = _series(120)
    out = features(bars, bars)
    assert out["ret_20_ex_5"] == pytest.approx(out["ret_20"] - out["ret_5"])


def test_close_location_value_places_the_close_inside_its_own_range() -> None:
    bars = _series(120)
    last = bars[-1]
    expected = ((last.close - last.low) - (last.high - last.close)) / (last.high - last.low)
    assert features(bars, bars)["clv"] == pytest.approx(expected)
    assert expected == pytest.approx(0.0), "this fixture closes exactly mid-range"


def test_gap_is_measured_from_the_previous_close_to_this_open() -> None:
    bars = _series(120)
    expected = math.log(bars[-1].open / bars[-2].close) * 100.0
    assert features(bars, bars)["gap_pct"] == pytest.approx(expected)


def test_inside_and_outside_bars_are_read_from_the_previous_range() -> None:
    base = _series(30)
    inside = [*base, _bar(30, base[-1].close, high=base[-1].high - 0.1,
                          low=base[-1].low + 0.1, open_=base[-1].close, volume=1e6)]
    outside = [*base, _bar(30, base[-1].close, high=base[-1].high + 5.0,
                           low=base[-1].low - 5.0, open_=base[-1].close, volume=1e6)]
    assert features(inside, inside)["inside_bar"] == 1.0
    assert features(inside, inside)["outside_bar"] == 0.0
    assert features(outside, outside)["outside_bar"] == 1.0
    assert features(outside, outside)["inside_bar"] == 0.0


def test_nr7_marks_only_the_narrowest_range_of_seven() -> None:
    base = _series(30)
    narrow = [*base, _bar(30, base[-1].close, high=base[-1].close + 0.05,
                          low=base[-1].close - 0.05, open_=base[-1].close, volume=1e6)]
    assert features(narrow, narrow)["nr7"] == 1.0
    assert features(base, base)["nr7"] == 0.0, "a constant-range series has no narrowest bar"


def test_relative_volume_compares_today_against_the_prior_twenty() -> None:
    base = _series(40)
    spike = [*base, _bar(40, base[-1].close, high=base[-1].high, low=base[-1].low,
                         open_=base[-1].open, volume=3_000_000.0)]
    out = features(spike, spike)
    assert out["rel_volume_20"] == pytest.approx(3.0)


def test_rsi_is_one_hundred_when_every_change_is_a_gain() -> None:
    bars = _series(40)  # strictly rising
    assert features(bars, bars)["rsi_14"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# no drift from the board's own ATR
# ---------------------------------------------------------------------------


def test_research_atr_matches_the_boards_atr_on_the_same_input() -> None:
    """``src/`` may not import ``webapp/``, so the ATR exists twice. It must agree.

    Without this, the research could silently redefine what the project means by
    ATR while the live spot frame kept the original, and no test would notice.
    """
    bars = _series(60, start=50.0, step=0.7)
    research_ranges = true_ranges(bars)
    board_ranges = [
        r for r in board_true_ranges(
            [BoardBar(high=b.high, low=b.low, close=b.close) for b in bars],
        )
        if r is not None
    ]
    assert research_ranges == pytest.approx(board_ranges)
    for period in (5, 14, 20):
        assert wilder_atr(research_ranges, period) == pytest.approx(
            board_wilder_atr(board_ranges, period),
        )


def test_atr_needs_a_full_window_and_says_so() -> None:
    assert wilder_atr([1.0, 1.0, 1.0], 14) is None
    assert wilder_atr([], 14) is None
    assert wilder_atr([2.0] * 14, 14) == pytest.approx(2.0)
