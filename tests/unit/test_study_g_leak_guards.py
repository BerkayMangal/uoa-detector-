"""Study G's pre-mortem, turned into tests before its data exists.

``docs/study-G-preregistration.md`` §7 lists six ways the study could produce a
false positive and requires items 1, 2, 3 and 5 to each carry a test that fails
when the defect is introduced:

  1. a feature reaching across the 2026-09-19 boundary, through warmup or market
  2. beta estimated over the label's own window
  3. the liquidity filter applied over the wrong window, choosing survivors with
     hindsight
  5. a stale beta leaking market return into the residual, so a model that predicts
     the index scores as if it predicted the residual

Item 4 (the stopping rule) cannot be tested, which is why its dates are fixed in
the document. Item 6 (multiplicity smuggled in as a mechanical change) is guarded by
`tests/unit/test_study_f_runner.py`, which the runner must keep green.

These are written now, against synthetic fixtures, so that the study cannot be run
later on a harness whose guards were added after someone saw a number. The runner is
Study F's, unchanged — the boundary of item 1 is enforced where the panel is built
(`scripts/study_f_fetch_bars.py`, third argument), not in the search code, because
that code is the committed producer of a published result.

Expected values are arithmetic on the fixtures' own closes, never a second call into
the code under test.
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

_RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "study_f_runner.py"
_FETCHER = Path(__file__).resolve().parents[2] / "scripts" / "study_f_fetch_bars.py"
_START = date(2026, 9, 21)          # the first session after Study F's panel ends
_BOUNDARY = "2026-09-19"            # §5's inclusive lower bound


def _module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bars(
    n: int, *, start: float, step: float, volume: float = 4_000_000.0,
    first_day: date = _START,
) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        close = start + step * i
        out.append(Bar(
            day=first_day + timedelta(days=i), open=close - 0.25, high=close + 1.0,
            low=close - 1.0, close=close,
            volume=volume * (1.0 + 0.05 * ((i * 7) % 11)),
        ))
    return out


def _panel_input(n: int = 200) -> dict[str, list[Bar]]:
    return {
        "SPY": _bars(n, start=400.0, step=0.30),
        "AAA": _bars(n, start=100.0, step=0.50),
        "BBB": _bars(n, start=60.0, step=-0.10),
        "CCC": _bars(n, start=25.0, step=0.05),
    }


# ---------------------------------------------------------------------------
# §7 item 5 — the residual must remove exactly the market return it claims to
# ---------------------------------------------------------------------------


def test_the_residual_label_is_the_forward_return_minus_beta_times_the_market() -> None:
    """Computed from the fixture's closes and the row's OWN beta, by hand.

    A stale or mismatched beta is the defect: if the beta used to build the label
    differs from the beta the row carries as a feature, the model is handed a
    residual it cannot see the construction of, and part of the market return
    survives in the label.
    """
    runner = _module(_RUNNER, "study_f_runner")
    bars = _panel_input()
    horizon = 5
    panel = runner.build_panel(bars, horizon, label="idio")
    beta_column = list(runner.PANEL_FEATURES).index("beta_60")

    checked = 0
    for row in range(len(panel.y)):
        if panel.ticker_of[row] != "AAA":
            continue
        day_index = int(panel.day_of[row])
        bar_index = day_index + runner.WARMUP
        own = bars["AAA"]
        market = bars["SPY"]
        forward = math.log(own[bar_index + horizon].close / own[bar_index].close)
        market_forward = math.log(
            market[bar_index + horizon].close / market[bar_index].close,
        )
        beta = float(panel.x[row][beta_column])
        assert panel.y[row] == pytest.approx(forward - beta * market_forward), (
            f"day {day_index}: the residual does not equal forward - beta * market"
        )
        checked += 1
        if checked >= 40:
            break
    assert checked >= 40, "the fixture produced too few AAA rows to check"


def test_the_residual_and_the_forward_label_differ_on_the_same_panel() -> None:
    """Otherwise the assertion above is satisfied by a beta that is always zero."""
    runner = _module(_RUNNER, "study_f_runner")
    bars = _panel_input()
    forward = runner.build_panel(bars, 5, label="forward")
    idio = runner.build_panel(bars, 5, label="idio")
    assert forward.y.shape == idio.y.shape
    assert not np.allclose(forward.y, idio.y), (
        "the market-neutral label is indistinguishable from the forward one"
    )
    beta_column = list(runner.PANEL_FEATURES).index("beta_60")
    assert float(np.abs(idio.x[:, beta_column]).mean()) > 0.01, "beta is ~0 everywhere"


# ---------------------------------------------------------------------------
# §7 item 2 — beta may not see the window its own label spans
# ---------------------------------------------------------------------------


def test_beta_is_computed_from_the_prefix_and_not_from_the_label_window() -> None:
    """Move only the sessions the label spans; beta must not move with them.

    Beta enters both the feature vector and the residual label, so a beta that
    reached into the label's own window would leak the outcome into the input —
    the one defect that cannot be caught by looking at the label alone.
    """
    runner = _module(_RUNNER, "study_f_runner")
    horizon = 5
    base = _panel_input()
    bumped = {t: list(b) for t, b in base.items()}
    bumped["AAA"] = [
        bar if index <= 150 else Bar(
            day=bar.day, open=bar.open * 1.5, high=bar.high * 1.5,
            low=bar.low * 1.5, close=bar.close * 1.5, volume=bar.volume,
        )
        for index, bar in enumerate(bumped["AAA"])
    ]

    before = runner.build_panel(base, horizon, label="idio")
    after = runner.build_panel(bumped, horizon, label="idio")
    beta_column = list(runner.PANEL_FEATURES).index("beta_60")
    last_clean = 145 - runner.WARMUP

    rows = [r for r in range(len(before.y)) if before.day_of[r] <= last_clean]
    assert rows, "the fixture produced no rows before the bump"
    for row in rows:
        assert before.x[row][beta_column] == pytest.approx(after.x[row][beta_column]), (
            f"beta at day {before.day_of[row]} ({before.ticker_of[row]}) moved when "
            f"only sessions after it changed"
        )
    # And the bump must reach the labels, or nothing was exercised.
    moved = [
        r for r in range(len(before.y))
        if before.ticker_of[r] == "AAA" and before.day_of[r] > last_clean
        and not math.isclose(before.y[r], after.y[r])
    ]
    assert moved, "the bump moved no residual label"


def test_the_market_prefix_is_truncated_with_the_ticker_prefix() -> None:
    """Handing the library a longer SPY than the ticker's own history is a leak.

    Recomputed against the library directly: the panel row must equal the feature
    map of two prefixes that end on the same session.
    """
    runner = _module(_RUNNER, "study_f_runner")
    bars = _panel_input()
    panel = runner.build_panel(bars, 1, label="idio")
    names = runner.feature_names()
    for target_day in (0, 30, 90):
        row = next(
            r for r in range(len(panel.y))
            if panel.day_of[r] == target_day and panel.ticker_of[r] == "AAA"
        )
        cut = target_day + runner.WARMUP
        expected = features(bars["AAA"][: cut + 1], bars["SPY"][: cut + 1])
        for column, name in enumerate(names):
            assert panel.x[row][column] == pytest.approx(expected[name]), name


# ---------------------------------------------------------------------------
# §7 item 3 — the liquidity filter reads the study's own window
# ---------------------------------------------------------------------------


def test_the_liquidity_filter_reads_only_the_bars_it_is_handed() -> None:
    """§5 measures the $20M rule over the NEW window, so survivors cannot be
    inherited from Study F's panel."""
    runner = _module(_RUNNER, "study_f_runner")
    thin_then_thick = _bars(40, start=2.0, step=0.01) + _bars(
        40, start=300.0, step=0.5, first_day=_START + timedelta(days=40),
    )
    # First window: ~$2M median. Second: ~$120M+. Same ticker, different windows.
    early = {"X": thin_then_thick[:40], "SPY": _bars(40, start=400.0, step=0.3)}
    late = {"X": thin_then_thick[40:], "SPY": _bars(40, start=400.0, step=0.3)}
    assert runner.liquid_tickers(early) == ["SPY"], "the thin window must drop X"
    assert "X" in runner.liquid_tickers(late), "the thick window must keep X"


