"""Study F's harness: the four leaks §8 names, each with a test that fails without the fix.

Contract: ``docs/study-F-preregistration.md`` §8 ("The implementation must carry a
test for 1, 2, 3 and 4 that fails when the defect is introduced") and
``docs/study-F-preregistration-addendum.md`` ("Study F's implementation must carry
a test that fails when the purge is removed").

Every assertion here is arithmetic on the fixture's own numbers. The repository has
shipped four guards that could not fail (P39, P40, P44, P45), and the way that
happens is a test whose expectation is produced by the code it is testing.

No XGBoost is fitted here: the defects live in the panel builder, the fold
boundary, the standardiser and the analog predictor, none of which are the
estimator. The gate runs this file on every commit, so it stays fast.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from uoa_detector.research.features import Bar, features

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "study_f_runner.py"
_START = date(2025, 1, 2)


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("study_f_runner", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` resolves a string annotation by looking the module up in
    # ``sys.modules``; a module loaded from a spec and never registered there makes
    # every @dataclass in it raise on construction.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bars(n: int, *, start: float, step: float, volume: float = 5_000_000.0) -> list[Bar]:
    """A linear price path with a mildly varying volume.

    The variation is not decoration: ``price_volume_corr_20`` and ``volume_z_20``
    are None on a constant-volume series, and a None in any feature drops the row,
    so a flat fixture would produce an empty panel and every assertion below would
    be vacuous.
    """
    out: list[Bar] = []
    for i in range(n):
        close = start + step * i
        out.append(Bar(
            day=_START + timedelta(days=i), open=close - 0.25, high=close + 1.0,
            low=close - 1.0, close=close,
            volume=volume * (1.0 + 0.05 * ((i * 7) % 11)),
        ))
    return out


def _panel_input(n: int = 200) -> dict[str, list[Bar]]:
    """A market plus three names, each with a different drift.

    Dollar volume is well above the $20M filter for all four, so the liquidity
    rule is not what any assertion below is measuring.
    """
    return {
        "SPY": _bars(n, start=400.0, step=0.30),
        "AAA": _bars(n, start=100.0, step=0.50),
        "BBB": _bars(n, start=60.0, step=-0.10),
        "CCC": _bars(n, start=25.0, step=0.05),
    }


# ---------------------------------------------------------------------------
# §8 defect 4 and the addendum: the fold boundary
# ---------------------------------------------------------------------------


def test_the_training_window_stops_short_of_the_test_window_by_the_horizon() -> None:
    """The purge. Remove it and the last training label is built from test prices.

    At horizon 5 a training row at day t carries the return from t to t+5; with no
    purge the final training day is the day before the test fold opens, so all five
    sessions its label spans are test sessions.
    """
    runner = _runner()
    for horizon, expected_gap in ((1, 0), (5, 4)):
        for train, test in runner.folds(300, horizon):
            # The label at the last training day spans train.stop-1 .. +horizon.
            last_label_day = (train.stop - 1) + horizon
            assert last_label_day <= test.start, (
                f"horizon {horizon}: a training label reaches day {last_label_day}, "
                f"inside a test fold that opens at {test.start}"
            )
            assert test.start - train.stop == expected_gap


def test_folds_are_120_by_21_rolled_by_21_and_never_overlap() -> None:
    runner = _runner()
    windows = runner.folds(300, 1)
    assert len(windows) >= 2
    for index, (train, test) in enumerate(windows):
        assert len(test) == 21
        assert len(train) == 120, "rolling, not expanding: the window never grows"
        if index:
            previous_test = windows[index - 1][1]
            assert test.start == previous_test.stop, "step and test length disagree"
    starts = [train.start for train, _ in windows]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_the_day_index_counts_only_sessions_that_carry_rows() -> None:
    """A 120-session training window must be 120 POPULATED sessions.

    The panel's first 60 calendar sessions are warmup and hold no rows. Indexing the
    calendar rather than the panel would put them inside fold 0's training window,
    making it 60 real sessions while the protocol says 120 — and silently breaking
    the comparability with Study E that §2 rests on.
    """
    runner = _runner()
    sessions = 200
    panel = runner.build_panel(_panel_input(sessions), 1)
    assert len(panel.days) == sessions - runner.WARMUP - 1
    assert set(panel.day_of.tolist()) == set(range(len(panel.days))), (
        "a day index with no rows means the fold sizes are not what they say"
    )
    # Each populated session carries every liquid name, so the counts are exact.
    counts = {int(day): int((panel.day_of == day).sum()) for day in np.unique(panel.day_of)}
    assert set(counts.values()) == {len(panel.tickers)}


def test_a_purge_of_zero_would_be_caught_by_the_boundary_test() -> None:
    """The negative control: the assertion above is not vacuous.

    This reproduces the unpurged fold the audit found in ``study_e_signal`` and
    shows it violates the same predicate the real folds satisfy.
    """
    unpurged = (range(0, 120), range(120, 141))
    horizon = 5
    last_label_day = (unpurged[0].stop - 1) + horizon
    assert last_label_day > unpurged[1].start, (
        "the unpurged fold must violate the predicate, or the purge test proves nothing"
    )


# ---------------------------------------------------------------------------
# §8 defect 3: look-ahead in the panel
# ---------------------------------------------------------------------------


def test_changing_only_future_prices_moves_the_label_and_not_the_features() -> None:
    """Features at t come from the prefix; the label is the only forward-looking term."""
    runner = _runner()
    base = _panel_input()
    bumped = {ticker: list(bars) for ticker, bars in base.items()}
    # Raise every AAA close after day 150 by 50%. Rows at days <= 150 - horizon keep
    # their features; the labels of rows whose window reaches past 150 must move.
    bumped["AAA"] = [
        bar if index <= 150 else Bar(
            day=bar.day, open=bar.open * 1.5, high=bar.high * 1.5,
            low=bar.low * 1.5, close=bar.close * 1.5, volume=bar.volume,
        )
        for index, bar in enumerate(bumped["AAA"])
    ]

    before = runner.build_panel(base, 5)
    after = runner.build_panel(bumped, 5)
    assert before.x.shape == after.x.shape

    names = list(runner.PANEL_FEATURES)
    # Day index 0 is the first POPULATED session, which is calendar bar WARMUP, so a
    # calendar bar b sits at day index b - WARMUP. The bump starts at bar 151: rows
    # at bar <= 145 keep both their features and their label, and rows from bar 146
    # have a label reaching into the bumped days.
    last_clean = 145 - runner.WARMUP
    untouched = [row for row in range(len(before.y)) if before.day_of[row] <= last_clean]
    assert untouched, "the fixture produced no rows before the bump"
    for row in untouched:
        assert before.x[row].tolist() == pytest.approx(after.x[row].tolist()), (
            f"feature row {row} (day {before.day_of[row]}, "
            f"{before.ticker_of[row]}) changed when only future prices moved "
            f"(features: {names})"
        )
    assert not np.allclose(before.y, after.y), "the bump must move some label"
    # And specifically AAA's labels from calendar bar 146 on, where the bump lands.
    moved = [
        row for row in range(len(before.y))
        if before.ticker_of[row] == "AAA" and before.day_of[row] > last_clean
        and not math.isclose(before.y[row], after.y[row])
    ]
    assert moved, "the bump did not move the labels it was built to move"


def test_a_panel_row_holds_exactly_the_prefix_features_of_its_own_day() -> None:
    """The builder must hand ``features`` the prefix ending at t — not t+1.

    This is the assertion that actually catches §8's defect 3. The bump test above
    does not: it compares rows whose label window is clean, and at horizon 5 that
    window reaches further forward than the feature slice does, so a one-bar
    over-read still lands inside untouched data and the test stays green. Verified
    by mutation — ``bars[: i + 2]`` passed it.

    Recomputing the row against the library's own output for the same day has no
    such blind spot: any off-by-one in the slice makes the two disagree.
    """
    runner = _runner()
    bars = _panel_input()
    panel = runner.build_panel(bars, 1)
    names = runner.feature_names()
    for target_day in (0, 40, 100):
        row = next(
            index for index in range(len(panel.y))
            if panel.day_of[index] == target_day and panel.ticker_of[index] == "AAA"
        )
        bar_index = target_day + runner.WARMUP
        expected = features(
            bars["AAA"][: bar_index + 1], bars["SPY"][: bar_index + 1],
        )
        for column, name in enumerate(names):
            assert panel.x[row][column] == pytest.approx(expected[name]), (
                f"day {target_day} ({name}) disagrees with the library's own value"
            )


def test_the_label_is_the_forward_log_return_over_the_horizon() -> None:
    """Hand-computed from the fixture's closes, not from the runner's own arithmetic."""
    runner = _runner()
    bars = _panel_input()
    panel = runner.build_panel(bars, 5)
    aaa = bars["AAA"]
    # Day index 0 is the first populated session, which is calendar bar WARMUP; its
    # label spans bars WARMUP..WARMUP+5.
    expected = math.log(aaa[runner.WARMUP + 5].close / aaa[runner.WARMUP].close)
    first_row = min(
        (row for row in range(len(panel.y))
         if panel.day_of[row] == 0 and panel.ticker_of[row] == "AAA"),
        default=-1,
    )
    assert first_row >= 0, "no AAA row at the first usable session"
    assert panel.y[first_row] == pytest.approx(expected)


def test_the_frozen_feature_list_is_37_names_with_the_cross_sectional_one_last() -> None:
    """§4 fixes the count. The library cannot hold ``xs_rank_ret_20``; the panel adds it."""
    runner = _runner()
    assert len(runner.PANEL_FEATURES) == 37
    assert runner.PANEL_FEATURES[-1] == "xs_rank_ret_20"
    assert len(set(runner.PANEL_FEATURES)) == 37
    panel = runner.build_panel(_panel_input(), 1)
    assert panel.x.shape[1] == 37


def test_the_cross_sectional_rank_uses_only_that_days_names() -> None:
    """A rank that leaked across days would move when another day's values changed."""
    runner = _runner()
    values = np.asarray([1.0, 2.0, 3.0, 50.0, 60.0, 70.0])
    day_of = np.asarray([0, 0, 0, 1, 1, 1])
    ranks = runner._cross_sectional_rank(values, day_of)
    assert ranks.tolist() == pytest.approx([0.0, 0.5, 1.0, 0.0, 0.5, 1.0])
    # Day 1's values are 20x day 0's; if the rank were pooled they could not match.
    moved = values.copy()
    moved[3:] = [500.0, 600.0, 700.0]
    assert runner._cross_sectional_rank(moved, day_of).tolist() == pytest.approx(
        ranks.tolist(),
    )


