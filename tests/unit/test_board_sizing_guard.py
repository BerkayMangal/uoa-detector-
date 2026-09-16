"""Phase 5.2.B-fix10: an unmapped label reads bilinmiyor instead of breaking the board.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B1 ("Unknowns. A null
ask gives bilinmiyor") and §2 R-UN1; D11 (no rot).

Review findings FB-07 and FB-08:
  - ``sizing.bucket_for_label`` raises ``KeyError`` for a label missing from
    LABEL_BUCKETS and ``build_size`` called it unguarded, so adding a member to
    ``SignalLabel`` later would turn the whole board into a 500 - while the
    mapping it mirrors, ``RiskSizer``, defaults an unmapped label to DISCARD;
  - ``alfa_page.REGIME_CHIP_KEYS`` was defined and never read, so a reader would
    assume it drives the regime chips or their ages. Neither is true.

Pins:
  - ``bucket_for_label`` stays strict, so the parity test keeps its teeth;
  - ``build_size`` degrades to the unknown-bucket reading the module already
    uses for an unreadable source print;
  - the dead constant is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from webapp.board import alfa_page, sizing
from webapp.board.alfa_page import load_spread_cutoff_pct
from webapp.board.settings import load_board_settings
from webapp.board.tradability import QuoteView, TradabilityRead, assess_tradability

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)


def _chip() -> TradabilityRead:
    quote = QuoteView(
        option_symbol="AAA260918C00100000", nbbo_bid=0.39, nbbo_ask=0.41, volume=500,
        last_tape_time=None, fetched_at=_NOW - timedelta(seconds=41), returned=True,
    )
    return assess_tradability(
        "AAA", quote, None, tradability=_SETTINGS.tradability, spread_cutoff_pct=_CUTOFF,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=_NOW,
    )


def _signal(label: SignalLabel) -> StoredSignal:
    return BacktestStore().add(
        EnrichedEvent(
            print=build_print(
                event_id="s1", ts=_NOW - timedelta(hours=1), ticker="AAA", option_type="call",
                strike="100", dte=3, option_price="1.50",
            ),
        ),
        LabelDecision(label=label, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def test_bucket_for_label_stays_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    trimmed = dict(sizing.LABEL_BUCKETS)
    trimmed.pop(SignalLabel.SWEEP_UOA)
    monkeypatch.setattr(sizing, "LABEL_BUCKETS", MappingProxyType(trimmed))
    with pytest.raises(KeyError):
        sizing.bucket_for_label(SignalLabel.SWEEP_UOA)


def test_an_unmapped_label_reads_unknown_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trimmed = dict(sizing.LABEL_BUCKETS)
    trimmed.pop(SignalLabel.SWEEP_UOA)
    monkeypatch.setattr(sizing, "LABEL_BUCKETS", MappingProxyType(trimmed))
    read = sizing.build_size(
        signal=_signal(SignalLabel.SWEEP_UOA), chip=_chip(),
        sizing=_SETTINGS.sizing, cost=_SETTINGS.cost,
    )
    assert read.bucket is None
    assert read.lot_cost_usd is not None  # the lot itself is still priced from the ask
    text = sizing.size_text(read)
    assert text.risk_bucket == "Profil risk kovası: bilinmiyor (kaynak baskının kaydı okunamadı)"
    assert text.audit_note.startswith("max_r satırın etiketinden gelir")


def test_a_mapped_label_is_unchanged() -> None:
    read = sizing.build_size(
        signal=_signal(SignalLabel.STANDARD_UOA), chip=_chip(),
        sizing=_SETTINGS.sizing, cost=_SETTINGS.cost,
    )
    assert read.bucket == RiskBucket.STANDARD_UOA.value
    assert sizing.size_text(read).risk_bucket.startswith("Profil risk kovası: STANDARD_UOA")


def test_the_dead_regime_chip_key_constant_is_gone() -> None:
    # D11: it had no consumer, and a reader would assume it drove the chips or their ages.
    assert not hasattr(alfa_page, "REGIME_CHIP_KEYS")