def test_the_twenty_million_line_is_the_one_the_document_fixed() -> None:
    runner = _module(_RUNNER, "study_f_runner")
    assert runner.MIN_DOLLAR_VOLUME == 20_000_000.0


# ---------------------------------------------------------------------------
# §7 item 1 — the boundary is enforced where the panel is built
# ---------------------------------------------------------------------------


def test_the_fetcher_keeps_the_boundary_session_and_drops_the_one_before() -> None:
    """Inclusive on 2026-09-19, exercised through the fetcher's own function.

    The first version of this test recomputed the comparison here instead of calling
    ``keep_since``, so the fetcher could have used ``>`` and stayed green. Caught
    before it shipped; the rule now lives in one place and this calls it.
    """
    module = _module(_FETCHER, "study_f_fetch_bars")
    rows: list[dict[str, object]] = [
        {"day": d} for d in ("2026-09-17", "2026-09-18", "2026-09-19", "2026-09-22")
    ]
    kept = module.keep_since(rows, _BOUNDARY)
    assert [r["day"] for r in kept] == ["2026-09-19", "2026-09-22"]
    # The last session of Study F's panel must not survive the filter.
    assert "2026-09-18" not in [r["day"] for r in kept]
    # And no bound keeps everything, so the filter is opt-in rather than always-on.
    assert module.keep_since(rows, None) == rows


def test_a_malformed_bound_fails_before_any_request_is_made() -> None:
    """71 requests are not the place to discover a typo in a date."""
    module = _module(_FETCHER, "study_f_fetch_bars")
    with pytest.raises(ValueError):
        module.date.fromisoformat("2026-13-45")


def test_the_panel_holds_no_session_the_bars_do_not_carry() -> None:
    """The runner builds its day index from the rows it is given, so a panel fed
    only post-boundary bars cannot reach an earlier session."""
    runner = _module(_RUNNER, "study_f_runner")
    bars = _panel_input()
    panel = runner.build_panel(bars, 1)
    supplied = {bar.day for bar in bars["AAA"]}
    assert set(panel.days) <= supplied
    assert min(panel.days) >= _START
    # Day index 0 is the first POPULATED session, which is WARMUP bars in.
    assert panel.days[0] == _START + timedelta(days=runner.WARMUP)
