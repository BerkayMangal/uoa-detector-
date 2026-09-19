"""Phase 5.2.B1: position size per row (``webapp/board/sizing.py``). Pure unit tests.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B1 ("Cell", "Risk
bucket line", "Disclosure", "Unknowns"), §4.3 (owner-only values) and §2 R-WD1;
decision P15.

Pins:
  - the bucket mapping equals ``RiskSizer``'s, for every ``SignalLabel``;
  - ``lots = floor(risk / (ask x 100 + commission))`` at the exact boundary,
    just below it, and at zero;
  - the risk-bucket line is the contract's wording, byte for byte;
  - 0 lots renders its own frozen note and is never hidden;
  - a null, crossed, zero-ask or stale quote reads ``bilinmiyor`` while the
    chip's quote age stays available;
  - an unreadable source print reads ``bilinmiyor``, never a made-up bucket;
  - ``(varsayılan değer)`` only while the owner values are unconfirmed;
  - the max_r disclosure names the label and points at the audit block;
  - every frozen and generated string passes ``ensure_clean``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webapp.board.alfa_page import load_spread_cutoff_pct
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings
from webapp.board.sizing import (
    LABEL_BUCKETS,
    SIZE_COPY,
    SizeRead,
    bucket_for_label,
    build_size,
    lots_for,
    size_text,
)
from webapp.board.tradability import QuoteView, TradabilityRead, assess_tradability, executable_ask

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize
from uoa_detector.risk.sizer import RiskSizer

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_COMMISSION = _SETTINGS.cost.commission_per_contract_usd


def _chip(
    bid: float | None,
    ask: float | None,
    *,
    age_seconds: int = 41,
    settings: BoardSettings = _SETTINGS,
) -> TradabilityRead:
    quote = (
        None
        if bid is None or ask is None
        else QuoteView(
            option_symbol="AAA260918C00100000", nbbo_bid=bid, nbbo_ask=ask, volume=500,
            last_tape_time=None, fetched_at=_NOW - timedelta(seconds=age_seconds), returned=True,
        )
    )
    return assess_tradability(
        "AAA", quote, None, tradability=settings.tradability, spread_cutoff_pct=_CUTOFF,
        cost=settings.cost, sizing=settings.sizing, now=_NOW,
    )


def _signal(*, label: SignalLabel = SignalLabel.STANDARD_UOA, max_r: float = 0.5) -> StoredSignal:
    return BacktestStore().add(
        EnrichedEvent(print=build_print(event_id="s1", ticker="AAA", strike="100", dte=3)),
        LabelDecision(label=label, reason="test"),
        PositionSize(bucket=bucket_for_label(label), max_r=max_r),
    )


def _size(
    *,
    bid: float | None = 0.39,
    ask: float | None = 0.41,
    age_seconds: int = 41,
    signal: StoredSignal | None = None,
    settings: BoardSettings = _SETTINGS,
) -> SizeRead:
    return build_size(
        signal=signal if signal is not None else _signal(),
        chip=_chip(bid, ask, age_seconds=age_seconds, settings=settings),
        sizing=settings.sizing,
        cost=settings.cost,
    )


# ---------------------------------------------------------------------------
# Bucket mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", list(SignalLabel))
def test_bucket_mapping_equals_the_risk_sizer(label: SignalLabel) -> None:
    """The board names the bucket without a profile; only max_r is a profile number."""
    sizer = RiskSizer(load_default_profile())
    assert bucket_for_label(label) is sizer.size_for(label).bucket


def test_every_label_is_mapped() -> None:
    assert set(LABEL_BUCKETS) == set(SignalLabel)


# ---------------------------------------------------------------------------
# Lot arithmetic
# ---------------------------------------------------------------------------


def test_lots_is_floor_of_risk_over_one_lot_plus_commission() -> None:
    per_lot = 1.0 * 100 + _COMMISSION  # 100.65
    assert lots_for(per_lot, 1.0, _COMMISSION) == 1  # exactly one lot fits
    assert lots_for(per_lot - 0.01, 1.0, _COMMISSION) == 0  # a cent short
    assert lots_for(per_lot * 3, 1.0, _COMMISSION) == 3
    assert lots_for(per_lot * 3 - 0.01, 1.0, _COMMISSION) == 2


def test_lots_is_unknown_without_a_risk_amount_or_an_ask() -> None:
    assert lots_for(None, 1.0, _COMMISSION) is None
    assert lots_for(50.0, None, _COMMISSION) is None
    assert lots_for(50.0, 0.0, _COMMISSION) is None
    assert lots_for(-50.0, 1.0, _COMMISSION) is None
    assert lots_for(float("nan"), 1.0, _COMMISSION) is None


def test_one_lot_is_ask_times_one_hundred_and_its_share_of_capital() -> None:
    read = _size()
    assert read.ask == 0.41
    assert read.lot_cost_usd == pytest.approx(41.0)
    assert read.lot_pct_capital == pytest.approx(41.0 / _SETTINGS.sizing.capital_usd * 100)
    assert read.risk_usd == pytest.approx(0.5 * _SETTINGS.sizing.r_usd)
    assert read.lots == 1  # floor(50 / (41 + 0.65))
    assert size_text(read).lot == "1 lot $41 · sermayenin %0.41 kadarı"


# ---------------------------------------------------------------------------
# The risk-bucket line (contract wording)
# ---------------------------------------------------------------------------


def test_risk_bucket_line_matches_the_contract_wording() -> None:
    assert size_text(_size()).risk_bucket == (
        "Profil risk kovası: STANDARD_UOA, max_r 0.5 → 0.5 × R = $50 → 1 lot"  # noqa: RUF001
    )


def test_zero_lots_is_rendered_with_its_own_note() -> None:
    read = _size(bid=1.95, ask=2.05)  # one lot is $205.65, the risk amount is $50
    assert read.lots == 0
    assert read.no_lots is True
    text = size_text(read)
    assert text.risk_bucket.endswith("→ 0 lot")
    assert text.zero_note == "1 lot risk tutarını aşıyor: 0 lot"


def test_a_zero_max_r_label_sizes_to_zero_lots() -> None:
    read = _size(signal=_signal(label=SignalLabel.IGNORE_NOISE, max_r=0.0))
    assert (read.bucket, read.max_r, read.risk_usd, read.lots) == ("DISCARD", 0.0, 0.0, 0)
    assert size_text(read).risk_bucket == (
        "Profil risk kovası: DISCARD, max_r 0 → 0 × R = $0 → 0 lot"  # noqa: RUF001
    )


# ---------------------------------------------------------------------------
# Unknowns (contract §6 B1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bid", "ask", "age_seconds", "why"),
    [
        (None, None, 41, "no quote row"),
        (1.10, 1.00, 41, "crossed"),
        (0.0, 0.0, 41, "zero ask"),
        (0.39, 0.41, _SETTINGS.tradability.max_quote_age_seconds + 1, "stale"),
    ],
)
def test_an_unexecutable_quote_gives_no_size(bid: float | None, ask: float | None, age_seconds: int, why: str) -> None:
    chip = _chip(bid, ask, age_seconds=age_seconds)
    assert executable_ask(chip) is None, why
    read = _size(bid=bid, ask=ask, age_seconds=age_seconds)
    assert (read.ask, read.lot_cost_usd, read.lot_pct_capital, read.lots) == (None, None, None, None), why
    text = size_text(read)
    assert text.lot == "1 lot: bilinmiyor (işlem yapılabilir kotasyon yok)", why
    assert text.risk_bucket == (
        "Profil risk kovası: STANDARD_UOA, max_r 0.5 → 0.5 × R = $50 → lot sayısı bilinmiyor"  # noqa: RUF001
    ), why
    assert text.zero_note is None, why


def test_a_stale_quote_keeps_its_age_visible() -> None:
    stale = _chip(0.39, 0.41, age_seconds=_SETTINGS.tradability.max_quote_age_seconds + 59)
    assert stale.state == "no_quote"
    assert stale.quote_age_seconds == _SETTINGS.tradability.max_quote_age_seconds + 59


def test_an_unreadable_source_print_has_no_bucket() -> None:
    read = build_size(signal=None, chip=_chip(0.39, 0.41), sizing=_SETTINGS.sizing, cost=_SETTINGS.cost)
    assert (read.bucket, read.max_r, read.risk_usd, read.lots) == (None, None, None, None)
    text = size_text(read)
    assert text.risk_bucket == "Profil risk kovası: bilinmiyor (kaynak baskının kaydı okunamadı)"
    assert text.audit_note == (
        "max_r satırın etiketinden gelir; etiketi birleşik skor belirler (bu satırın kaydı okunamadı)."
    )
    assert text.lot == "1 lot $41 · sermayenin %0.41 kadarı"  # the lot itself is still known


# ---------------------------------------------------------------------------
# Disclosure (§4.3, decision P15; R-EV2)
# ---------------------------------------------------------------------------


def test_default_marker_follows_the_owner_confirmation() -> None:
    """5.3.5: the profile now carries O3's confirmation, so the marker is absent by default.

    Inverted rather than halved. Asserting only the confirmed side would leave the
    disclosure itself untested, and the disclosure is what stops an invented capital
    figure from being read as the owner's own.
    """
    assert size_text(_size()).default_marker is None
    unconfirmed = _SETTINGS.model_copy(
        update={"sizing": _SETTINGS.sizing.model_copy(update={"values_confirmed_by_owner": False})},
    )
    assert size_text(_size(settings=unconfirmed)).default_marker == "(varsayılan değer)"


def test_audit_note_discloses_where_max_r_comes_from() -> None:
    assert size_text(_size()).audit_note == (
        "max_r satırın STANDARD_UOA etiketinden gelir; etiketi birleşik skor belirler (skor bu blokta)."
    )


def test_the_size_cell_states_no_probability_and_no_outcome() -> None:
    texts = [*SIZE_COPY.values()]
    for read in (_size(), _size(bid=None, ask=None), _size(bid=1.95, ask=2.05)):
        text = size_text(read)
        texts.extend(
            t for t in (text.title, text.lot, text.risk_bucket, text.zero_note, text.audit_note,
                        text.default_marker) if t is not None
        )
    joined = " ".join(texts).lower()
    for banned in ("olasılık", "beklenen", "kâr", "kazanç"):
        assert banned not in joined, banned


def test_every_frozen_and_generated_string_is_clean() -> None:
    texts = [*SIZE_COPY.values()]
    for label in SignalLabel:
        for bid, ask in ((0.39, 0.41), (None, None), (1.95, 2.05), (0.0, 0.0), (1.10, 1.00)):
            read = _size(bid=bid, ask=ask, signal=_signal(label=label, max_r=0.5))
            text = size_text(read)
            texts.extend(
                t for t in (text.title, text.lot, text.risk_bucket, text.zero_note,
                            text.audit_note, text.default_marker) if t is not None
            )
    for text in texts:
        assert ensure_clean(text) == text


def test_the_copy_dictionary_is_read_only() -> None:
    with pytest.raises(TypeError):
        SIZE_COPY["lot"] = "x"  # type: ignore[index]