# ---------------------------------------------------------------------------
# §8 defect 1: standardisation across the boundary
# ---------------------------------------------------------------------------


def test_standardisation_statistics_come_from_the_training_rows_alone() -> None:
    runner = _runner()
    train = np.asarray([[0.0], [2.0], [4.0]])
    test = np.asarray([[100.0], [200.0]])
    scaled_train, scaled_test = runner.standardise(train, test)
    # mean 2, population sd sqrt(8/3) — computed here, not by calling the module.
    mean, sd = 2.0, math.sqrt(8.0 / 3.0)
    assert scaled_train.ravel().tolist() == pytest.approx(
        [(0.0 - mean) / sd, 0.0, (4.0 - mean) / sd],
    )
    assert scaled_test.ravel().tolist() == pytest.approx(
        [(100.0 - mean) / sd, (200.0 - mean) / sd],
    )
    # The test rows must not have entered the statistics: pooled mean would be 61.2.
    assert scaled_test.min() > 30.0, "test rows leaked into the scaler"


def test_a_constant_column_is_zeroed_rather_than_dividing_by_zero() -> None:
    runner = _runner()
    train = np.asarray([[5.0], [5.0], [5.0]])
    scaled_train, scaled_test = runner.standardise(train, np.asarray([[5.0], [9.0]]))
    assert scaled_train.ravel().tolist() == [0.0, 0.0, 0.0]
    assert scaled_test.ravel().tolist() == pytest.approx([0.0, 4.0])


