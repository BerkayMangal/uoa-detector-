"""Phase 5.2.A2: tradability chip and cost gate (``webapp/board/tradability.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CO1/R-CO2, §4.3
("Spread cutoff"), §5 A2; decisions P15, P16.

Pins:
  - spread % of mid is the ``penalties.py`` formula; round trip is
    (ask − bid) × 100 + 2 × commission; 1 contract is ask × 100 and its share
    of capital;
  - the four states at their exact cutoffs:
    ``kotasyon yok`` (no row, not returned, null NBBO, stale, crossed) is
    never ``İŞLENMEZ``; ``İŞLENMEZ`` for bid 0 or spread above
    ``penalty_triggers.spread_pct_threshold``; ``DAR`` above
    ``max_tradable_spread_pct`` or for a thin known exit; unknown depth never
    demotes; a stale depth reading counts as unknown;
  - the İŞLENMEZ cutoff is read from ``profiles/v5_default.yaml`` (15.0) and
    only read;
  - reasons such as ``SMCI %17 makas``; quote age and last-trade minutes;
  - the ``values_confirmed`` flag follows ``sizing.values_confirmed_by_owner``;
  - every label, template and formatted reason passes ``ensure_clean``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webapp.board.honesty import ensure_clean
from webapp.board.settings import TradabilitySettings, load_board_settings
from webapp.board.tradability import (
    CHIP_COPY,
    REASON_TEMPLATES,
    STATE_LABELS,
    DepthView,
    QuoteView,
    assess_tradability,
    format_pct,
    one_lot_cost_usd,
    one_lot_pct_of_capital,
    round_trip_usd,
    spread_pct_of_mid,
)

from uoa_detector.calibration import load_profile

_REPO = Path(__file__).resolve().parents[2]
_BOARD = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_SPREAD_CUTOFF = load_profile(_REPO / "profiles" / "v5_default.yaml").penalty_triggers.spread_pct_threshold
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)


def _quote(
    bid: float | None,
    ask: float | None,
    *,
    age_s: int = 41,
    returned: bool = True,
    tape_minutes_ago: int | None = 4,
) -> QuoteView:
    return QuoteView(
        option_symbol="SMCI260918C00037000",
        nbbo_bid=bid,
        nbbo_ask=ask,
        volume=1876,
        last_tape_time=None if tape_minutes_ago is None else _NOW - timedelta(minutes=tape_minutes_ago),
        fetched_at=_NOW - timedelta(seconds=age_s),
        returned=returned,
    )


def _depth(bid_size: int | None, *, age_s: int = 60, quote_minutes_ago: int = 4) -> DepthView:
    return DepthView(
        option_symbol="SMCI260918C00037000",
        nbbo_bid_size=bid_size,
        nbbo_ask_size=108,
        quote_time=_NOW - timedelta(minutes=quote_minutes_ago),
        fetched_at=_NOW - timedelta(seconds=age_s),
    )


def _assess(
    quote: QuoteView | None,
    depth: DepthView | None = None,
    *,
    tradability: TradabilitySettings = _BOARD.tradability,
) -> object:
    return assess_tradability(
        "SMCI", quote, depth,
        tradability=tradability, spread_cutoff_pct=_SPREAD_CUTOFF,
        cost=_BOARD.cost, sizing=_BOARD.sizing, now=_NOW,
    )


def test_pinned_cutoffs_used_by_these_tests() -> None:
    assert _SPREAD_CUTOFF == 15.0
    assert _BOARD.tradability.max_tradable_spread_pct == 5.0
    assert _BOARD.tradability.min_exit_bid_size == 10
    assert _BOARD.tradability.max_quote_age_seconds == 900
    assert _BOARD.cost.commission_per_contract_usd == 0.65
    assert _BOARD.sizing.capital_usd == 10000


def test_cost_formulas() -> None:
    assert spread_pct_of_mid(1.23, 1.24) == pytest.approx(0.01 / 1.235 * 100)
    assert spread_pct_of_mid(0.0, 0.0) is None
    assert round_trip_usd(0.87, 0.91, 0.65) == pytest.approx(5.30)
    assert one_lot_cost_usd(0.91) == pytest.approx(91.0)
    assert one_lot_pct_of_capital(0.91, 10000) == pytest.approx(0.91)
    assert format_pct(17.0) == "17"
    assert format_pct(5.25) == "5.2"


def test_no_quote_row_is_unknown_not_untradable() -> None:
    read = _assess(None)
    assert read.state == "no_quote"  # type: ignore[attr-defined]
    assert read.label == "kotasyon yok"  # type: ignore[attr-defined]
    assert read.reason == "SMCI için kotasyon kaydı yok"  # type: ignore[attr-defined]
    assert read.bid is None and read.round_trip_usd is None  # type: ignore[attr-defined]


def test_symbol_not_returned_is_unknown() -> None:
    read = _assess(_quote(None, None, returned=False))
    assert read.state == "no_quote"  # type: ignore[attr-defined]
    assert read.reason == "SMCI sözleşmesi UW'de yok"  # type: ignore[attr-defined]


def test_null_nbbo_is_unknown_never_illiquid() -> None:
    read = _assess(_quote(None, None, tape_minutes_ago=None))
    assert read.state == "no_quote"  # type: ignore[attr-defined]
    assert read.reason == "SMCI bugün işlem görmedi, NBBO yok"  # type: ignore[attr-defined]


def test_quote_age_cutoff_is_inclusive() -> None:
    assert _assess(_quote(0.87, 0.91, age_s=900)).state == "tradable"  # type: ignore[attr-defined]
    stale = _assess(_quote(0.87, 0.91, age_s=901))
    assert stale.state == "no_quote"  # type: ignore[attr-defined]
    assert stale.reason == "SMCI kotasyonu 901 sn önce alındı, eski"  # type: ignore[attr-defined]
    assert stale.quote_age_seconds == 901  # type: ignore[attr-defined]


def test_crossed_quote_is_unknown() -> None:
    read = _assess(_quote(0.95, 0.90))
    assert read.state == "no_quote"  # type: ignore[attr-defined]
    assert read.reason == "SMCI kotasyonu tutarsız (ask bid'in altında)"  # type: ignore[attr-defined]


def test_zero_bid_is_untradable() -> None:
    read = _assess(_quote(0.0, 0.01))
    assert read.state == "untradable"  # type: ignore[attr-defined]
    assert read.label == "İŞLENMEZ"  # type: ignore[attr-defined]
    assert read.reason == "SMCI bid 0, çıkış fiyatı yok"  # type: ignore[attr-defined]


def test_spread_at_the_penalty_threshold_is_narrow_and_above_is_untradable() -> None:
    at_cutoff = _assess(_quote(0.925, 1.075))  # (0.15 / 1.0) * 100 = 15.0 exactly
    assert at_cutoff.spread_pct == pytest.approx(15.0)  # type: ignore[attr-defined]
    assert at_cutoff.state == "narrow"  # type: ignore[attr-defined]
    owner_example = _assess(_quote(0.915, 1.085))  # 17.0 exactly
    assert owner_example.state == "untradable"  # type: ignore[attr-defined]
    assert owner_example.reason == "SMCI %17 makas"  # type: ignore[attr-defined]


def test_spread_at_the_tradable_cutoff_is_tradable_and_above_is_narrow() -> None:
    at_cutoff = _assess(_quote(0.975, 1.025))  # 5.0 exactly
    assert at_cutoff.state == "tradable"  # type: ignore[attr-defined]
    assert at_cutoff.label == "İŞLENİR"  # type: ignore[attr-defined]
    assert at_cutoff.reason is None  # type: ignore[attr-defined]
    above = _assess(_quote(0.97, 1.03))  # 6.0
    assert above.state == "narrow"  # type: ignore[attr-defined]
    assert above.label == "DAR"  # type: ignore[attr-defined]
    assert above.reason == "SMCI %6 makas"  # type: ignore[attr-defined]


def test_exit_depth_rules() -> None:
    thin = _assess(_quote(0.87, 0.91), _depth(9))
    assert thin.state == "narrow"  # type: ignore[attr-defined]
    assert thin.reason == "SMCI çıkış derinliği 9 kontrat"  # type: ignore[attr-defined]
    assert thin.exit_depth == 9  # type: ignore[attr-defined]
    assert thin.exit_depth_at_last_print is True  # type: ignore[attr-defined]

    assert _assess(_quote(0.87, 0.91), _depth(10)).state == "tradable"  # type: ignore[attr-defined]
    unknown = _assess(_quote(0.87, 0.91), _depth(None))
    assert unknown.state == "tradable"  # type: ignore[attr-defined]
    assert unknown.exit_depth is None  # type: ignore[attr-defined]
    no_row = _assess(_quote(0.87, 0.91), None)
    assert no_row.state == "tradable"  # type: ignore[attr-defined]
    assert no_row.exit_depth_at_last_print is False  # type: ignore[attr-defined]
    stale_depth = _assess(_quote(0.87, 0.91), _depth(3, age_s=901))
    assert stale_depth.state == "tradable"  # type: ignore[attr-defined]
    assert stale_depth.exit_depth is None  # type: ignore[attr-defined]


def test_cost_cells_and_ages_on_a_tradable_quote() -> None:
    read = _assess(_quote(0.87, 0.91, age_s=41, tape_minutes_ago=4), _depth(137))
    assert read.bid == 0.87  # type: ignore[attr-defined]
    assert read.ask == 0.91  # type: ignore[attr-defined]
    assert read.round_trip_usd == pytest.approx(5.30)  # type: ignore[attr-defined]
    assert read.lot_cost_usd == pytest.approx(91.0)  # type: ignore[attr-defined]
    assert read.lot_pct_capital == pytest.approx(0.91)  # type: ignore[attr-defined]
    assert read.quote_age_seconds == 41  # type: ignore[attr-defined]
    assert read.last_trade_minutes == 4  # type: ignore[attr-defined]
    # 5.3.5: O3 is confirmed in the profile this chip reads.
    assert read.values_confirmed is True  # type: ignore[attr-defined]


def test_last_trade_falls_back_to_the_depth_quote_time() -> None:
    read = _assess(_quote(0.87, 0.91, tape_minutes_ago=None), _depth(137, quote_minutes_ago=7))
    assert read.last_trade_minutes == 7  # type: ignore[attr-defined]


def test_tradable_cutoff_comes_from_settings() -> None:
    strict = TradabilitySettings(max_tradable_spread_pct=1.0, min_exit_bid_size=10, max_quote_age_seconds=900)
    assert _assess(_quote(0.87, 0.91), tradability=strict).state == "narrow"  # type: ignore[attr-defined]


def test_labels_and_copy_are_the_contract_strings() -> None:
    assert dict(STATE_LABELS) == {
        "tradable": "İŞLENİR", "narrow": "DAR", "untradable": "İŞLENMEZ", "no_quote": "kotasyon yok",
    }
    assert CHIP_COPY["default_value"] == "(varsayılan değer)"
    assert CHIP_COPY["at_last_trade"] == "son işlem anında"
    assert CHIP_COPY["quote_age"].format(seconds=41) == "kotasyon 41 sn önce alındı"
    assert CHIP_COPY["last_trade"].format(minutes=4) == "son işlem 4 dk önce"
    with pytest.raises(TypeError):
        STATE_LABELS["narrow"] = "x"  # type: ignore[index]


def test_every_label_template_and_reason_is_clean() -> None:
    texts = [*STATE_LABELS.values(), *REASON_TEMPLATES.values(), *CHIP_COPY.values()]
    for quote, depth in [
        (None, None),
        (_quote(None, None, returned=False), None),
        (_quote(None, None), None),
        (_quote(0.87, 0.91, age_s=5000), None),
        (_quote(0.95, 0.90), None),
        (_quote(0.0, 0.01), None),
        (_quote(0.915, 1.085), None),
        (_quote(0.97, 1.03), None),
        (_quote(0.87, 0.91), _depth(3)),
    ]:
        reason = _assess(quote, depth).reason  # type: ignore[attr-defined]
        assert reason is not None
        texts.append(reason)
    texts.extend([
        CHIP_COPY["quote_age"].format(seconds=41),
        CHIP_COPY["last_trade"].format(minutes=4),
        CHIP_COPY["contracts"].format(size=137),
        CHIP_COPY["lot_pct"].format(pct="0.91"),
        CHIP_COPY["bid_ask"].format(bid="0.87", ask="0.91"),
    ])
    for text in texts:
        assert ensure_clean(text) == text
