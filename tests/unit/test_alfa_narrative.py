"""Phase 5.2.A5: reason sentence and mandatory counter-argument (``webapp/board/narrative.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CA1, R-CA2 and
R-WD1; §5 A5.

Pins:
  - the reason sentence names the lehte families (profile order), the
    direction and the position read; without lehte families it says so;
  - the counter-argument is the first applicable item in the contract's
    priority order: cost (İŞLENMEZ or DAR), aleyhte families, chase ``geç
    kaldın``, catalyst in window, unknown families at or above
    ``narrative.counter_min_unknown_families``, penalty ledger items;
  - ``kotasyon yok`` is not a cost counter-argument;
  - without any: exactly ``Bariz bir karşı argüman bulunamadı — bu bir onay
    değildir`` followed by ``Bakılanlar: ...`` listing only the checks that ran;
  - every counter template starts with ``AMA``; every template and generated
    string passes ``ensure_clean``; the dictionaries are read-only;
  - the rendered row: the bull and bear columns carry identical classes in a
    two-column grid, and the bear column starts with ``AMA`` or is the fallback
    followed by the checked list.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from webapp.board.aggregate import build_board_rows
from webapp.board.alfa_page import (
    CASE_CLASS,
    build_alfa_page,
    load_spread_cutoff_pct,
    template_context,
)
from webapp.board.copy_tr import COUNTER_LEAD, NO_COUNTER_FOUND
from webapp.board.evidence import (
    STAGE_BY_FAMILY,
    EvidenceInputs,
    StageTelemetryView,
    build_row_evidence,
)
from webapp.board.honesty import ensure_clean
from webapp.board.narrative import (
    CHECK_TEMPLATES,
    COUNTER_PRIORITY,
    COUNTER_TEMPLATES,
    DIRECTION_OBJECTS,
    NARRATIVE_COPY,
    POSITION_CLAUSES,
    REASON_TEMPLATES,
    CatalystCheck,
    ChaseCheck,
    PenaltyCheck,
    build_narrative,
)
from webapp.board.netprem import TapeSummary
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.tradability import QuoteView, assess_tradability

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_NARRATIVE = _SETTINGS.narrative
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_DEADBAND = _SETTINGS.evidence.flow_net_premium_deadband_usd
_MEASURED = {
    "dealer_gamma": "full_short_and_proximate",
    "dark_pool": "confirmed_match",
    "sector": "strong",
    "price_confirmation": "neutral",
}
_TRADABLE = (0.39, 0.41)
_NARROW = (0.97, 1.03)
_UNTRADABLE = (1.83, 2.17)


def _prints(
    *, ticker: str = "AAA", fill_side: str = "at_ask", strikes: tuple[str, ...] = ("100",),
) -> list[BoardPrint]:
    out: list[BoardPrint] = []
    for i, strike in enumerate(strikes):
        pr = build_print(
            event_id=f"{ticker}{i}", ts=_TS + timedelta(seconds=i), ticker=ticker, option_type="call",
            strike=strike, dte=3, premium="100000",
        )
        sig = BacktestStore().add(
            EnrichedEvent(print=pr),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
        out.append(
            BoardPrint(
                run_id=_RUN, event_id=f"{ticker}{i}", signal=sig,
                meta=PrintMetaView(fill_side=fill_side, option_chain=None),
            ),
        )
    return out


def _tape(bullish_net: float) -> TapeSummary:
    return TapeSummary(
        ticker="AAA", trade_date=_TS.date(), net_call_premium=bullish_net, net_put_premium=0.0,
        minutes=90, first_tape_time=_TS, last_tape_time=_NOW, fetched_at=_NOW,
    )


def _telemetry(branches: dict[str, str]) -> dict[str, StageTelemetryView]:
    return {
        STAGE_BY_FAMILY[family]: StageTelemetryView(stage_name=STAGE_BY_FAMILY[family], branch=branch, degraded=False)
        for family, branch in branches.items()
    }


def _quote(symbol: str, bid: float, ask: float) -> QuoteView:
    return QuoteView(
        option_symbol=symbol, nbbo_bid=bid, nbbo_ask=ask, volume=500,
        last_tape_time=_NOW - timedelta(minutes=4), fetched_at=_NOW - timedelta(seconds=41), returned=True,
    )


def _chip(bid: float | None, ask: float | None) -> Any:
    quote = None if bid is None or ask is None else _quote("AAA260918C00100000", bid, ask)
    return assess_tradability(
        "AAA", quote, None, tradability=_SETTINGS.tradability, spread_cutoff_pct=_CUTOFF,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=_NOW,
    )


def _measured_row_evidence(*, fill_side: str = "at_ask") -> tuple[Any, Any]:
    (row,) = build_board_rows(_prints(fill_side=fill_side), _SETTINGS.aggregation)
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=None, telemetry=_telemetry(_MEASURED),
        tape=_tape(_DEADBAND * 2), ticker_info=None,
    )
    return row, evidence


def _narrative(
    *,
    cost: bool,
    against: bool,
    chase_late: bool,
    catalyst: bool,
    unknown: bool,
    penalties: bool,
) -> Any:
    (row,) = build_board_rows(_prints(), _SETTINGS.aggregation)
    branches = {**_MEASURED, "price_confirmation": "call_contrarian" if against else "neutral"}
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=None, telemetry=_telemetry(branches),
        tape=None if unknown else _tape(_DEADBAND * 2), ticker_info=None,
    )
    return build_narrative(
        row, evidence, _chip(*(_UNTRADABLE if cost else _TRADABLE)), settings=_NARRATIVE,
        chase=ChaseCheck(verdict="geç kaldın", late=True) if chase_late else ChaseCheck(verdict="hâlâ makul", late=False),
        catalyst=CatalystCheck(in_window=("FOMC faiz kararı",) if catalyst else ()),
        penalties=PenaltyCheck(applied=("Düşük açık pozisyon",) if penalties else ()),
    )


# ---------------------------------------------------------------------------
# Counter-argument priority (R-CA1)
# ---------------------------------------------------------------------------


def test_counter_argument_follows_the_contract_priority() -> None:
    assert COUNTER_PRIORITY == ("cost", "against", "chase_late", "catalyst", "unknown", "penalties")
    flags = dict.fromkeys(COUNTER_PRIORITY, True)
    for key in COUNTER_PRIORITY:
        assert _narrative(**flags).counter_key == key
        flags[key] = False
    assert _narrative(**flags).counter_key is None


@pytest.mark.parametrize(
    ("flag", "counter"),
    [
        ("cost", "AMA maliyet: AAA %17 makas (İŞLENMEZ)."),
        ("against", "AMA 1 aile aleyhte: Fiyat teyidi."),
        ("chase_late", "AMA kovalama hükmü: geç kaldın."),
        ("catalyst", "AMA vade içinde katalizör var: FOMC faiz kararı."),
        ("unknown", "AMA 2 aile bilinmiyor (Akış, Açık pozisyon); bilgi yok, temiz demek değil."),
        ("penalties", "AMA ceza defterinde uygulanan: Düşük açık pozisyon."),
    ],
)
def test_each_counter_argument_text(flag: str, counter: str) -> None:
    narrative = _narrative(**{**dict.fromkeys(COUNTER_PRIORITY, False), flag: True})
    assert narrative.counter == counter
    assert narrative.counter.startswith(f"{COUNTER_LEAD} ")
    assert narrative.checked is None


def test_narrow_cost_is_a_counter_argument_and_no_quote_is_not() -> None:
    row, evidence = _measured_row_evidence()
    narrow = build_narrative(row, evidence, _chip(*_NARROW), settings=_NARRATIVE)
    assert narrow.counter == "AMA maliyet: AAA %6 makas (DAR)."
    no_quote = build_narrative(row, evidence, _chip(None, None), settings=_NARRATIVE)
    assert no_quote.counter == NO_COUNTER_FOUND
    assert no_quote.checked == "Bakılanlar: maliyet (kotasyon yok), aleyhte aile (0), bilinmeyen aile (1)."


def test_unknown_families_below_the_profile_count_are_no_counter_argument() -> None:
    assert _NARRATIVE.counter_min_unknown_families == 2
    options = dict.fromkeys(COUNTER_PRIORITY, False)
    assert _narrative(**options).counter_key is None  # one unknown family (Açık pozisyon)
    assert _narrative(**{**options, "unknown": True}).counter_key == "unknown"  # exactly two


def test_fallback_lists_only_the_checks_that_ran() -> None:
    with_everything = _narrative(**dict.fromkeys(COUNTER_PRIORITY, False))
    assert with_everything.counter == NO_COUNTER_FOUND == "Bariz bir karşı argüman bulunamadı — bu bir onay değildir"
    assert with_everything.counter_key is None
    assert with_everything.checked == (
        "Bakılanlar: maliyet (İŞLENİR), aleyhte aile (0), kovalama (hâlâ makul), "
        "vade içi katalizör (0), bilinmeyen aile (1), ceza defteri (0 uygulanan)."
    )
    row, evidence = _measured_row_evidence()
    before_b3 = build_narrative(row, evidence, _chip(*_TRADABLE), settings=_NARRATIVE)
    assert before_b3.checked == "Bakılanlar: maliyet (İŞLENİR), aleyhte aile (0), bilinmeyen aile (1)."


# ---------------------------------------------------------------------------
# Reason sentence
# ---------------------------------------------------------------------------


def test_reason_names_the_lehte_families_direction_and_position() -> None:
    row, evidence = _measured_row_evidence()
    narrative = build_narrative(row, evidence, _chip(*_TRADABLE), settings=_NARRATIVE)
    # Phase 5.2.A-fix4 (D10, review FA-03): dealer gamma is non-directional (decision P9),
    # so it is no longer listed under "yukarıyı gösteriyor"; it gets its own clause.
    assert narrative.reason == (
        "Neden: 3 bağımsız kaynak yukarıyı gösteriyor (Akış, Karanlık havuz, Sektör); "
        "Dealer gamma yön göstermez, hareketi büyütebilir; "
        "prim tek strike'ta toplanmış (kasıtlı pozisyon)."
    )


def test_reason_for_a_sold_call_points_down() -> None:
    (row,) = build_board_rows(_prints(fill_side="at_bid"), _SETTINGS.aggregation)
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=None,
        telemetry=_telemetry({"sector": "contrarian", "price_confirmation": "neutral"}),
        tape=_tape(-_DEADBAND * 2), ticker_info=None,
    )
    narrative = build_narrative(row, evidence, _chip(*_TRADABLE), settings=_NARRATIVE)
    assert narrative.reason.startswith("Neden: 2 bağımsız kaynak aşağıyı gösteriyor (Akış, Sektör); ")


def test_reason_without_lehte_families_and_other_position_reads() -> None:
    (mixed,) = build_board_rows(_prints(strikes=("100", "105")), _SETTINGS.aggregation)
    evidence = build_row_evidence(
        mixed, settings=_SETTINGS, now=_NOW, signal=None, telemetry=None, tape=None, ticker_info=None,
    )
    assert build_narrative(mixed, evidence, _chip(*_TRADABLE), settings=_NARRATIVE).reason == (
        "Neden: lehte bağımsız kaynak yok, satırı yalnızca akış baskıları oluşturuyor; "
        "prim birkaç strike'a yayılmış (karışık)."
    )
    (scattered,) = build_board_rows(_prints(strikes=("100", "105", "110", "115")), _SETTINGS.aggregation)
    evidence = build_row_evidence(
        scattered, settings=_SETTINGS, now=_NOW, signal=None, telemetry=None, tape=None, ticker_info=None,
    )
    assert build_narrative(scattered, evidence, _chip(*_TRADABLE), settings=_NARRATIVE).reason.endswith(
        "prim birçok strike'a dağılmış (dağınık envanter).",
    )


# ---------------------------------------------------------------------------
# Copy (R-CA1, R-WD1)
# ---------------------------------------------------------------------------


def test_every_counter_template_starts_with_ama() -> None:
    for template in COUNTER_TEMPLATES.values():
        assert template.startswith(f"{COUNTER_LEAD} ")


def test_every_template_and_generated_string_is_clean() -> None:
    texts: list[str] = [
        *NARRATIVE_COPY.values(), *REASON_TEMPLATES.values(), *DIRECTION_OBJECTS.values(),
        *POSITION_CLAUSES.values(), *COUNTER_TEMPLATES.values(), *CHECK_TEMPLATES.values(),
        NO_COUNTER_FOUND,
    ]
    flags = dict.fromkeys(COUNTER_PRIORITY, True)
    for key in (*COUNTER_PRIORITY, None):
        narrative = _narrative(**flags)
        texts.extend(t for t in (narrative.reason, narrative.counter, narrative.checked) if t is not None)
        if key is not None:
            flags[key] = False
    for text in texts:
        assert ensure_clean(text) == text


def test_dictionaries_are_read_only() -> None:
    for mapping in (NARRATIVE_COPY, REASON_TEMPLATES, POSITION_CLAUSES, COUNTER_TEMPLATES, CHECK_TEMPLATES):
        with pytest.raises(TypeError):
            mapping["x"] = "y"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Rendered row structure (R-CA1, R-CA2)
# ---------------------------------------------------------------------------


def test_bull_and_bear_columns_share_width_and_typography() -> None:
    from webapp.main import templates

    prints = [*_prints(ticker="AAA"), *_prints(ticker="BBB")]
    telemetry = {
        "AAA0": _telemetry(_MEASURED),
        "BBB0": _telemetry({**_MEASURED, "price_confirmation": "call_contrarian"}),
    }
    tapes = {("AAA", _TS.date()): _tape(_DEADBAND * 2)}

    def evidence_source(run_id: str, requests: Any) -> EvidenceInputs:
        return EvidenceInputs(telemetry=telemetry, tapes=tapes, infos={})

    def quote_source(symbols: Any) -> Any:
        return {s: _quote(s, *_TRADABLE) for s in symbols}, {}

    page = build_alfa_page(
        prints, _SETTINGS, now=_NOW, spread_cutoff_pct=_CUTOFF,
        quote_source=quote_source, evidence_source=evidence_source,
    )
    template = templates.env.get_template("_alfa_row.html")
    counters: dict[str, str] = {}
    for view in page.views:
        rendered = template.render(row=view.row, view=view, **template_context())
        cases = re.search(r'<div class="([^"]*)" data-cases>', rendered)
        bull = re.search(r'<div class="([^"]*)" data-case="bull">', rendered)
        bear = re.search(r'<div class="([^"]*)" data-case="bear">', rendered)
        assert cases is not None and bull is not None and bear is not None
        assert "md:grid-cols-2" in cases.group(1)
        assert bull.group(1) == bear.group(1) == CASE_CLASS
        titles = re.findall(r'<p class="([^"]*)" data-case-title>', rendered)
        assert len(titles) == 2 and titles[0] == titles[1]
        counter = re.search(r"<p data-counter>([^<]*)</p>", rendered)
        assert counter is not None
        text = html.unescape(counter.group(1))
        counters[view.row.ticker] = text
        if text == NO_COUNTER_FOUND:
            checked = re.search(r"<p data-checked>([^<]*)</p>", rendered)
            assert checked is not None
            assert html.unescape(checked.group(1)).startswith("Bakılanlar: ")
        else:
            assert text.startswith(f"{COUNTER_LEAD} ")
            assert "data-checked" not in rendered
    assert counters == {"AAA": NO_COUNTER_FOUND, "BBB": "AMA 1 aile aleyhte: Fiyat teyidi."}
