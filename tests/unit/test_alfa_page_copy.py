"""Phase 5.2.A1: the ``/alfa`` page copy is frozen and clean (rule R-WD1).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-WD1, §5 A1.

Pins:
  - every frozen string the A1 page can show passes ``ensure_clean``;
  - every generated string (summary, side counts, detail summary, fill-side
    labels), formatted from real rows, passes ``ensure_clean``;
  - the dictionaries are read-only;
  - a failed read and an empty run are distinct page states.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webapp.board.aggregate import POSITION_READ_LABELS
from webapp.board.alfa_page import (
    ALFA_COPY,
    FILL_SIDE_LABELS,
    OPTION_TYPE_LABELS,
    build_alfa_page,
    detail_summary_text,
    fill_side_label,
    side_counts_text,
    template_context,
)
from webapp.board.direction import DIRECTION_LABELS, FALLBACK_MARKER
from webapp.board.honesty import ensure_clean
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_SETTINGS = load_board_settings(Path(__file__).resolve().parents[2] / "profiles" / "board_v1.yaml")
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)


def _prints() -> list[BoardPrint]:
    out: list[BoardPrint] = []
    sides = ["at_ask", "at_bid", "midpoint", None, "above_ask", "below_bid", "unknown"]
    for i, side in enumerate(sides):
        pr = build_print(
            event_id=f"c{i}", ts=_TS + timedelta(minutes=i), ticker="SPY",
            option_type="call" if i % 2 else "put", strike=str(700 + i), dte=3,
        )
        sig = BacktestStore().add(
            EnrichedEvent(print=pr),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
        meta = None if side is None else PrintMetaView(fill_side=side, option_chain=None)
        out.append(BoardPrint(run_id="live-2026-09-15", event_id=f"c{i}", signal=sig, meta=meta))
    return out


def test_every_frozen_string_is_clean() -> None:
    frozen = [
        *ALFA_COPY.values(),
        *FILL_SIDE_LABELS.values(),
        *OPTION_TYPE_LABELS.values(),
        *POSITION_READ_LABELS.values(),
        *DIRECTION_LABELS.values(),
        FALLBACK_MARKER,
    ]
    for text in frozen:
        assert ensure_clean(text) == text


def test_every_generated_string_is_clean() -> None:
    page = build_alfa_page(_prints(), _SETTINGS)
    assert page.rows
    generated = [page.summary]
    for row in page.rows:
        generated.extend([side_counts_text(row), detail_summary_text(row)])
        generated.extend(fill_side_label(p.fill_side) for p in row.prints)
    generated.append(fill_side_label("a-side-we-have-never-seen"))
    for text in generated:
        assert ensure_clean(text) == text


def test_dictionaries_are_read_only() -> None:
    for mapping in (ALFA_COPY, FILL_SIDE_LABELS, OPTION_TYPE_LABELS):
        with pytest.raises(TypeError):
            mapping["page_title"] = "x"  # type: ignore[index]


def test_failed_read_and_empty_run_are_distinct_states() -> None:
    failed = build_alfa_page(None, _SETTINGS)
    empty = build_alfa_page([], _SETTINGS)
    assert failed.load_failed is True
    assert empty.load_failed is False
    assert failed.rows == empty.rows == ()


def test_template_context_exposes_the_frozen_dictionaries() -> None:
    ctx = template_context()
    assert ctx["copy"] is ALFA_COPY
    assert ctx["direction_labels"] is DIRECTION_LABELS
    assert ctx["position_labels"] is POSITION_READ_LABELS
    assert ctx["fallback_marker"] == FALLBACK_MARKER