# ---------------------------------------------------------------------------
# §8 defect 2: the analog reaching into the test window
# ---------------------------------------------------------------------------


def test_the_analog_prediction_ignores_the_test_labels_entirely() -> None:
    """Its output is a mean of TRAINING labels; test outcomes are not an input."""
    runner = _runner()
    train_x = np.asarray([[1.0, 0.0], [0.99, 0.1], [0.0, 1.0], [0.1, 0.99]])
    train_y = np.asarray([0.10, 0.12, -0.10, -0.12])
    test_x = np.asarray([[1.0, 0.05], [0.05, 1.0]])
    predicted = runner.analog_predict(train_x, train_y, test_x, k=2)
    # Row 0 points along the first axis, so its two nearest are rows 0 and 1.
    assert predicted[0] == pytest.approx((0.10 + 0.12) / 2)
    assert predicted[1] == pytest.approx((-0.10 - 0.12) / 2)


def test_the_analog_cannot_choose_more_neighbours_than_it_was_given() -> None:
    runner = _runner()
    train_x = np.asarray([[1.0, 0.0], [0.0, 1.0]])
    train_y = np.asarray([0.2, -0.2])
    predicted = runner.analog_predict(train_x, train_y, np.asarray([[1.0, 1.0]]), k=25)
    assert predicted[0] == pytest.approx(0.0), "k is clipped to the training rows"


