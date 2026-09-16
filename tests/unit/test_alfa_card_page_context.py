"""Phase 5.2.C1-fix3: the card freezes the page around the row (review C1-R3).

Contract §3 lists "the regime sentence and tripwires" among the things the
frozen view must hold. On the parallel FAZ B branch those are **page-level**
(``AlfaPage.regime``), not fields of ``AlfaRowView`` — so the recursive row
encoder, which future-proofs every row-level field FAZ B adds, cannot reach
them. Without this fix, the day FAZ B merges every card written would silently
omit the regime sentence, its tripwires, the capital header and the single-bet
clusters; cards are append-only, so they could never be backfilled.

``cards.page_context`` freezes everything the page model carries except the row
containers (the row this card is about is already frozen under ``row_view``,
and the board's other rows are not this decision). It names no field, so
whatever a later branch puts on the page is captured the day it lands.

Pins:
  - ``page_context`` carries every ``AlfaPage`` field but the row containers,
    computed from the dataclass itself, so a field added later must appear;
  - a field a subclass adds — a regime sentence with its tripwires, FAZ B's own
    shape — is frozen with no change to ``webapp/board/cards.py``, and that
    module's source names neither;
  - the card the route writes carries the page the owner was looking at,
    including the cost-gate state the press was made under;
  - the frozen row view is unchanged (the C1b pin still holds).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board import alfa_page, cards

from tests.unit._alfa_card_harness import open_board, press

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from fastapi.testclient import TestClient

_ROW_CONTAINERS = {"rows", "views", "sections"}
_CARDS_SOURCE = Path(cards.__file__)


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    yield from open_board(tmp_path, monkeypatch, name="page-context.db")


@dataclass(frozen=True)
class _Tripwire:
    text: str
    armed: bool


@dataclass(frozen=True)
class _PageWithRegime(alfa_page.AlfaPage):
    """FAZ B's shape: the regime band and its tripwires hang off the page, not the row."""

    regime_sentence: str = ""
    tripwires: tuple[_Tripwire, ...] = ()


def _page(**overrides: object) -> alfa_page.AlfaPage:
    fields: dict[str, object] = {
        "rows": (),
        "views": (),
        "sections": (),
        "print_count": 2,
        "load_failed": False,
        "gate_on": False,
        "session_date": date(2026, 9, 16),
        "today": date(2026, 9, 16),
        "rendered_at": datetime(2026, 9, 16, 14, 5, tzinfo=UTC),
    }
    fields.update(overrides)
    return alfa_page.AlfaPage(**fields)  # type: ignore[arg-type]


def _page_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(alfa_page.AlfaPage)} - _ROW_CONTAINERS


def test_page_context_holds_every_page_field_but_the_row_containers() -> None:
    frozen = cards.page_context(_page())
    assert isinstance(frozen, dict)
    # From the dataclass, not a list written here: a field added later must appear.
    assert set(frozen) == _page_fields()
    assert _ROW_CONTAINERS.isdisjoint(frozen)
    assert frozen["gate_on"] is False
    assert frozen["print_count"] == 2
    assert frozen["load_failed"] is False
    assert frozen["session_date"] == "2026-09-16"
    assert frozen["rendered_at"] == "2026-09-16T14:05:00+00:00"


def test_a_page_field_a_later_branch_adds_is_frozen_with_no_change_here() -> None:
    page = _PageWithRegime(
        rows=(), views=(), sections=(), print_count=1, load_failed=False,
        regime_sentence="Endeks opsiyonlarında dealer gamması pozitif.",
        tripwires=(_Tripwire(text="VIX 20 üstü", armed=True),),
    )
    frozen = cards.page_context(page)
    assert isinstance(frozen, dict)

    assert frozen["regime_sentence"] == "Endeks opsiyonlarında dealer gamması pozitif."
    assert frozen["tripwires"] == [{"text": "VIX 20 üstü", "armed": True}]
    assert set(frozen) == _page_fields() | {"regime_sentence", "tripwires"}

    # The proof that it is generic: the module froze fields it does not name.
    source = _CARDS_SOURCE.read_text(encoding="utf-8")
    assert "regime_sentence" not in source
    assert "tripwires" not in source


def test_the_card_freezes_the_page_the_press_was_made_on(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert press(client, "pas", gate="off").status_code == 303

    (card,) = m._card_repo().list_cards()
    doc = card.card
    assert isinstance(doc, dict)
    frozen = doc["page_context"]
    assert isinstance(frozen, dict)

    assert set(frozen) == _page_fields()
    assert frozen["gate_on"] is False  # the owner had the cost gate off when pressing
    assert frozen["load_failed"] is False
    assert frozen["print_count"] == 3  # the seeded run's three prints
    assert frozen["session_date"] == "2026-09-16"
    assert frozen["today"] == "2026-09-16"

    # It is the page the server rebuilt, whole — not a chosen handful of values.
    prints = m._board_reader().load_run("live-2026-09-16")
    rebuilt = m._build_board_page(prints, gate_on=False, run_latest_ts=None)
    assert frozen == cards.page_context(rebuilt)

    # And the row snapshot still equals the rebuilt row view (the C1b pin).
    view = next(v for v in rebuilt.views if v.row.ticker == "SPY" and v.row.direction == "up")
    assert doc["row_view"] == cards.snapshot(view)


def test_the_gate_state_the_card_records_follows_the_press(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert press(client, "pas").status_code == 303  # the gate is on unless ?gate=off

    (card,) = m._card_repo().list_cards()
    doc = card.card
    assert isinstance(doc, dict)
    frozen = doc["page_context"]
    assert isinstance(frozen, dict)
    assert frozen["gate_on"] is True
