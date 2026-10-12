"""Phase 5.26: the basket-relative lag, worked by hand (display only)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from webapp.board.baskets import (
    Basket,
    BasketSettings,
    anchor_dates,
    basket_lags,
    lags_by_ticker,
    load_basket_settings,
)
from webapp.board.daily_close import ClosePoint

D0 = date(2026, 10, 1)


def _days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def _series(closes: list[float], days: list[date] | None = None) -> list[ClosePoint]:
    use = days if days is not None else _days(len(closes))
    return [ClosePoint(day=d, close=c) for d, c in zip(use, closes, strict=True)]


def _cfg(**over: object) -> BasketSettings:
    base: dict[str, object] = {
        "basket_version": "t",
        "window_sessions": 5,
        "min_members_with_data": 3,
        "lag_flag_pct": 3.0,
        "min_basket_move_pct": 2.0,
        "baskets": [{"id": "opt", "name": "Optik", "members": ["AAA", "BBB", "CCC", "DDD"]}],
    }
    base.update(over)
    return BasketSettings.model_validate(base)


def _flat_then(last: float) -> list[float]:
    """Six closes: 100 on the start anchor, `last` on the end anchor."""
    return [100.0, 100.0, 100.0, 100.0, 100.0, last]


# ---------------------------------------------------------------------------
# The residual
# ---------------------------------------------------------------------------


def test_residual_is_own_return_minus_the_mean_of_the_others() -> None:
    cfg = _cfg()
    closes = {
        "AAA": _series(_flat_then(110.0)),  # +10 %
        "BBB": _series(_flat_then(110.0)),  # +10 %
        "CCC": _series(_flat_then(110.0)),  # +10 %
        "DDD": _series(_flat_then(100.0)),  # 0 %
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    # DDD against three peers at +10: 0 - 10 = -10
    assert lags["DDD"].state == "measured"
    assert lags["DDD"].residual_pct == pytest.approx(-10.0)
    assert lags["DDD"].peers_pct == pytest.approx(10.0)
    assert lags["DDD"].own_pct == pytest.approx(0.0)
    # AAA against (10, 10, 0) -> mean 6.667; 10 - 6.667
    assert lags["AAA"].residual_pct == pytest.approx(10.0 - 20.0 / 3.0)


def test_self_is_excluded_from_the_peer_mean() -> None:
    """Including self would shrink the gap by 1/n; the contract (§3.2) excludes it."""
    cfg = _cfg()
    closes = {
        "AAA": _series(_flat_then(110.0)),
        "BBB": _series(_flat_then(110.0)),
        "CCC": _series(_flat_then(110.0)),
        "DDD": _series(_flat_then(100.0)),
    }
    lag = basket_lags(cfg.baskets[0], closes, cfg)["DDD"]
    with_self = 0.0 - (30.0 / 4.0)  # the diluted number: -7.5
    assert lag.residual_pct != pytest.approx(with_self)
    assert lag.residual_pct == pytest.approx(-10.0)


def test_residuals_sum_to_zero_across_a_basket_only_when_peers_exclude_self() -> None:
    """Sanity of the definition: residual_i = (n/(n-1)) * (r_i - mean), so they cancel."""
    cfg = _cfg()
    closes = {
        "AAA": _series(_flat_then(112.0)),
        "BBB": _series(_flat_then(106.0)),
        "CCC": _series(_flat_then(103.0)),
        "DDD": _series(_flat_then(99.0)),
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert sum(lag.residual_pct or 0.0 for lag in lags.values()) == pytest.approx(0.0)


def test_lagging_and_leading_flags_use_the_profile_cutoff() -> None:
    cfg = _cfg(lag_flag_pct=3.0)
    closes = {
        "AAA": _series(_flat_then(110.0)),
        "BBB": _series(_flat_then(110.0)),
        "CCC": _series(_flat_then(110.0)),
        "DDD": _series(_flat_then(100.0)),
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert lags["DDD"].lagging is True
    assert lags["DDD"].leading is False
    assert lags["AAA"].lagging is False
    # AAA's residual is +3.33, which clears 3.0 — it leads
    assert lags["AAA"].leading is True


def test_a_residual_inside_the_cutoff_is_measured_but_not_flagged() -> None:
    cfg = _cfg(lag_flag_pct=3.0)
    closes = {
        "AAA": _series(_flat_then(106.0)),
        "BBB": _series(_flat_then(105.0)),
        "CCC": _series(_flat_then(105.0)),
        "DDD": _series(_flat_then(104.0)),
    }
    lag = basket_lags(cfg.baskets[0], closes, cfg)["DDD"]
    assert lag.state == "measured"
    assert lag.lagging is False
    assert lag.leading is False


def test_rank_one_is_the_strongest_and_ties_break_by_ticker() -> None:
    cfg = _cfg()
    closes = {
        "AAA": _series(_flat_then(105.0)),
        "BBB": _series(_flat_then(105.0)),  # ties AAA
        "CCC": _series(_flat_then(110.0)),
        "DDD": _series(_flat_then(100.0)),
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert [lags[t].rank for t in ("CCC", "AAA", "BBB", "DDD")] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Shared anchors (§3.3)
# ---------------------------------------------------------------------------


def test_anchors_are_the_newest_session_and_the_one_window_before_it() -> None:
    closes = {"AAA": _series([1.0] * 8), "BBB": _series([1.0] * 8)}
    start, end = anchor_dates(closes, ["AAA", "BBB"], 5) or (None, None)
    assert end == D0 + timedelta(days=7)
    assert start == D0 + timedelta(days=2)


def test_anchors_count_sessions_in_the_union_not_one_members_series() -> None:
    """A name added last week must not shorten the window for the others."""
    closes = {
        "AAA": _series([1.0] * 8),
        "NEW": _series([1.0] * 3, _days(8)[-3:]),
    }
    anchors = anchor_dates(closes, ["AAA", "NEW"], 5)
    assert anchors == (D0 + timedelta(days=2), D0 + timedelta(days=7))


def test_history_shorter_than_the_window_has_no_anchors() -> None:
    closes = {"AAA": _series([1.0] * 5)}
    assert anchor_dates(closes, ["AAA"], 5) is None


def test_a_member_with_a_gap_on_an_anchor_is_dropped_not_guessed() -> None:
    """'On or before' would price DDD on a different day and call it the same window."""
    cfg = _cfg()
    days = _days(6)
    closes = {
        "AAA": _series(_flat_then(110.0)),
        "BBB": _series(_flat_then(110.0)),
        "CCC": _series(_flat_then(110.0)),
        # DDD has no close on the start anchor (days[0]); it starts a day later
        "DDD": _series([90.0, 90.0, 90.0, 90.0, 100.0], days[1:]),
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert lags["DDD"].state == "thin_data"
    assert lags["DDD"].residual_pct is None
    # the three that are priced still compare, and say one was dropped
    assert lags["AAA"].state == "measured"
    assert lags["AAA"].members_priced == 3
    assert lags["AAA"].members_total == 4
    # DDD's missing close did not leak into the mean: peers of AAA are BBB, CCC at +10
    assert lags["AAA"].peers_pct == pytest.approx(10.0)


def test_a_hole_on_the_start_anchor_is_not_papered_over_with_an_older_close() -> None:
    """The real failure: DDD has history, but not on the anchor day itself.

    "On or before" would silently price DDD from the session before the anchor and
    present a six-session move beside five-session moves as one comparison.
    """
    cfg = _cfg()
    days = _days(8)  # anchors: days[2] -> days[7]
    flat = [100.0] * 7 + [110.0]
    closes = {
        "AAA": _series(flat),
        "BBB": _series(flat),
        "CCC": _series(flat),
        # DDD: closes on every day except the start anchor, days[2]
        "DDD": _series([80.0, 80.0, 90.0, 90.0, 90.0, 90.0, 110.0], [d for d in days if d != days[2]]),
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert lags["DDD"].state == "thin_data"
    assert lags["DDD"].residual_pct is None
    assert lags["AAA"].members_priced == 3
    assert lags["AAA"].peers_pct == pytest.approx(10.0)  # DDD's older close did not leak in


def test_a_non_positive_close_is_not_priced() -> None:
    cfg = _cfg()
    closes = {
        "AAA": _series(_flat_then(110.0)),
        "BBB": _series(_flat_then(110.0)),
        "CCC": _series(_flat_then(110.0)),
        "DDD": _series([0.0, 100.0, 100.0, 100.0, 100.0, 100.0]),
    }
    assert basket_lags(cfg.baskets[0], closes, cfg)["DDD"].state == "thin_data"


# ---------------------------------------------------------------------------
# The three no-number branches (§3.4)
# ---------------------------------------------------------------------------


def test_too_few_priced_members_is_thin_data_for_everyone() -> None:
    cfg = _cfg(min_members_with_data=3)
    closes = {
        "AAA": _series(_flat_then(110.0)),
        "BBB": _series(_flat_then(100.0)),
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert {lag.state for lag in lags.values()} == {"thin_data"}
    assert all(lag.residual_pct is None for lag in lags.values())
    assert set(lags) == {"AAA", "BBB", "CCC", "DDD"}


def test_a_flat_basket_shows_no_residual() -> None:
    """A name cannot lag a group that did not move."""
    cfg = _cfg(min_basket_move_pct=2.0)
    closes = {
        "AAA": _series(_flat_then(101.0)),
        "BBB": _series(_flat_then(101.0)),
        "CCC": _series(_flat_then(101.0)),
        "DDD": _series(_flat_then(97.0)),  # mean +1.0 %, under the 2.0 gate
    }
    lags = basket_lags(cfg.baskets[0], closes, cfg)
    assert {lags[t].state for t in ("AAA", "BBB", "CCC", "DDD")} == {"flat_basket"}
    assert all(lag.residual_pct is None for lag in lags.values())


def test_a_basket_that_fell_is_not_flat() -> None:
    cfg = _cfg(min_basket_move_pct=2.0)
    closes = {
        "AAA": _series(_flat_then(90.0)),
        "BBB": _series(_flat_then(90.0)),
        "CCC": _series(_flat_then(90.0)),
        "DDD": _series(_flat_then(100.0)),
    }
    lag = basket_lags(cfg.baskets[0], closes, cfg)["DDD"]
    assert lag.state == "measured"
    assert lag.leading is True  # held up while the group fell


def test_no_history_at_all_is_an_empty_answer() -> None:
    cfg = _cfg()
    assert basket_lags(cfg.baskets[0], {}, cfg) == {}


# ---------------------------------------------------------------------------
# Several baskets
# ---------------------------------------------------------------------------


def test_a_name_in_two_baskets_takes_the_first_in_file_order() -> None:
    cfg = _cfg(baskets=[
        {"id": "one", "name": "Bir", "members": ["AAA", "BBB", "CCC"]},
        {"id": "two", "name": "Iki", "members": ["AAA", "XXX", "YYY"]},
    ])
    closes = {t: _series(_flat_then(110.0)) for t in ("AAA", "BBB", "CCC", "XXX", "YYY")}
    closes["BBB"] = _series(_flat_then(100.0))
    lags = lags_by_ticker(closes, cfg)
    assert lags["AAA"].basket_id == "one"
    assert cfg.basket_of("aaa") is not None
    assert cfg.basket_of("aaa").id == "one"  # type: ignore[union-attr]
    assert lags["XXX"].basket_id == "two"


def test_a_ticker_outside_every_basket_has_no_entry() -> None:
    cfg = _cfg()
    assert cfg.basket_of("ZZZ") is None
    assert "ZZZ" not in lags_by_ticker({"ZZZ": _series(_flat_then(120.0))}, cfg)


def test_members_lists_every_name_once_in_file_order() -> None:
    cfg = _cfg(baskets=[
        {"id": "one", "name": "Bir", "members": ["AAA", "BBB"]},
        {"id": "two", "name": "Iki", "members": ["BBB", "CCC"]},
    ])
    assert cfg.members() == ("AAA", "BBB", "CCC")


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def test_the_committed_profile_loads_and_holds_the_optical_basket() -> None:
    cfg = load_basket_settings()
    assert cfg.basket_version == "baskets_v1"
    optical = cfg.basket_of("COHR")
    assert optical is not None
    assert {"AAOI", "COHR", "LITE"} <= set(optical.members)


def test_members_are_upper_cased_on_load() -> None:
    basket = Basket.model_validate({"id": "x", "name": "X", "members": ["aaoi", " cohr ", "lite"]})
    assert basket.members == ("AAOI", "COHR", "LITE")


def test_a_repeated_member_is_rejected() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        Basket.model_validate({"id": "x", "name": "X", "members": ["AAOI", "aaoi"]})


def test_a_basket_needs_two_members() -> None:
    with pytest.raises(ValidationError):
        Basket.model_validate({"id": "x", "name": "X", "members": ["AAOI"]})


def test_a_repeated_basket_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="repeat an id"):
        _cfg(baskets=[
            {"id": "a", "name": "A", "members": ["AAA", "BBB"]},
            {"id": "a", "name": "B", "members": ["CCC", "DDD"]},
        ])


def test_a_peer_floor_below_three_is_rejected() -> None:
    """Two names is a pair spread, not a basket (§3.5)."""
    with pytest.raises(ValidationError):
        _cfg(min_members_with_data=2)


def test_an_unknown_key_in_the_profile_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "baskets.yaml"
    path.write_text(
        "basket_version: v\nwindow_sessions: 5\nmin_members_with_data: 3\n"
        "lag_flag_pct: 3.0\nmin_basket_move_pct: 2.0\nsurprise: 1\nbaskets: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_basket_settings(path)


def test_a_profile_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "baskets.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_basket_settings(path)
