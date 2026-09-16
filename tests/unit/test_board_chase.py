"""Phase 5.2.B3: the chase verdict (``webapp/board/chase.py``). Pure unit tests.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B3 (the line, the
verdict bands, the since-print flow context) and §2 R-CO1, R-EV1, R-WD1.

Pins:
  - the option change is ``ask / print price − 1``, priced at the executable ask;
  - both band cutoffs classify exactly, and no usable quote is its own verdict;
  - spot at print is ``moneyness × strike``; a missing or stale ATM row reads
    ``bilinmiyor`` without taking the option half of the line down with it;
  - the flow context is direction-aware, and it never moves the verdict;
  - a sold-option row keeps the ask-based verdict and says so;
  - the line matches the contract's wording;
  - every frozen and generated string passes ``ensure_clean`` and states no
    probability.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from webapp.board.alfa_page import load_spread_cutoff_pct
from webapp.board.atm import AtmView
from webapp.board.chase import (
    CHASE_COPY,
    FLOW_LABELS,
    SIDE_LABELS,
    VERDICT_LABELS,
    ChaseRead,
    build_chase,
    chase_text,
    classify_chase,
    current_spot,
    flow_context,
    option_change_pct,
    spot_at_print,
    underlying_move_pct,
)
from webapp.board.honesty import ensure_clean
from webapp.board.netprem import TapeSummary
from webapp.board.settings import load_board_settings
from webapp.board.tradability import QuoteView, TradabilityRead, assess_tradability

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CHASE = _SETTINGS.chase
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_MAX_AGE = _SETTINGS.tradability.max_quote_age_seconds
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_EXPIRY = date(2026, 9, 18)


def _signal(*, option_price: str = "1.50", spot: str = "198", strike: str = "100") -> StoredSignal:
    return BacktestStore().add(
        EnrichedEvent(
            print=build_print(
                event_id="c1", ts=_TS, ticker="AAA", option_type="call",
                strike=strike, spot=spot, dte=3, option_price=option_price,
            ),
        ),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _chip(bid: float | None, ask: float | None, *, age_seconds: int = 41) -> TradabilityRead:
    quote = (
        None
        if bid is None or ask is None
        else QuoteView(
            option_symbol="AAA260918C00100000", nbbo_bid=bid, nbbo_ask=ask, volume=500,
            last_tape_time=None, fetched_at=_NOW - timedelta(seconds=age_seconds), returned=True,
        )
    )
    return assess_tradability(
        "AAA", quote, None, tradability=_SETTINGS.tradability, spread_cutoff_pct=_CUTOFF,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=_NOW,
    )


def _atm(stock_price: float | None = 199.19, *, age_seconds: int = 41) -> AtmView:
    return AtmView(
        ticker="AAA", expiry=_EXPIRY, strike=100.0, stock_price=stock_price,
        call_bid=1.0, call_ask=1.1, call_iv=0.4, put_bid=0.9, put_ask=1.0, put_iv=0.4,
        trade_date=_TS.date(), fetched_at=_NOW - timedelta(seconds=age_seconds),
    )


def _tape(bullish_net: float) -> TapeSummary:
    return TapeSummary(
        ticker="AAA", trade_date=_TS.date(), net_call_premium=bullish_net, net_put_premium=0.0,
        minutes=30, first_tape_time=_TS, last_tape_time=_NOW, fetched_at=_NOW,
    )


def _read(**over: object) -> ChaseRead:
    options: dict[str, object] = {
        "signal": _signal(),
        "chip": _chip(1.58, 1.60),
        "direction": "up",
        "sold": False,
        "atm_rows": (_atm(),),
        "tape_since": _tape(400_000.0),
        "settings": _CHASE,
        "max_spot_age_seconds": _MAX_AGE,
        "now": _NOW,
    }
    options.update(over)
    return build_chase(**options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Option change and the verdict bands
# ---------------------------------------------------------------------------


def test_option_change_is_the_ask_over_the_print_price() -> None:
    assert option_change_pct(1.50, 1.80) == pytest.approx(20.0)
    assert option_change_pct(1.50, 1.20) == pytest.approx(-20.0)
    assert option_change_pct(4.05, 4.20) == pytest.approx((4.20 / 4.05 - 1) * 100)


@pytest.mark.parametrize(
    ("print_price", "ask"),
    [(None, 1.60), (1.50, None), (0.0, 1.60), (1.50, 0.0), (-1.0, 1.6), (float("nan"), 1.6)],
)
def test_option_change_is_unknown_without_two_usable_prices(
    print_price: float | None, ask: float | None,
) -> None:
    assert option_change_pct(print_price, ask) is None


def test_the_verdict_bands_classify_both_cutoffs_exactly() -> None:
    assert (_CHASE.reasonable_max_pct, _CHASE.late_min_pct) == (10.0, 15.0)
    assert classify_chase(_CHASE.reasonable_max_pct, _CHASE) == "reasonable"  # at the cutoff
    assert classify_chase(_CHASE.reasonable_max_pct + 0.01, _CHASE) == "caution"
    assert classify_chase(_CHASE.late_min_pct - 0.01, _CHASE) == "caution"
    assert classify_chase(_CHASE.late_min_pct, _CHASE) == "late"  # at the cutoff
    assert classify_chase(-30.0, _CHASE) == "reasonable"
    assert classify_chase(None, _CHASE) == "no_quote"


def test_the_verdict_labels_are_the_contract_wording() -> None:
    assert dict(VERDICT_LABELS) == {
        "reasonable": "hâlâ makul",
        "caution": "dikkat",
        "late": "geç kaldın",
        "no_quote": "kotasyon yok",
    }


@pytest.mark.parametrize(
    ("bid", "ask", "age_seconds"),
    [(None, None, 41), (1.70, 1.60, 41), (0.0, 0.0, 41), (1.58, 1.60, _MAX_AGE + 1)],
)
def test_an_unexecutable_or_stale_quote_has_no_verdict(bid: float | None, ask: float | None, age_seconds: int) -> None:
    read = _read(chip=_chip(bid, ask, age_seconds=age_seconds))
    assert (read.verdict, read.ask, read.change_pct) == ("no_quote", None, None)
    assert chase_text(read).line.startswith("baskı $1.50 → işlem yapılabilir kotasyon yok")


# ---------------------------------------------------------------------------
# The underlying half
# ---------------------------------------------------------------------------


def test_spot_at_print_is_moneyness_times_strike() -> None:
    assert spot_at_print(Decimal("1.98"), Decimal("100")) == pytest.approx(198.0)
    assert spot_at_print(None, Decimal("100")) is None
    assert spot_at_print(Decimal("0"), Decimal("100")) is None


def test_underlying_move_is_spot_now_over_spot_at_print() -> None:
    assert underlying_move_pct(198.0, 199.19) == pytest.approx((199.19 / 198.0 - 1) * 100)
    assert underlying_move_pct(198.0, None) is None
    assert underlying_move_pct(None, 199.19) is None
    assert underlying_move_pct(0.0, 199.19) is None


def test_current_spot_takes_the_newest_fresh_atm_row() -> None:
    older = _atm(198.5, age_seconds=300)
    newer = _atm(199.19, age_seconds=41)
    assert current_spot((older, newer), max_age_seconds=_MAX_AGE, now=_NOW) == 199.19
    assert current_spot((), max_age_seconds=_MAX_AGE, now=_NOW) is None
    assert current_spot((_atm(None),), max_age_seconds=_MAX_AGE, now=_NOW) is None
    stale = _atm(199.19, age_seconds=_MAX_AGE + 1)
    assert current_spot((stale,), max_age_seconds=_MAX_AGE, now=_NOW) is None


def test_a_missing_underlying_does_not_take_the_option_half_down() -> None:
    read = _read(atm_rows=())
    assert read.verdict == "reasonable"
    assert read.underlying_move_pct is None
    assert chase_text(read).line == (
        "baskı $1.50 → şimdi $1.60 (ask), %6.7 yukarıda; hisse hareketi bilinmiyor"
    )


# ---------------------------------------------------------------------------
# Flow context (never a verdict input)
# ---------------------------------------------------------------------------


def test_flow_context_is_direction_aware_and_labelled() -> None:
    assert flow_context(_tape(400_000.0), "up") == "continuing"
    assert flow_context(_tape(400_000.0), "down") == "reversed"
    assert flow_context(_tape(-400_000.0), "up") == "reversed"
    assert flow_context(_tape(0.0), "up") == "unknown"
    assert flow_context(None, "up") == "unknown"
    assert FLOW_LABELS["continuing"] == "akış baskıdan beri sürüyor"
    assert FLOW_LABELS["reversed"] == "akış baskıdan beri döndü"


def test_the_flow_context_never_changes_the_verdict() -> None:
    verdicts = {
        _read(tape_since=tape).verdict
        for tape in (_tape(400_000.0), _tape(-400_000.0), None)
    }
    assert verdicts == {"reasonable"}


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------


def test_the_line_matches_the_contract_wording() -> None:
    read = _read(signal=_signal(option_price="4.05"), chip=_chip(4.17, 4.20))
    text = chase_text(read)
    assert text.verdict == "hâlâ makul"
    assert text.line == "baskı $4.05 → şimdi $4.20 (ask), %3.7 yukarıda; hisse baskıdan beri %+0.6"
    assert text.flow == "akış baskıdan beri sürüyor"
    assert text.sold_note is None


def test_a_cheaper_quote_reads_below_and_an_unchanged_one_reads_flat() -> None:
    below = chase_text(_read(chip=_chip(1.18, 1.20))).line
    assert below.startswith("baskı $1.50 → şimdi $1.20 (ask), %20 aşağıda")
    flat = chase_text(_read(chip=_chip(1.48, 1.50))).line
    assert flat.startswith("baskı $1.50 → şimdi $1.50 (ask), değişmedi")
    assert SIDE_LABELS == {"up": "yukarıda", "down": "aşağıda"}


def test_a_late_row_feeds_the_counter_argument() -> None:
    read = _read(chip=_chip(1.78, 1.80))
    assert (read.verdict, read.late, read.label) == ("late", True, "geç kaldın")
    assert _read().late is False


def test_an_unreadable_source_print_has_no_price_half() -> None:
    read = _read(signal=None)
    assert (read.verdict, read.print_price, read.spot_at_print) == ("no_quote", None, None)
    assert chase_text(read).line == "baskı fiyatı bilinmiyor; hisse hareketi bilinmiyor"


def test_a_sold_option_row_keeps_the_ask_based_verdict_and_says_so() -> None:
    read = _read(sold=True, direction="down")
    assert read.sold is True
    assert read.verdict == "reasonable"  # unchanged: the board sizes a buyer entering at ask
    text = chase_text(read)
    assert text.sold_note == "bu satır satılan opsiyondan geliyor; hüküm ask'ten giren alıcıya göre"
    assert text.flow == "akış baskıdan beri döndü"  # the row's own direction


# ---------------------------------------------------------------------------
# Copy (R-EV1, R-WD1)
# ---------------------------------------------------------------------------


def test_every_frozen_and_generated_string_is_clean_and_states_no_probability() -> None:
    texts: list[str] = [*CHASE_COPY.values(), *VERDICT_LABELS.values(), *FLOW_LABELS.values(),
                        *SIDE_LABELS.values()]
    for chip in (_chip(1.58, 1.60), _chip(1.78, 1.80), _chip(1.18, 1.20), _chip(None, None)):
        for sold in (False, True):
            for signal in (_signal(), None):
                for atm_rows in ((_atm(),), ()):
                    text = chase_text(
                        _read(chip=chip, sold=sold, signal=signal, atm_rows=atm_rows),
                    )
                    texts.extend(
                        t for t in (text.title, text.verdict, text.line, text.flow, text.sold_note)
                        if t is not None
                    )
    for text in texts:
        assert ensure_clean(text) == text
    joined = " ".join(texts).lower()
    for banned in ("olasılık", "ihtimal", "beklenen", "kâr"):
        assert banned not in joined, banned


def test_the_dictionaries_are_read_only() -> None:
    for mapping in (CHASE_COPY, VERDICT_LABELS, FLOW_LABELS, SIDE_LABELS):
        with pytest.raises(TypeError):
            mapping["x"] = "y"  # type: ignore[index]
