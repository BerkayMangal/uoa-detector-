"""Phase 5.2.C2a: the pass ledger page model (``webapp/board/decision_ledger.py``).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2).

Pure: no database, no HTTP, no clock. The cards are built by hand so the tests
say exactly which snapshot a face was read from.

Pins:
  - the face is read out of the FROZEN snapshot, and a card the page does not
    recognise (an older or newer board, a truncated document) reads
    ``bilinmiyor`` instead of raising — a ledger that 500s on an old card would
    lose the history it exists to show;
  - one cell per profile horizon, and a horizon with no stored row reads
    ``henüz hesaplanmadı``: never a zero;
  - the contract's own counts line, byte for byte, below the gate;
  - the gate holds in BOTH directions at ``fills.min_n_for_stats``, and one full
    group alone unlocks nothing (contract §4: "each group");
  - only rows that reached a final status are counted, and only measured ones
    enter a statistic;
  - the cap is disclosed, and an empty ledger says which kind of nothing it is;
  - every frozen and generated string passes ``honesty.ensure_clean``.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board.cards import DECISIONS, DecisionCard
from webapp.board.decision_ledger import (
    DEFTER_COPY,
    CardFace,
    build_ledger_page,
    card_face,
    shown,
    template_context,
)
from webapp.board.fills import FILL_COPY
from webapp.board.honesty import ensure_clean, forbidden_words
from webapp.board.outcomes import (
    COMPUTED,
    NO_DATA,
    OUTCOME_COPY,
    PENDING,
    Outcome,
    signed_pct_text,
)
from webapp.board.settings import load_board_settings

if TYPE_CHECKING:
    from webapp.board.cards import Json

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_MIN_N = _SETTINGS.fills.min_n_for_stats  # D8: profile-driven, never a literal
_HORIZONS = _SETTINGS.outcomes.horizons_trading_days
_LIMIT = _SETTINGS.ledger.max_cards_per_page

_T0 = datetime(2026, 9, 16, 14, 5, tzinfo=UTC)
_SYMBOL = "SPY260918C00760000"

# A snapshot shaped like the real frozen row view (webapp/board/cards.py).
_SNAPSHOT: dict[str, object] = {
    "row_view": {
        "symbol": _SYMBOL,
        "strength_text": "Orta",
        "chip_text": {
            "spread": "%2",
            "round_trip": "$134",
            "lot": "$100",
            "exit_depth": "12 kontrat",
            "quote_age": "kotasyon 41 sn önce alındı",
        },
        "narrative": {
            "reason": "Tek strike'ta yoğunlaşma",
            "counter": "AMA makas geniş",
        },
        "evidence": {"counts": {"supporting": 4, "against": 0, "unknown": 2}},
    },
    "constituents": [["live-2026-09-16", "s1"]],
}


def _card(
    *,
    card_id: str = "c1",
    decision: str = "pas",
    ticker: str = "SPY",
    direction: str = "yukarı",
    created: datetime = _T0,
    snapshot: object = None,
    symbol: str | None = _SYMBOL,
) -> DecisionCard:
    document = _SNAPSHOT if snapshot is None else snapshot
    return DecisionCard(
        id=card_id,
        created_at=created,
        decision=decision,
        ticker=ticker,
        direction=direction,
        run_id="live-2026-09-16",
        dominant_option_symbol=symbol,
        card_json=json.dumps(document, ensure_ascii=False),
        board_version="unknown",
        board_profile_hash="hash",
        calibration_profile_hash=None,
        trade_id=None,
        note=None,
    )


def _outcome(
    card_id: str,
    *,
    horizon_days: int = 1,
    status: str = COMPUTED,
    excess: float | None = 1.5,
    option_bid: float | None = None,
) -> Outcome:
    return Outcome(
        card_id=card_id,
        horizon_days=horizon_days,
        computed_at=_T0,
        underlying_close_at_card_day=100.0,
        underlying_close_at_horizon=102.0,
        spy_close_at_card_day=400.0,
        spy_close_at_horizon=402.0,
        market_neutral_excess=excess,
        option_symbol=_SYMBOL,
        option_bid_at_horizon=option_bid,
        status=status,
    )


def _page(
    cards: list[DecisionCard],
    outcomes: list[Outcome] | None = None,
    **kwargs: object,
) -> object:
    return build_ledger_page(
        cards,
        outcomes or [],
        horizons=_HORIZONS,
        min_n=_MIN_N,
        limit=_LIMIT,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The frozen face
# ---------------------------------------------------------------------------


def test_the_face_is_read_out_of_the_frozen_snapshot() -> None:
    face = card_face(_card())
    assert face == CardFace(
        strength="Orta",
        spread="%2",
        round_trip="$134",
        lot="$100",
        exit_depth="12 kontrat",
        quote_age="kotasyon 41 sn önce alındı",
        reason="Tek strike'ta yoğunlaşma",
        counter="AMA makas geniş",
        symbol=_SYMBOL,
        supporting=4,
        against=0,
        unknown=2,
    )
    assert face.evidence_counts_text == "4 lehte · 0 aleyhte · 2 bilinmiyor"


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param({}, id="empty-document"),
        pytest.param("not-a-mapping", id="a-string"),
        pytest.param([1, 2, 3], id="a-list"),
        pytest.param({"row_view": None}, id="null-row-view"),
        pytest.param({"row_view": {"chip_text": 5}}, id="chip-text-is-a-number"),
        pytest.param({"row_view": {"narrative": []}}, id="narrative-is-a-list"),
        pytest.param({"row_view": {"strength_text": ""}}, id="blank-strings"),
        pytest.param({"row_view": {"strength_text": "   "}}, id="whitespace-only"),
        pytest.param({"row_view": {"evidence": {"counts": {"supporting": True}}}}, id="bool-count"),
        pytest.param({"row_view": {"evidence": {"counts": {"supporting": "4"}}}}, id="text-count"),
    ],
)
def test_a_card_the_page_does_not_recognise_reads_unknown(snapshot: object) -> None:
    face = card_face(_card(snapshot=snapshot, symbol=None))
    assert face == CardFace(*([None] * 12))  # type: ignore[arg-type]
    assert face.evidence_counts_text == "bilinmiyor"
    for value in (face.strength, face.reason, face.counter, face.spread):
        assert shown(value) == "bilinmiyor"


def test_the_symbol_falls_back_to_the_cards_own_column() -> None:
    """A snapshot without a symbol still names the contract the card recorded."""
    face = card_face(_card(snapshot={"row_view": {}}, symbol="NVDA260918P00170000"))
    assert face.symbol == "NVDA260918P00170000"


def test_a_partial_snapshot_keeps_what_it_has() -> None:
    face = card_face(_card(snapshot={"row_view": {"strength_text": "Zayıf", "narrative": {}}}))
    assert face.strength == "Zayıf"
    assert face.reason is None
    assert shown(face.reason) == "bilinmiyor"


# ---------------------------------------------------------------------------
# Rows and cells
# ---------------------------------------------------------------------------


def test_one_cell_per_profile_horizon_in_profile_order() -> None:
    page = _page([_card()])
    (row,) = page.rows  # type: ignore[attr-defined]
    assert [cell.horizon_days for cell in row.cells] == list(_HORIZONS)
    assert [cell.horizon_text for cell in row.cells] == [f"{n} gün" for n in _HORIZONS]


def test_a_horizon_with_no_stored_row_is_never_a_zero() -> None:
    (row,) = _page([_card()]).rows  # type: ignore[attr-defined]
    cell = row.cells[0]
    assert cell.outcome is None
    assert cell.status_text == "henüz hesaplanmadı"
    assert cell.status_text == OUTCOME_COPY["not_computed"]
    assert cell.excess_text == "bilinmiyor"
    assert cell.option_bid_text == "bilinmiyor"
    assert cell.measured is False


def test_a_cell_carries_its_stored_outcome() -> None:
    stored = _outcome("c1", horizon_days=_HORIZONS[0], excess=-2.5, option_bid=0.75)
    (row,) = _page([_card()], [stored]).rows  # type: ignore[attr-defined]
    cell = row.cells[0]
    assert cell.outcome is stored
    assert cell.status_text == COMPUTED
    assert cell.excess_text == signed_pct_text(-2.5) == "-%2.5"
    assert cell.option_bid_text == "$0.75"
    assert cell.measured is True


def test_an_outcome_of_another_card_never_lands_on_this_one() -> None:
    other = _outcome("somebody-else", horizon_days=_HORIZONS[0])
    (row,) = _page([_card()], [other]).rows  # type: ignore[attr-defined]
    assert all(cell.outcome is None for cell in row.cells)


def test_a_row_keeps_the_repositorys_order_and_its_own_identity() -> None:
    newest = _card(card_id="new", created=_T0)
    older = _card(card_id="old", created=_T0 - timedelta(days=1), decision="log")
    page = _page([newest, older])
    assert [row.card.id for row in page.rows] == ["new", "old"]  # type: ignore[attr-defined]
    assert page.rows[0].href == "/kart/new"  # type: ignore[attr-defined]
    assert page.rows[0].created_text == "2026-09-16 14:05 UTC"  # type: ignore[attr-defined]


def test_the_decision_word_is_the_one_the_card_page_uses() -> None:
    log = _page([_card(decision="log")]).rows[0]  # type: ignore[attr-defined]
    pas = _page([_card(decision="pas")]).rows[0]  # type: ignore[attr-defined]
    assert log.decision_text == FILL_COPY["decision_log"] == "loglandı"
    assert pas.decision_text == FILL_COPY["decision_pas"] == "pas geçildi"


# ---------------------------------------------------------------------------
# Count-gated aggregates (contract §4)
# ---------------------------------------------------------------------------


def _measured(decision: str, n: int, *, excess: float = 1.5) -> tuple[list[DecisionCard], list[Outcome]]:
    cards = [_card(card_id=f"{decision}-{i}", decision=decision) for i in range(n)]
    outcomes = [
        _outcome(card.id, horizon_days=_HORIZONS[0], excess=excess + i)
        for i, card in enumerate(cards)
    ]
    return cards, outcomes


def test_the_counts_line_is_the_contracts_own_example() -> None:
    pas_cards, pas_outcomes = _measured("pas", 12)
    log_cards, log_outcomes = _measured("log", 3)
    page = _page([*pas_cards, *log_cards], [*pas_outcomes, *log_outcomes])
    summary = page.summaries[0]  # type: ignore[attr-defined]
    assert summary.counts_text == "12 pas, 3 log; istatistik için yetersiz örnek"
    assert summary.enough is False


def test_below_the_gate_no_group_reports_a_statistic() -> None:
    cards, outcomes = _measured("log", _MIN_N - 1)
    summary = _page(cards, outcomes).summaries[0]  # type: ignore[attr-defined]
    assert summary.enough is False
    for group in summary.groups:
        assert group.stats.enough is False
        assert group.stats.median_pct is None
        assert group.stats.q1_pct is None
        assert group.stats.q3_pct is None
    assert summary.counts_text.endswith("istatistik için yetersiz örnek")


def test_one_group_at_the_gate_alone_reports_nothing() -> None:
    """Contract §4: "unless EACH group has at least ``fills.min_n_for_stats``"."""
    log_cards, log_outcomes = _measured("log", _MIN_N)
    pas_cards, pas_outcomes = _measured("pas", _MIN_N - 1)
    page = _page([*log_cards, *pas_cards], [*log_outcomes, *pas_outcomes])
    summary = page.summaries[0]  # type: ignore[attr-defined]
    groups = {group.decision: group for group in summary.groups}
    # Each group still counts its own measured sample...
    assert groups["log"].stats.enough is True
    assert groups["pas"].stats.enough is False
    assert groups["pas"].stats.median_pct is None
    # ...but nothing is reportable while the control group is one card short, so
    # no median is ever shown beside a sample that cannot support a comparison.
    assert summary.enough is False
    assert summary.reportable_groups == ()
    assert summary.counts_text == f"{_MIN_N - 1} pas, {_MIN_N} log; istatistik için yetersiz örnek"


def test_only_final_rows_are_counted() -> None:
    cards = [_card(card_id=f"c{i}", decision="pas") for i in range(3)]
    outcomes = [
        _outcome("c0", horizon_days=_HORIZONS[0], status=PENDING, excess=None),
        _outcome("c1", horizon_days=_HORIZONS[0], status=NO_DATA, excess=None),
        _outcome("c2", horizon_days=_HORIZONS[0], status=COMPUTED, excess=1.0),
    ]
    summary = _page(cards, outcomes).summaries[0]  # type: ignore[attr-defined]
    groups = {group.decision: group for group in summary.groups}
    # veri yok is final (it will never be recomputed); bekliyor is not.
    assert groups["pas"].final_n == 2
    # Only the measured one can enter a statistic: "veri yok" is not a zero.
    assert groups["pas"].stats.n == 1


def test_every_horizon_gets_its_own_summary() -> None:
    cards, outcomes = _measured("log", 2)
    page = _page(cards, outcomes)
    assert [s.horizon_days for s in page.summaries] == list(_HORIZONS)  # type: ignore[attr-defined]
    assert page.summaries[1].counts_text == "0 pas, 0 log; istatistik için yetersiz örnek"  # type: ignore[attr-defined]


def test_both_decisions_always_have_a_group() -> None:
    summary = _page([_card()]).summaries[0]  # type: ignore[attr-defined]
    assert tuple(group.decision for group in summary.groups) == DECISIONS


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_cap_is_disclosed_and_says_when_it_bit() -> None:
    page = build_ledger_page(
        [_card(card_id=f"c{i}") for i in range(3)], [], horizons=_HORIZONS, min_n=_MIN_N, limit=3,
    )
    assert page.capped is True
    assert page.cap_text == "En son 3 kart listelenir."
    smaller = build_ledger_page(
        [_card()], [], horizons=_HORIZONS, min_n=_MIN_N, limit=3,
    )
    assert smaller.capped is False


def test_an_empty_ledger_says_which_kind_of_nothing_it_is() -> None:
    empty = _page([])
    assert empty.filtered is False  # type: ignore[attr-defined]
    assert empty.empty_text == "Kayıtlı karar kartı yok."  # type: ignore[attr-defined]
    filtered = _page([], decision="pas")
    assert filtered.filtered is True  # type: ignore[attr-defined]
    assert filtered.empty_text == "Bu süzgeçle eşleşen kart yok."  # type: ignore[attr-defined]
    assert _page([], ticker="SPY").filtered is True  # type: ignore[attr-defined]


def test_the_active_filters_come_back_for_the_form() -> None:
    page = _page([_card()], decision="pas", ticker="SPY")
    assert (page.decision, page.ticker) == ("pas", "SPY")  # type: ignore[attr-defined]


def test_a_failed_read_is_a_state_of_its_own() -> None:
    page = _page([], load_failed=True)
    assert page.load_failed is True  # type: ignore[attr-defined]


def test_the_page_model_never_touches_a_database() -> None:
    """Everything it needs is passed in: the route does the two reads, once each."""
    parameters = set(inspect.signature(build_ledger_page).parameters)
    assert "engine" not in parameters
    assert "repo" not in parameters
    assert parameters == {
        "cards", "outcomes", "horizons", "min_n", "limit", "decision", "ticker", "load_failed",
    }


# ---------------------------------------------------------------------------
# Text (contract R-WD1)
# ---------------------------------------------------------------------------


def test_every_frozen_and_generated_string_is_clean() -> None:
    for text in DEFTER_COPY.values():
        assert ensure_clean(text) == text
    pas_cards, pas_outcomes = _measured("pas", 2)
    page = _page(pas_cards, pas_outcomes)
    generated = [
        page.cap_text,  # type: ignore[attr-defined]
        page.empty_text,  # type: ignore[attr-defined]
        page.summaries[0].counts_text,  # type: ignore[attr-defined]
        page.summaries[0].horizon_text,  # type: ignore[attr-defined]
        page.summaries[0].groups[0].sample_text,  # type: ignore[attr-defined]
        page.rows[0].decision_text,  # type: ignore[attr-defined]
        page.rows[0].created_text,  # type: ignore[attr-defined]
        page.rows[0].face.evidence_counts_text,  # type: ignore[attr-defined]
        page.rows[0].cells[0].status_text,  # type: ignore[attr-defined]
        shown(None),
    ]
    for text in generated:
        assert ensure_clean(text) == text
    assert forbidden_words(" ".join(DEFTER_COPY.values())) == []


def test_the_template_context_carries_only_frozen_copy_and_helpers() -> None:
    context = template_context()
    assert context["defter_copy"] is DEFTER_COPY
    assert context["defter_decisions"] == DECISIONS
    assert callable(context["defter_shown"])


def test_the_snapshot_document_type_is_the_cards_own() -> None:
    """The face reads ``DecisionCard.card`` (parsed ``card_json``), nothing else."""
    document: Json = _card().card
    assert isinstance(document, dict)
    assert "row_view" in document
