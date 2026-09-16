"""Phase 5.2.A1: per-(ticker, direction) aggregation (``webapp/board/aggregate.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A1; decisions P11, P12.

Pins:
  - exactly one row per (ticker, side-aware direction); a sold call joins the
    bought puts in ``aşağı``, a sold put joins the bought calls in ``yukarı``;
  - side-aware and option-type-fallback prints merge into one row but are
    counted separately;
  - total premium, print count, distinct contracts; the dominant contract is
    the largest SUMMED premium per (strike, expiry, type), not the largest
    single print, and carries the recorded UW chain;
  - concentration = top strike (summed across expiries) / row total;
    dominance = row / (row + opposite direction of the same ticker);
  - the position read follows ``BoardSettings.aggregation`` at its exact
    cutoffs, including the minimum strike count for ``dağınık envanter``;
  - rows sort by total premium; the combined score never orders them;
  - the detail lists every constituent print, newest first;
  - position labels are the contract strings and pass the forbidden-word guard.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from webapp.board.aggregate import POSITION_READ_LABELS, build_board_rows, classify_position
from webapp.board.honesty import ensure_clean
from webapp.board.settings import AggregationSettings, load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_BOARD = Path(__file__).resolve().parents[2] / "profiles" / "board_v1.yaml"
_SETTINGS = load_board_settings(_BOARD).aggregation
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_RUN = "live-2026-09-15"
_IDS = itertools.count()


def _signal(
    ticker: str,
    option_type: str,
    strike: str,
    premium: str,
    *,
    dte: int = 3,
    minute: int = 0,
    score: float = 0.3,
) -> StoredSignal:
    pr = build_print(
        event_id=f"e{next(_IDS)}", ts=_TS + timedelta(minutes=minute), ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike, dte=dte, premium=premium,
    )
    event = EnrichedEvent(print=pr, combined_score_post_penalty=score)
    return BacktestStore().add(
        event,
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _bp(sig: StoredSignal, fill_side: str | None, chain: str | None = None) -> BoardPrint:
    assert sig.event_id is not None
    meta = None if fill_side is None else PrintMetaView(fill_side=fill_side, option_chain=chain)
    return BoardPrint(run_id=_RUN, event_id=sig.event_id, signal=sig, meta=meta)


def _by_key(prints: list[BoardPrint], settings: AggregationSettings = _SETTINGS) -> dict[tuple[str, str], object]:
    return {(r.ticker, r.direction): r for r in build_board_rows(prints, settings)}


def test_one_row_per_ticker_and_side_aware_direction() -> None:
    prints = [
        _bp(_signal("SPY", "call", "760", "100000"), "at_ask"),  # bought call: up
        _bp(_signal("SPY", "put", "740", "50000"), "at_bid"),  # sold put: up
        _bp(_signal("SPY", "call", "770", "80000"), "at_bid"),  # sold call: down
        _bp(_signal("SPY", "put", "730", "20000"), "at_ask"),  # bought put: down
    ]
    rows = build_board_rows(prints, _SETTINGS)

    assert [(r.ticker, r.direction) for r in rows] == [("SPY", "up"), ("SPY", "down")]
    up, down = rows
    assert (up.total_premium, up.print_count) == (Decimal(150000), 2)
    assert (down.total_premium, down.print_count) == (Decimal(100000), 2)
    assert up.direction_label == "yukarı"
    assert down.direction_label == "aşağı"


def test_three_prints_of_one_name_and_direction_are_one_row() -> None:
    prints = [_bp(_signal("NVDA", "call", "180", "40000", minute=m), "at_ask") for m in range(3)]
    (row,) = build_board_rows(prints, _SETTINGS)
    assert row.print_count == 3
    assert row.distinct_contracts == 1


def test_fallback_prints_merge_but_are_counted_separately() -> None:
    prints = [
        _bp(_signal("AMD", "call", "150", "60000"), "at_ask"),
        _bp(_signal("AMD", "call", "150", "30000"), None),  # legacy row, no meta
        _bp(_signal("AMD", "call", "155", "30000"), "midpoint"),
    ]
    (row,) = build_board_rows(prints, _SETTINGS)
    assert row.direction == "up"
    assert (row.side_aware_prints, row.fallback_prints) == (1, 2)
    assert [p.side_aware for p in row.prints].count(False) == 2


def test_dominant_contract_is_the_largest_summed_premium() -> None:
    prints = [
        _bp(_signal("TSLA", "call", "300", "300000", dte=3), "at_ask"),
        _bp(_signal("TSLA", "call", "310", "200000", dte=10, minute=1), "at_ask", "TSLA260925C00310000"),
        _bp(_signal("TSLA", "call", "310", "150000", dte=10, minute=2), "at_ask"),
    ]
    (row,) = build_board_rows(prints, _SETTINGS)

    assert row.total_premium == Decimal(650000)
    assert row.distinct_contracts == 2
    dominant = row.dominant
    assert dominant.key.strike == Decimal(310)
    assert dominant.key.expiry == (_TS + timedelta(days=10)).date()
    assert dominant.key.option_type == "call"
    assert dominant.premium == Decimal(350000)
    assert dominant.print_count == 2
    assert dominant.option_chain == "TSLA260925C00310000"


def test_concentration_sums_the_top_strike_across_expiries() -> None:
    prints = [
        _bp(_signal("AAPL", "call", "100", "250000", dte=3), "at_ask"),
        _bp(_signal("AAPL", "call", "100", "250000", dte=10), "at_ask"),
        _bp(_signal("AAPL", "call", "105", "400000", dte=3), "at_ask"),
    ]
    (row,) = build_board_rows(prints, _SETTINGS)

    assert row.dominant.key.strike == Decimal(105)  # largest single contract
    assert row.top_strike == Decimal(100)  # largest strike across expiries
    assert row.distinct_strikes == 2
    assert row.concentration_pct == pytest.approx(500000 / 900000 * 100)


def test_dominance_is_measured_against_the_opposite_direction() -> None:
    prints = [
        _bp(_signal("META", "call", "700", "300000"), "at_ask"),
        _bp(_signal("META", "call", "700", "100000"), "at_bid"),
        _bp(_signal("MSFT", "put", "400", "50000"), "at_ask"),
    ]
    rows = _by_key(prints)
    assert rows[("META", "up")].dominance_pct == pytest.approx(75.0)  # type: ignore[attr-defined]
    assert rows[("META", "down")].dominance_pct == pytest.approx(25.0)  # type: ignore[attr-defined]
    assert rows[("MSFT", "down")].dominance_pct == pytest.approx(100.0)  # type: ignore[attr-defined]


def _strike_row(premiums: list[str], settings: AggregationSettings = _SETTINGS) -> object:
    prints = [
        _bp(_signal("QQQ", "call", str(500 + 5 * i), premium), "at_ask")
        for i, premium in enumerate(premiums)
    ]
    (row,) = build_board_rows(prints, settings)
    return row


def test_position_read_intentional_at_the_exact_cutoff() -> None:
    assert _SETTINGS.intentional_min_top_strike_share_pct == 60.0
    row = _strike_row(["600000", "400000"])
    assert row.concentration_pct == 60.0  # type: ignore[attr-defined]
    assert row.position_read == "intentional"  # type: ignore[attr-defined]
    assert row.position_label == "kasıtlı pozisyon"  # type: ignore[attr-defined]


def test_position_read_scattered_at_the_exact_cutoff_with_enough_strikes() -> None:
    assert _SETTINGS.scattered_max_top_strike_share_pct == 30.0
    assert _SETTINGS.min_strikes_for_scattered == 4
    row = _strike_row(["300000", "300000", "200000", "200000"])
    assert row.concentration_pct == 30.0  # type: ignore[attr-defined]
    assert row.distinct_strikes == 4  # type: ignore[attr-defined]
    assert row.position_read == "scattered"  # type: ignore[attr-defined]
    assert row.position_label == "dağınık envanter"  # type: ignore[attr-defined]


def test_position_read_scattered_needs_the_minimum_strike_count() -> None:
    strict = AggregationSettings(
        intentional_min_top_strike_share_pct=60.0,
        scattered_max_top_strike_share_pct=30.0,
        min_strikes_for_scattered=5,
    )
    row = _strike_row(["250000", "250000", "250000", "250000"], strict)
    assert row.concentration_pct == 25.0  # type: ignore[attr-defined]
    assert row.position_read == "mixed"  # type: ignore[attr-defined]


def test_position_read_mixed_between_the_cutoffs() -> None:
    row = _strike_row(["450000", "350000", "200000"])
    assert row.position_read == "mixed"  # type: ignore[attr-defined]
    assert row.position_label == "karışık"  # type: ignore[attr-defined]


def test_cutoffs_come_from_settings() -> None:
    loose = AggregationSettings(
        intentional_min_top_strike_share_pct=40.0,
        scattered_max_top_strike_share_pct=20.0,
        min_strikes_for_scattered=2,
    )
    assert classify_position(45.0, 3, loose) == "intentional"
    assert classify_position(45.0, 3, _SETTINGS) == "mixed"
    assert classify_position(None, 9, _SETTINGS) == "mixed"


def test_zero_premium_row_has_unknown_shares_and_reads_mixed() -> None:
    (row,) = build_board_rows([_bp(_signal("IWM", "put", "200", "0"), "at_ask")], _SETTINGS)
    assert row.concentration_pct is None
    assert row.dominance_pct is None
    assert row.position_read == "mixed"


def test_rows_sort_by_total_premium_never_by_score() -> None:
    prints = [
        _bp(_signal("NVDA", "call", "180", "100000", score=0.95), "at_ask"),
        _bp(_signal("AAPL", "call", "230", "200000", score=0.05), "at_ask"),
        _bp(_signal("AMZN", "put", "220", "150000", score=0.50), "at_ask"),
    ]
    assert [r.ticker for r in build_board_rows(prints, _SETTINGS)] == ["AAPL", "AMZN", "NVDA"]


def test_detail_lists_every_print_newest_first() -> None:
    prints = [
        _bp(_signal("SPY", "call", "760", "10000", minute=m), "at_ask") for m in (5, 1, 9, 3)
    ]
    (row,) = build_board_rows(prints, _SETTINGS)
    assert len(row.prints) == 4
    assert [p.timestamp.minute for p in row.prints] == [9, 5, 3, 1]
    assert row.latest_ts == row.prints[0].timestamp
    assert {p.event_id for p in row.prints} == {p.event_id for p in prints}


def test_no_prints_no_rows() -> None:
    assert build_board_rows([], _SETTINGS) == []


def test_position_labels_are_the_contract_strings_and_clean() -> None:
    assert dict(POSITION_READ_LABELS) == {
        "intentional": "kasıtlı pozisyon",
        "scattered": "dağınık envanter",
        "mixed": "karışık",
    }
    for text in POSITION_READ_LABELS.values():
        assert ensure_clean(text) == text
    with pytest.raises(TypeError):
        POSITION_READ_LABELS["mixed"] = "x"  # type: ignore[index]
