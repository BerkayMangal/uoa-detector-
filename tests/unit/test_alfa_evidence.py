"""Phase 5.2.A3: four-state evidence (``webapp/board/evidence.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-UN1/R-UN2/R-WD1,
§5 A3, §5 A4 (counts, strength) and §9; decisions P8, P9 and P10.

Pins, one test (or parameter) per §9 cell:
  - Akış: lehte / aleyhte / nötr inside the dead-band (and exactly on it) /
    bilinmiyor without a tape or with a stale one;
  - Dealer gamma: lehte labelled ``hareketi büyütebilir``; the three nötr
    branches; bilinmiyor for no_data, timeout and deg; never aleyhte, even for
    a sold option;
  - Karanlık havuz, Sektör, Fiyat teyidi: every branch named in the table,
    deg, and the kapsam-dışı cells (Sektör ``no_sector`` only for ETF/Index);
  - Açık pozisyon: açılış, kapanış, henüz doğrulanmadı, no confirm row
    (``bilinmiyor (T+1 bekleniyor)``), expires before T+1;
  - the legacy table (M23 1.0/0.3, M25 >= 0.7/0.0, M26 True; everything
    else bilinmiyor labelled ``telemetri yok (eski satır)``; M27 never maps);
  - orientation flip for sold options; nötr and kapsam-dışı are not counted;
  - the strength label and the R-UN2 render guard;
  - the source print is the largest print of the dominant contract;
  - the database reader, and every generated string through ``ensure_clean``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.aggregate import build_board_rows
from webapp.board.copy_tr import STRONG_LABEL
from webapp.board.db import make_engine
from webapp.board.evidence import (
    BRANCH_STATES,
    EMPTY_INPUTS,
    EVIDENCE_COPY,
    FAMILY_LABELS,
    NOTE_TEMPLATES,
    STATE_LABELS,
    STRENGTH_LABELS,
    EvidenceCounts,
    EvidenceRequest,
    FamilyRead,
    LegacyScores,
    StageTelemetryView,
    build_row_evidence,
    classify_strength,
    count_families,
    dominant_print,
    evidence_sort_key,
    flow_family,
    guard_strength,
    legacy_family,
    load_legacy_scores,
    open_interest_family,
    read_evidence_inputs,
    request_for,
    stage_family,
    strength_label,
    trade_date_et,
)
from webapp.board.honesty import ensure_clean
from webapp.board.netprem import TapeMinute, TapeSummary, upsert_tape
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.telemetry import AlfaStageTelemetry, ensure_telemetry_tables
from webapp.board.ticker_info import TickerInfoSnapshot, TickerInfoView, upsert_ticker_infos

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from webapp.board.aggregate import BoardRow

    from uoa_detector.backtest.store import StoredSignal

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_EVIDENCE = _SETTINGS.evidence
_LEGACY = load_legacy_scores(_REPO / "profiles" / "v5_default.yaml")
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_RUN = "live-2026-09-15"
_DEADBAND = _EVIDENCE.flow_net_premium_deadband_usd
_MAX_AGE = _SETTINGS.tape.max_age_seconds


def _t(branch: str | None, *, degraded: bool = False) -> StageTelemetryView:
    return StageTelemetryView(stage_name="stage", branch=branch, degraded=degraded)


def _info(issue_type: str | None) -> TickerInfoView:
    return TickerInfoView(ticker="X", issue_type=issue_type, sector=None, fetched_at=_NOW)


def _tape(bullish_net: float, *, age_seconds: float = 60.0) -> TapeSummary:
    return TapeSummary(
        ticker="SPY", trade_date=_TS.date(), net_call_premium=bullish_net, net_put_premium=0.0,
        minutes=30, first_tape_time=_TS, last_tape_time=_NOW,
        fetched_at=_NOW - timedelta(seconds=age_seconds),
    )


def _signal(
    event_id: str,
    *,
    ticker: str = "SPY",
    option_type: str = "call",
    strike: str = "760",
    premium: str = "100000",
    second: int = 0,
    **scores: Any,
) -> StoredSignal:
    pr = build_print(
        event_id=event_id, ts=_TS + timedelta(seconds=second), ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike, dte=3, premium=premium,
    )
    sig = BacktestStore().add(
        EnrichedEvent(print=pr),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )
    return sig.model_copy(update=scores) if scores else sig


def _board_print(sig: StoredSignal, event_id: str, fill_side: str | None) -> BoardPrint:
    meta = None if fill_side is None else PrintMetaView(fill_side=fill_side, option_chain=None)
    return BoardPrint(run_id=_RUN, event_id=event_id, signal=sig, meta=meta)


def _rows(*prints: BoardPrint) -> list[BoardRow]:
    return build_board_rows(list(prints), _SETTINGS.aggregation)


# ---------------------------------------------------------------------------
# §9: Dealer gamma (M21, non-directional)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "state"),
    [
        ("full_short_and_proximate", "supporting"),
        ("partial_one_condition", "neutral"),
        ("no_conditions_met", "neutral"),
        ("extreme_distance_cutoff", "neutral"),
        ("no_data", "unknown"),
        ("timeout", "unknown"),
    ],
)
@pytest.mark.parametrize("sold", [False, True])
def test_dealer_gamma_cells(branch: str, state: str, sold: bool) -> None:
    read = stage_family("dealer_gamma", _t(branch), sold=sold)
    assert read.state == state
    assert read.flipped is False


def test_dealer_gamma_lehte_is_labelled_amplifies() -> None:
    read = stage_family("dealer_gamma", _t("full_short_and_proximate"), sold=True)
    assert read.text == "Dealer gamma: lehte (hareketi büyütebilir)"


def test_dealer_gamma_is_never_aleyhte() -> None:
    assert "against" not in set(BRANCH_STATES["dealer_gamma"].values())


def test_dealer_gamma_degraded_is_unknown() -> None:
    read = stage_family("dealer_gamma", _t("full_short_and_proximate", degraded=True), sold=False)
    assert read.state == "unknown"
    assert read.note == NOTE_TEMPLATES["degraded"]


# ---------------------------------------------------------------------------
# §9: Karanlık havuz (M26), Sektör (M25), Fiyat teyidi (M23)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "branch", "state"),
    [
        ("dark_pool", "confirmed_match", "supporting"),
        ("dark_pool", "direction_mismatch", "against"),
        ("dark_pool", "direction_unclear", "neutral"),
        ("dark_pool", "no_qualifying_prints", "neutral"),
        ("dark_pool", "timeout", "unknown"),
        ("dark_pool", "unknown_option_type", "out_of_scope"),
        ("sector", "strong", "supporting"),
        ("sector", "moderate", "supporting"),
        ("sector", "contrarian", "against"),
        ("sector", "weak", "neutral"),
        ("sector", "all_neutral", "neutral"),
        ("sector", "empty_peer_flow", "neutral"),
        ("sector", "timeout", "unknown"),
        ("sector", "unknown_option_type", "out_of_scope"),
        ("price_confirmation", "call_confirmed", "supporting"),
        ("price_confirmation", "put_confirmed", "supporting"),
        ("price_confirmation", "call_contrarian", "against"),
        ("price_confirmation", "put_contrarian", "against"),
        ("price_confirmation", "neutral", "neutral"),
        ("price_confirmation", "timeout", "unknown"),
        ("price_confirmation", "provider_error", "unknown"),
        ("price_confirmation", "data_missing_neutral", "unknown"),
        ("price_confirmation", "neutral_unknown_type", "out_of_scope"),
    ],
)
def test_directional_family_cells(family: str, branch: str, state: str) -> None:
    assert stage_family(family, _t(branch), sold=False).state == state


@pytest.mark.parametrize("family", ["dark_pool", "sector", "price_confirmation"])
@pytest.mark.parametrize("branch", ["confirmed_match", "strong", "call_confirmed", "no_qualifying_prints"])
def test_degraded_call_always_reads_unknown(family: str, branch: str) -> None:
    read = stage_family(family, _t(branch, degraded=True), sold=False)
    assert read.state == "unknown"
    assert read.dimmed is True


@pytest.mark.parametrize(
    ("info", "degraded", "state"),
    [
        (_info("ETF"), False, "out_of_scope"),
        (_info("Index"), False, "out_of_scope"),
        (_info("etf"), False, "out_of_scope"),
        (_info("Common Stock"), False, "unknown"),
        (_info("ADR"), False, "unknown"),
        (_info(None), False, "unknown"),
        (None, False, "unknown"),
        (_info("ETF"), True, "unknown"),
    ],
)
def test_sector_no_sector_is_out_of_scope_only_for_etf_or_index(
    info: TickerInfoView | None, degraded: bool, state: str,
) -> None:
    read = stage_family("sector", _t("no_sector", degraded=degraded), sold=False, ticker_info=info)
    assert read.state == state


def test_missing_stage_row_and_unmapped_branches_read_unknown_never_out_of_scope() -> None:
    assert stage_family("sector", None, sold=False).state == "unknown"
    for branch in ("preset_skip", "something_new", None):
        read = stage_family("price_confirmation", _t(branch), sold=False)
        assert read.state == "unknown", branch


# ---------------------------------------------------------------------------
# Orientation: sold options swap lehte and aleyhte for M23, M25 and M26
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "branch", "sold_state"),
    [
        ("price_confirmation", "call_confirmed", "against"),
        ("price_confirmation", "call_contrarian", "supporting"),
        ("sector", "strong", "against"),
        ("sector", "contrarian", "supporting"),
        ("dark_pool", "confirmed_match", "against"),
        ("dark_pool", "direction_mismatch", "supporting"),
        ("dark_pool", "direction_unclear", "neutral"),
        ("sector", "timeout", "unknown"),
        ("price_confirmation", "neutral_unknown_type", "out_of_scope"),
    ],
)
def test_sold_option_flips_directional_results(family: str, branch: str, sold_state: str) -> None:
    read = stage_family(family, _t(branch), sold=True)
    assert read.state == sold_state
    assert read.flipped is (sold_state in ("supporting", "against"))
    if read.flipped:
        assert read.note == NOTE_TEMPLATES["sold_flip"]


# ---------------------------------------------------------------------------
# §9: Akış (net-premium tape, read in the row's direction)
# ---------------------------------------------------------------------------


def test_flow_lehte_aleyhte_and_notr_around_the_deadband() -> None:
    strong = _tape(_DEADBAND + 150_000)
    assert flow_family(strong, "up", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW).state == "supporting"
    assert flow_family(strong, "down", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW).state == "against"
    inside = _tape(_DEADBAND - 1)
    assert flow_family(inside, "up", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW).state == "neutral"
    assert flow_family(inside, "down", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW).state == "neutral"
    on_the_band = _tape(_DEADBAND)
    assert flow_family(on_the_band, "up", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW).state == "neutral"
    read = flow_family(strong, "up", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW)
    assert read.text == "Akış: lehte · bu yönde gün içi net prim +$400,000"


def test_flow_net_premium_uses_calls_minus_puts() -> None:
    tape = TapeSummary(
        ticker="NVDA", trade_date=_TS.date(), net_call_premium=1_990_676.0, net_put_premium=-2_021_489.0,
        minutes=3, first_tape_time=_TS, last_tape_time=_TS, fetched_at=_NOW,
    )
    assert tape.bullish_net_premium == 4_012_165.0
    assert tape.net_premium_for("down") == -4_012_165.0


def test_flow_without_a_tape_or_with_a_stale_one_is_unknown() -> None:
    missing = flow_family(None, "up", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW)
    assert (missing.state, missing.note) == ("unknown", NOTE_TEMPLATES["tape_missing"])
    stale = flow_family(
        _tape(_DEADBAND * 10, age_seconds=_MAX_AGE + 1), "up",
        deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW,
    )
    assert stale.state == "unknown"
    assert stale.note == "net prim bandı 15 dk önce alındı, eski"
    at_limit = flow_family(
        _tape(_DEADBAND * 10, age_seconds=_MAX_AGE), "up",
        deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW,
    )
    assert at_limit.state == "supporting"


# ---------------------------------------------------------------------------
# §9: Açık pozisyon (B4 T+1 confirmation only; P10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "family_state", "text"),
    [
        (None, "unknown", "Açık pozisyon: bilinmiyor (T+1 bekleniyor)"),
        ("opening", "supporting", "Açık pozisyon: lehte"),
        ("closing", "against", "Açık pozisyon: aleyhte"),
        ("unconfirmed", "unknown", "Açık pozisyon: bilinmiyor (henüz doğrulanmadı)"),
        ("expires_before_t1", "out_of_scope", "Açık pozisyon: kapsam-dışı (T+1'den önce vade)"),
    ],
)
def test_open_interest_cells(state: Any, family_state: str, text: str) -> None:
    read = open_interest_family(state)
    assert read.state == family_state
    assert read.text == text


# ---------------------------------------------------------------------------
# §9 legacy table (no telemetry)
# ---------------------------------------------------------------------------


def test_legacy_scores_are_the_contract_values_of_the_live_profile() -> None:
    assert LegacyScores(
        m23_confirmed=1.0, m23_contrarian=0.3, m25_supporting_min=0.7, m25_contrarian=0.0,
    ) == _LEGACY


@pytest.mark.parametrize(
    ("family", "scores", "state"),
    [
        ("price_confirmation", {"price_confirmation_score": 1.0}, "supporting"),
        ("price_confirmation", {"price_confirmation_score": 0.3}, "against"),
        ("price_confirmation", {"price_confirmation_score": 0.5}, "unknown"),
        ("price_confirmation", {"price_confirmation_score": None}, "unknown"),
        ("sector", {"sector_confirmation_score": 0.7}, "supporting"),
        ("sector", {"sector_confirmation_score": 1.0}, "supporting"),
        ("sector", {"sector_confirmation_score": 0.0}, "against"),
        ("sector", {"sector_confirmation_score": 0.5}, "unknown"),
        ("sector", {"sector_confirmation_score": 0.3}, "unknown"),
        ("dark_pool", {"dark_pool_confirmation": True}, "supporting"),
        ("dark_pool", {"dark_pool_confirmation": False}, "unknown"),
        ("dealer_gamma", {"gamma_score": 1.0}, "unknown"),
    ],
)
def test_legacy_cells(family: str, scores: dict[str, Any], state: str) -> None:
    read = legacy_family(family, _signal("L1", **scores), sold=False, scores=_LEGACY)
    assert read.state == state
    expected_note = NOTE_TEMPLATES["legacy"] if state == "unknown" else NOTE_TEMPLATES["legacy_value"]
    assert read.note == expected_note


def test_legacy_unknown_is_labelled_telemetry_missing() -> None:
    read = legacy_family("dealer_gamma", _signal("L2"), sold=False, scores=_LEGACY)
    assert read.text == "Dealer gamma: bilinmiyor · telemetri yok (eski satır)"


def test_legacy_without_scores_maps_nothing() -> None:
    read = legacy_family("price_confirmation", _signal("L3", price_confirmation_score=1.0), sold=False, scores=None)
    assert read.state == "unknown"


def test_legacy_value_equal_to_a_fallback_score_never_maps() -> None:
    profile = load_profile(_REPO / "profiles" / "v5_default.yaml")
    modules = profile.scoring.modules
    m23 = modules.m23.model_copy(update={"contrarian_score": modules.m23.neutral_score})
    m25 = modules.m25.model_copy(update={"contrarian_score": modules.m25.timeout_score})
    ambiguous = profile.model_copy(
        update={
            "scoring": profile.scoring.model_copy(
                update={"modules": modules.model_copy(update={"m23": m23, "m25": m25})},
            ),
        },
    )
    scores = LegacyScores.from_profile(ambiguous)
    assert scores.m23_contrarian is None
    assert scores.m25_contrarian is None
    assert scores.m23_confirmed == 1.0


def test_m27_opening_closing_score_never_maps_to_open_interest() -> None:
    rows = _rows(_board_print(_signal("L4", opening_closing_score=1.0), "L4", None))
    evidence = build_row_evidence(
        rows[0], settings=_SETTINGS, now=_NOW, signal=_signal("L4", opening_closing_score=1.0),
        telemetry=None, tape=None, ticker_info=None, legacy_scores=_LEGACY,
    )
    oi = next(f for f in evidence.families if f.family == "open_interest")
    assert oi.text == "Açık pozisyon: bilinmiyor (T+1 bekleniyor)"


# ---------------------------------------------------------------------------
# Counts and strength (A4, R-UN1, R-UN2)
# ---------------------------------------------------------------------------


def _fr(state: str) -> FamilyRead:
    return FamilyRead(family="flow", state=state)  # type: ignore[arg-type]


def test_neutral_and_out_of_scope_are_not_counted() -> None:
    counts = count_families(
        [_fr("supporting"), _fr("supporting"), _fr("against"), _fr("unknown"), _fr("neutral"), _fr("out_of_scope")],
    )
    assert counts == EvidenceCounts(supporting=2, against=1, unknown=1)


@pytest.mark.parametrize(
    ("counts", "strength"),
    [
        (EvidenceCounts(4, 0, 2), "strong"),
        (EvidenceCounts(4, 0, 3), "moderate"),
        (EvidenceCounts(4, 1, 0), "moderate"),
        (EvidenceCounts(3, 0, 0), "moderate"),
        (EvidenceCounts(2, 2, 0), "moderate"),
        (EvidenceCounts(2, 3, 0), "weak"),
        (EvidenceCounts(1, 0, 0), "weak"),
        (EvidenceCounts(0, 0, 6), "weak"),
    ],
)
def test_strength_at_the_profile_cutoffs(counts: EvidenceCounts, strength: str) -> None:
    assert _EVIDENCE.strong_min_supporting == 4
    assert _EVIDENCE.moderate_min_supporting == 2
    assert classify_strength(counts, _EVIDENCE) == strength


def test_render_guard_refuses_strong_with_three_or_more_unknowns() -> None:
    for unknown in (_EVIDENCE.max_unknown_for_strong + 1, 6):
        counts = EvidenceCounts(supporting=4, against=0, unknown=unknown)
        assert guard_strength("strong", counts, _EVIDENCE) == "moderate"
        assert strength_label("strong", counts, _EVIDENCE) != STRONG_LABEL
    weak_counts = EvidenceCounts(supporting=1, against=0, unknown=5)
    assert guard_strength("strong", weak_counts, _EVIDENCE) == "weak"
    allowed = EvidenceCounts(supporting=4, against=0, unknown=_EVIDENCE.max_unknown_for_strong)
    assert strength_label("strong", allowed, _EVIDENCE) == STRONG_LABEL == "Güçlü"


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _telemetry(**branches: str) -> dict[str, StageTelemetryView]:
    stages = {
        "dealer_gamma": "m21_dealer_gamma", "dark_pool": "m26_dark_pool",
        "sector": "m25_sector_peer", "price_confirmation": "m23_price_confirmation",
    }
    return {stages[f]: StageTelemetryView(stage_name=stages[f], branch=b, degraded=False) for f, b in branches.items()}


def test_source_print_is_the_largest_print_of_the_dominant_contract() -> None:
    small_dominant = _signal("d1", strike="760", premium="300000")
    big_dominant = _signal("d2", strike="760", premium="350000", second=5)
    lone_big = _signal("d3", strike="770", premium="500000", second=9)
    rows = _rows(
        _board_print(small_dominant, "d1", "at_ask"),
        _board_print(big_dominant, "d2", "at_ask"),
        _board_print(lone_big, "d3", "at_ask"),
    )
    (row,) = rows
    assert row.dominant.key.strike == Decimal(760)
    assert dominant_print(row).event_id == "d2"
    assert request_for(row) == EvidenceRequest(event_id="d2", ticker="SPY", trade_date=date(2026, 9, 15))


def test_row_evidence_for_a_bought_call_in_profile_order() -> None:
    sig = _signal("r1")
    (row,) = _rows(_board_print(sig, "r1", "at_ask"))
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=sig,
        telemetry=_telemetry(
            dealer_gamma="full_short_and_proximate", dark_pool="direction_unclear",
            sector="strong", price_confirmation="call_confirmed",
        ),
        tape=_tape(_DEADBAND + 1), ticker_info=None,
    )
    assert [f.family for f in evidence.families] == list(_EVIDENCE.counted_families)
    assert [f.state for f in evidence.families] == [
        "supporting", "supporting", "neutral", "supporting", "supporting", "unknown",
    ]
    assert evidence.counts == EvidenceCounts(supporting=4, against=0, unknown=1)
    assert evidence.strength == "strong"
    assert evidence.word == "4 lehte · 0 aleyhte · 1 bilinmiyor"
    assert (evidence.legacy, evidence.unread, evidence.source_event_id) == (False, False, "r1")


def test_row_evidence_for_a_sold_call_flips_stage_results_but_not_flow_or_gamma() -> None:
    sig = _signal("s1")
    (row,) = _rows(_board_print(sig, "s1", "at_bid"))
    assert row.direction == "down"
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=sig,
        telemetry=_telemetry(
            dealer_gamma="full_short_and_proximate", dark_pool="confirmed_match",
            sector="contrarian", price_confirmation="call_confirmed",
        ),
        tape=_tape(-(_DEADBAND + 1)),  # net put buying: the row's direction (down) dominates
        ticker_info=None,
    )
    states = {f.family: f.state for f in evidence.families}
    assert states == {
        "flow": "supporting",
        "dealer_gamma": "supporting",
        "dark_pool": "against",
        "sector": "supporting",
        "price_confirmation": "against",
        "open_interest": "unknown",
    }
    assert evidence.counts == EvidenceCounts(supporting=3, against=2, unknown=1)


def test_row_evidence_on_a_legacy_print_uses_only_the_legacy_table() -> None:
    sig = _signal("g1", price_confirmation_score=1.0, sector_confirmation_score=0.5, gamma_score=1.0)
    (row,) = _rows(_board_print(sig, "g1", None))
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=sig, telemetry=None,
        tape=None, ticker_info=None, legacy_scores=_LEGACY,
    )
    states = {f.family: f.state for f in evidence.families}
    assert states == {
        "flow": "unknown", "dealer_gamma": "unknown", "dark_pool": "unknown",
        "sector": "unknown", "price_confirmation": "supporting", "open_interest": "unknown",
    }
    assert evidence.legacy is True


def test_unread_evidence_is_unknown_everywhere() -> None:
    sig = _signal("u1")
    (row,) = _rows(_board_print(sig, "u1", "at_ask"))
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=sig, telemetry=_telemetry(sector="strong"),
        tape=_tape(_DEADBAND * 2), ticker_info=None, oi_state="opening", unread=True,
    )
    assert {f.state for f in evidence.families} == {"unknown"}
    assert evidence.counts == EvidenceCounts(supporting=0, against=0, unknown=6)
    assert evidence.unread is True


def test_removing_a_family_from_the_profile_removes_it_from_the_count() -> None:
    fewer = _SETTINGS.model_copy(
        update={"evidence": _EVIDENCE.model_copy(update={"counted_families": ("sector", "price_confirmation")})},
    )
    sig = _signal("f1")
    (row,) = _rows(_board_print(sig, "f1", "at_ask"))
    evidence = build_row_evidence(
        row, settings=fewer, now=_NOW, signal=sig,
        telemetry=_telemetry(sector="strong", price_confirmation="neutral"),
        tape=None, ticker_info=None,
    )
    assert [f.family for f in evidence.families] == ["sector", "price_confirmation"]
    assert evidence.counts == EvidenceCounts(supporting=1, against=0, unknown=0)


def test_sort_key_is_l_desc_a_asc_u_asc_premium_desc() -> None:
    keyed = {
        "more_support": evidence_sort_key(EvidenceCounts(3, 1, 2), Decimal(1)),
        "less_support": evidence_sort_key(EvidenceCounts(2, 0, 0), Decimal(10**9)),
        "fewer_against": evidence_sort_key(EvidenceCounts(3, 0, 5), Decimal(1)),
        "fewer_unknown": evidence_sort_key(EvidenceCounts(3, 0, 1), Decimal(1)),
        "bigger_premium": evidence_sort_key(EvidenceCounts(3, 0, 1), Decimal(2)),
    }
    assert sorted(keyed, key=keyed.__getitem__) == [
        "bigger_premium", "fewer_unknown", "fewer_against", "more_support", "less_support",
    ]


def test_trade_date_is_the_new_york_date() -> None:
    assert trade_date_et(datetime(2026, 9, 16, 2, 0, tzinfo=UTC)) == date(2026, 9, 15)
    assert trade_date_et(datetime(2026, 9, 15, 14, 0, tzinfo=UTC)) == date(2026, 9, 15)


# ---------------------------------------------------------------------------
# Database reader
# ---------------------------------------------------------------------------


def test_read_evidence_inputs_from_the_database(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'evidence.db'}")
    try:
        assert read_evidence_inputs(engine, _RUN, []) is EMPTY_INPUTS
        empty = read_evidence_inputs(engine, _RUN, [EvidenceRequest("e1", "SPY", _TS.date())])
        assert (dict(empty.telemetry), dict(empty.tapes), dict(empty.infos)) == ({}, {}, {})

        ensure_telemetry_tables(engine)
        with Session(engine) as session:
            session.add_all(
                [
                    AlfaStageTelemetry(
                        run_id=_RUN, event_id="e1", stage_name="m25_sector_peer", branch="no_sector",
                        metadata_json='{"branch": "no_sector", "peer_count_returned": "0"}',
                        degraded=False, profile_content_hash="h", written_at=_TS,
                    ),
                    AlfaStageTelemetry(
                        run_id=_RUN, event_id="e1", stage_name="m21_dealer_gamma", branch="no_data",
                        metadata_json=None, degraded=True, profile_content_hash="h", written_at=_TS,
                    ),
                    AlfaStageTelemetry(
                        run_id="other-run", event_id="e1", stage_name="m23_price_confirmation",
                        branch="neutral", metadata_json="not json", degraded=False,
                        profile_content_hash="h", written_at=_TS,
                    ),
                ],
            )
            session.commit()
        minute = TapeMinute(
            trade_date=_TS.date(), tape_time=_TS, net_call_premium=500_000.0, net_put_premium=-100_000.0,
            net_call_volume=10, net_put_volume=-5, call_volume=100, put_volume=50, net_delta=1.5,
        )
        upsert_tape(engine, "spy", [minute], fetched_at=_NOW)
        upsert_ticker_infos(engine, [TickerInfoSnapshot("SPY", "ETF", None)], fetched_at=_NOW)

        inputs = read_evidence_inputs(
            engine, _RUN, [EvidenceRequest("e1", "SPY", _TS.date()), EvidenceRequest("e9", "QQQ", _TS.date())],
        )
    finally:
        engine.dispose()
    assert set(inputs.telemetry) == {"e1"}
    sector = inputs.telemetry["e1"]["m25_sector_peer"]
    assert (sector.branch, sector.degraded) == ("no_sector", False)
    assert sector.metadata == {"branch": "no_sector", "peer_count_returned": "0"}
    assert inputs.telemetry["e1"]["m21_dealer_gamma"].degraded is True
    assert "m23_price_confirmation" not in inputs.telemetry["e1"]  # another run's row
    assert inputs.tapes[("SPY", _TS.date())].bullish_net_premium == 600_000.0
    assert inputs.infos["SPY"].fund_or_index is True
    assert stage_family("sector", sector, sold=False, ticker_info=inputs.infos["SPY"]).state == "out_of_scope"


# ---------------------------------------------------------------------------
# Copy (R-WD1)
# ---------------------------------------------------------------------------


def test_every_frozen_and_generated_evidence_string_is_clean() -> None:
    texts: list[str] = [*FAMILY_LABELS.values(), *STATE_LABELS.values(), *STRENGTH_LABELS.values()]
    texts.extend(
        NOTE_TEMPLATES[key].format(minutes=15, amount="+$400,000") for key in NOTE_TEMPLATES
    )
    texts.append(EVIDENCE_COPY["word"].format(supporting=4, against=1, unknown=1))
    for family, branches in BRANCH_STATES.items():
        for branch in (*branches, "no_sector", "unmapped", None):
            for sold in (False, True):
                for degraded in (False, True):
                    read = stage_family(family, _t(branch, degraded=degraded), sold=sold, ticker_info=_info("ETF"))
                    texts.append(read.text)
    for state in (None, "opening", "closing", "unconfirmed", "expires_before_t1"):
        texts.append(open_interest_family(state).text)  # type: ignore[arg-type]
    for net in (-1e6, 0.0, 1e6):
        for age in (0.0, _MAX_AGE * 2.0):
            texts.append(
                flow_family(_tape(net, age_seconds=age), "up", deadband_usd=_DEADBAND, max_age_seconds=_MAX_AGE, now=_NOW).text,
            )
    for text in texts:
        assert ensure_clean(text) == text


def test_dictionaries_are_read_only() -> None:
    for mapping in (FAMILY_LABELS, STATE_LABELS, NOTE_TEMPLATES, EVIDENCE_COPY, BRANCH_STATES):
        with pytest.raises(TypeError):
            mapping["flow"] = "x"  # type: ignore[index]
