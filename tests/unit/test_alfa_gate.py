"""Phase 5.2.A2: cost gate sections and chip text (``webapp/board/alfa_page.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CO1/R-CO2/R-WD1,
§5 A2 ("Row face", "Gate"); decisions P15 and P16.

Pins:
  - gate on: main section (İŞLENİR then DAR, in premium order), then
    ``kotasyon yok``, then ``İŞLENMEZ`` with its reason; gate off: one list;
  - every row appears in exactly one section in both modes (never hidden);
  - the main section is present even when empty;
  - a failed quote read marks the page and leaves every chip unknown;
  - without a quote source every chip is ``kotasyon yok`` and no cutoff is
    needed; a priced quote without the cutoff is refused;
  - chip strings: spread %, bid/ask, round trip, 1 contract and its share of
    capital, exit depth ``son işlem anında``, quote age, last trade, and
    ``(varsayılan değer)`` only while values are unconfirmed;
  - every section title and chip string passes ``ensure_clean``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.alfa_page import (
    ALFA_COPY,
    build_alfa_page,
    chip_text,
    db_quote_source,
    load_spread_cutoff_pct,
    section_heading,
    template_context,
)
from webapp.board.copy_tr import GATE_LABEL
from webapp.board.db import make_engine
from webapp.board.honesty import ensure_clean
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.tradability import DepthView, QuoteView

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)

# ticker, strike, premium, chain
_ROWS = [
    ("AAA", "100", "300000", "AAA260918C00100000"),  # İŞLENİR: exactly 5% spread, deep exit
    ("BBB", "50", "200000", "BBB260918C00050000"),  # İŞLENMEZ: 17% spread
    ("CCC", "20", "100000", "CCC260918C00020000"),  # kotasyon yok: no quote row
    ("DDD", "10", "50000", "DDD260918C00010000"),  # DAR: 6% spread
]


def _prints() -> list[BoardPrint]:
    out: list[BoardPrint] = []
    for i, (ticker, strike, premium, chain) in enumerate(_ROWS):
        pr = build_print(
            event_id=f"g{i}", ts=_TS, ticker=ticker, option_type="call",
            strike=strike, dte=3, premium=premium,
        )
        sig = BacktestStore().add(
            EnrichedEvent(print=pr),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
        out.append(
            BoardPrint(
                run_id="live-2026-09-15", event_id=f"g{i}", signal=sig,
                meta=PrintMetaView(fill_side="at_ask", option_chain=chain),
            ),
        )
    return out


def _quote(symbol: str, bid: float, ask: float) -> QuoteView:
    return QuoteView(
        option_symbol=symbol, nbbo_bid=bid, nbbo_ask=ask, volume=500,
        last_tape_time=_NOW - timedelta(minutes=4), fetched_at=_NOW - timedelta(seconds=41),
        returned=True,
    )


def _source(symbols: Sequence[str]) -> tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]:
    quotes = {
        "AAA260918C00100000": _quote("AAA260918C00100000", 0.39, 0.41),
        "BBB260918C00050000": _quote("BBB260918C00050000", 1.83, 2.17),
        "DDD260918C00010000": _quote("DDD260918C00010000", 0.97, 1.03),
    }
    depths = {
        "AAA260918C00100000": DepthView(
            option_symbol="AAA260918C00100000", nbbo_bid_size=137, nbbo_ask_size=108,
            quote_time=_NOW - timedelta(minutes=4), fetched_at=_NOW - timedelta(seconds=60),
        ),
    }
    return (
        {s: q for s, q in quotes.items() if s in symbols},
        {s: d for s, d in depths.items() if s in symbols},
    )


def _page(**kwargs: Any) -> Any:
    options: dict[str, Any] = {"spread_cutoff_pct": _CUTOFF, "now": _NOW, "quote_source": _source}
    options.update(kwargs)
    return build_alfa_page(_prints(), _SETTINGS, **options)


def _tickers(section: Any) -> list[str]:
    return [v.row.ticker for v in section.views]


def test_gate_on_orders_main_then_no_quote_then_untradable() -> None:
    page = _page()
    assert page.gate_on is True
    assert [s.key for s in page.sections] == ["main", "no_quote", "untradable"]
    main, no_quote, untradable = page.sections
    assert _tickers(main) == ["AAA", "DDD"]
    assert [v.chip.label for v in main.views] == ["İŞLENİR", "DAR"]
    assert _tickers(no_quote) == ["CCC"]
    assert no_quote.views[0].chip.reason == "CCC için kotasyon kaydı yok"
    assert _tickers(untradable) == ["BBB"]
    assert untradable.views[0].chip.reason == "BBB %17 makas"
    assert section_heading(main) == "İŞLENİR ve DAR (2)"


def test_gate_off_lists_every_row_once_with_its_chip() -> None:
    page = _page(gate_on=False)
    assert [s.key for s in page.sections] == ["all"]
    (section,) = page.sections
    assert section.title == ALFA_COPY["section_all"]
    assert _tickers(section) == ["AAA", "BBB", "CCC", "DDD"]
    assert [v.chip.state for v in section.views] == ["tradable", "untradable", "no_quote", "narrow"]


def test_rows_are_never_hidden() -> None:
    for gate_on in (True, False):
        page = _page(gate_on=gate_on)
        shown = [v.row for s in page.sections for v in s.views]
        assert len(shown) == len(page.rows) == 4
        assert {r.ticker for r in shown} == {"AAA", "BBB", "CCC", "DDD"}


def test_main_section_is_present_even_when_empty() -> None:
    page = _page(quote_source=lambda symbols: ({}, {}))
    assert [s.key for s in page.sections] == ["main", "no_quote"]
    assert page.sections[0].views == ()
    assert len(page.sections[1].views) == 4


def test_failed_quote_read_is_flagged_and_never_clean() -> None:
    def _broken(symbols: Sequence[str]) -> Any:
        msg = "database unavailable"
        raise RuntimeError(msg)

    page = _page(quote_source=_broken)
    assert page.quotes_failed is True
    assert {v.chip.state for v in page.views} == {"no_quote"}


def test_without_a_quote_source_every_chip_is_unknown_and_no_cutoff_is_needed() -> None:
    page = build_alfa_page(_prints(), _SETTINGS)
    assert page.quotes_failed is False
    assert {v.chip.state for v in page.views} == {"no_quote"}


def test_priced_quote_without_the_cutoff_is_refused() -> None:
    with pytest.raises(ValueError, match="spread_cutoff_pct"):
        build_alfa_page(_prints(), _SETTINGS, now=_NOW, quote_source=_source)


def test_chip_text_on_a_tradable_row() -> None:
    aaa = _page().sections[0].views[0]
    text = aaa.chip_text
    assert aaa.symbol == "AAA260918C00100000"
    assert text.label == "İŞLENİR"
    assert text.reason is None
    assert text.spread == "%5"
    assert text.bid_ask == "bid $0.39 / ask $0.41"
    assert text.round_trip == "$3.30"
    assert text.lot == "$41 · sermayenin %0.41 kadarı"
    assert text.exit_depth == "137 kontrat (son işlem anında)"
    assert text.quote_age == "kotasyon 41 sn önce alındı"
    assert text.last_trade == "son işlem 4 dk önce"
    assert text.default_marker == "(varsayılan değer)"


def test_chip_text_on_an_unknown_row_says_unknown() -> None:
    ccc = _page().sections[1].views[0]
    text = ccc.chip_text
    assert text.label == "kotasyon yok"
    assert (text.spread, text.round_trip, text.lot, text.exit_depth) == ("bilinmiyor",) * 4
    assert text.bid_ask is None
    assert text.quote_age is None


def test_confirmed_owner_values_drop_the_default_marker() -> None:
    confirmed = _SETTINGS.model_copy(
        update={"sizing": _SETTINGS.sizing.model_copy(update={"values_confirmed_by_owner": True})},
    )
    page = build_alfa_page(_prints(), confirmed, spread_cutoff_pct=_CUTOFF, now=_NOW, quote_source=_source)
    assert {v.chip_text.default_marker for v in page.views} == {None}


def test_spread_cutoff_is_read_from_the_live_calibration_profile() -> None:
    assert _CUTOFF == 15.0


def test_template_context_carries_the_gate_copy() -> None:
    ctx = template_context()
    assert ctx["gate_label"] == GATE_LABEL == "Alabileceklerimi göster"
    assert ctx["gate_off_param"] == "off"


def test_every_section_title_and_chip_string_is_clean() -> None:
    texts: list[str] = [
        ALFA_COPY[k]
        for k in (
            "gate_on", "gate_off", "section_main", "section_no_quote", "section_untradable",
            "section_all", "main_empty", "quotes_failed",
        )
    ]
    for gate_on in (True, False):
        page = _page(gate_on=gate_on)
        texts.extend(section_heading(s) for s in page.sections)
        for view in page.views:
            t = chip_text(view.chip)
            texts.extend(
                x for x in (
                    t.label, t.reason, t.spread, t.bid_ask, t.round_trip, t.lot, t.exit_depth,
                    t.quote_age, t.last_trade, t.default_marker,
                ) if x is not None
            )
    for text in texts:
        assert ensure_clean(text) == text


def test_db_quote_source_reads_an_empty_fresh_database(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        page = build_alfa_page(
            _prints(), _SETTINGS, spread_cutoff_pct=_CUTOFF, now=_NOW,
            quote_source=db_quote_source(engine),
        )
    finally:
        engine.dispose()
    assert page.quotes_failed is False
    assert {v.chip.state for v in page.views} == {"no_quote"}