# ---------------------------------------------------------------------------
# §8 defect 5 and §6: the null
# ---------------------------------------------------------------------------


def test_the_null_permutes_within_a_day_and_never_across_days() -> None:
    """The cross-section survives; only the row-to-outcome pairing is destroyed."""
    runner = _runner()
    y = np.asarray([1.0, 2.0, 3.0, 40.0, 50.0, 60.0])
    day_of = np.asarray([0, 0, 0, 1, 1, 1])
    for seed in range(20):
        shuffled = runner.shuffle_within_day(y, day_of, np.random.default_rng(seed))
        assert sorted(shuffled[:3].tolist()) == [1.0, 2.0, 3.0]
        assert sorted(shuffled[3:].tolist()) == [40.0, 50.0, 60.0]
    assert y.tolist() == [1.0, 2.0, 3.0, 40.0, 50.0, 60.0], "the input was mutated"


def test_the_search_reports_the_best_of_the_ten_frozen_configurations() -> None:
    runner = _runner()
    configs = [runner.label_of(config) for config in runner.configurations()]
    assert len(configs) == 10
    assert len(set(configs)) == 10
    assert "ridge" in configs
    assert "analog" in configs
    assert sum(1 for name in configs if name.startswith("xgb_")) == 8


def test_the_best_of_a_result_is_the_maximum_and_carries_its_fold_share() -> None:
    runner = _runner()
    result = runner.SearchResult(
        mean_ic={"a": 0.01, "b": 0.03, "c": -0.02},
        per_fold={"a": [0.01], "b": [0.04, -0.01, 0.06, 0.03], "c": [-0.02]},
    )
    assert result.best == "b"
    assert result.best_ic == pytest.approx(0.03)
    assert result.positive_fold_share("b") == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# §3: the liquidity filter was declared before fitting and is applied as declared
# ---------------------------------------------------------------------------


def test_the_liquidity_filter_drops_names_below_twenty_million_dollars() -> None:
    runner = _runner()
    bars = {
        "RICH": _bars(80, start=100.0, step=0.1, volume=1_000_000.0),   # $10.0M+
        "POOR": _bars(80, start=2.0, step=0.01, volume=1_000_000.0),    # ~$2M
    }
    # RICH's median dollar volume is about $104M; POOR's is about $2.4M.
    assert runner.liquid_tickers(bars) == ["RICH"]
    assert runner.MIN_DOLLAR_VOLUME == 20_000_000.0


def test_the_pre_registered_constants_are_the_ones_in_the_document() -> None:
    """A constant drifting from the frozen protocol is the quietest way to cheat."""
    runner = _runner()
    assert (runner.TRAIN, runner.TEST, runner.STEP) == (120, 21, 21)
    assert runner.HORIZONS == (1, 5)
    assert runner.WARMUP == 60
    assert runner.ANALOG_K == 25
    assert runner.NULL_PASSES == 200
    assert runner.T1_PERCENTILE == 99.0
    assert runner.T2_POSITIVE_FOLD_SHARE == 0.75
    assert len(runner.XGB_GRID) == 8
    depths = sorted({config["max_depth"] for config in runner.XGB_GRID})
    trees = sorted({config["n_estimators"] for config in runner.XGB_GRID})
    rates = sorted({config["learning_rate"] for config in runner.XGB_GRID})
    assert (depths, trees, rates) == ([2, 3], [100, 300], [0.03, 0.1])
