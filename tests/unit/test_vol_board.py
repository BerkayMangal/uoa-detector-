"""Unit tests for the pure vol-premium board assembly (Phase 4.34)."""

from __future__ import annotations

import math
from datetime import date

from webapp.gamma import GammaContext
from webapp.vol_board import VolBoardRow, build_vol_board


def _ctx(ticker: str, iv_pct: float | None, *, net_gex: float = 1.0,
         atm_iv: float | None = 0.50) -> GammaContext:
    return GammaContext(
        ticker=ticker, as_of="2026-06-26", spot=100.0, net_gex=net_gex,
        flip=98.0, call_wall=110.0, put_wall=90.0, atm_iv=atm_iv, iv_pct=iv_pct,
    )


def test_sorted_by_iv_rank_desc() -> None:
    rows = build_vol_board(
        {"A": _ctx("A", 0.40), "B": _ctx("B", 0.90), "C": _ctx("C", 0.70)},
        earnings={}, now=date(2026, 6, 26),
    )
    assert [r.ticker for r in rows] == ["B", "C", "A"]


def test_earnings_in_window_demoted_and_flagged() -> None:
    rows = build_vol_board(
        {"HI": _ctx("HI", 0.95), "LO": _ctx("LO", 0.30)},
        earnings={"HI": date(2026, 7, 5)},  # 9 days out, inside 30d window
        now=date(2026, 6, 26),
    )
    # LO (clean) ranks ABOVE HI despite lower IV-rank, because HI has earnings.
    assert [r.ticker for r in rows] == ["LO", "HI"]
    hi = next(r for r in rows if r.ticker == "HI")
    assert hi.earnings_in_window is True
    assert next(r for r in rows if r.ticker == "LO").earnings_in_window is False


def test_earnings_outside_window_not_demoted() -> None:
    rows = build_vol_board(
        {"HI": _ctx("HI", 0.95)},
        earnings={"HI": date(2026, 9, 1)},  # >30d out
        now=date(2026, 6, 26),
    )
    assert rows[0].earnings_in_window is False


def test_below_threshold_flag() -> None:
    rows = build_vol_board(
        {"RICH": _ctx("RICH", 0.80), "THIN": _ctx("THIN", 0.50)},
        earnings={}, now=date(2026, 6, 26), rich_threshold=0.75,
    )
    by = {r.ticker: r for r in rows}
    assert by["RICH"].below_threshold is False
    assert by["THIN"].below_threshold is True


def test_expected_move_fraction() -> None:
    rows = build_vol_board(
        {"X": _ctx("X", 0.60, atm_iv=0.50)}, earnings={},
        now=date(2026, 6, 26), window_days=30,
    )
    assert rows[0].expected_move_pct == \
        round(0.50 * math.sqrt(30 / 365) * 100, 1)


def test_none_iv_pct_sorts_last_and_no_crash() -> None:
    rows = build_vol_board(
        {"N": _ctx("N", None), "G": _ctx("G", 0.50)},
        earnings={}, now=date(2026, 6, 26),
    )
    assert rows[-1].ticker == "N"
    assert rows[-1].iv_rank is None


def test_none_atm_iv_gives_none_expected_move() -> None:
    rows = build_vol_board(
        {"X": _ctx("X", 0.60, atm_iv=None)}, earnings={},
        now=date(2026, 6, 26),
    )
    assert rows[0].expected_move_pct is None


def test_row_carries_regime_and_walls() -> None:
    rows = build_vol_board({"X": _ctx("X", 0.60, net_gex=-2.0)}, earnings={},
                           now=date(2026, 6, 26))
    r = rows[0]
    assert r.regime == "short"
    assert r.call_wall == 110.0 and r.put_wall == 90.0
    assert isinstance(r, VolBoardRow)
