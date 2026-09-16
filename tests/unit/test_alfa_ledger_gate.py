"""Phase 5.2.C32-fix3: the ledger aggregates only when EVERY group is ready.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2), in its own
words: "Nothing is aggregated unless each group has at least
``fills.min_n_for_stats`` cards with final outcomes. Below that, only counts are
shown (``12 pas, 3 log; istatistik için yetersiz örnek``)."

The page model used to open the gate when ANY group cleared it, so a taken
sample of twenty rendered a median beside a three-card passed sample — and the
shared counts line dropped its "yetersiz örnek" tail for both groups at the same
time, leaving the thin control group entirely unlabelled. The comparison between
log and pas is the only reason those numbers are shown at all, so the gate is
now the contract's: both groups, or neither.

Pins:
  - one full group alone unlocks nothing, in either direction, and the counts
    line keeps its tail while any group is short;
  - both groups at the gate report their own medians, and only then;
  - the boundary in both directions, one card either side;
  - an empty control group keeps the gate shut;
  - rendered: no ``Medyan fark`` and no group block while the gate is shut, both
    group blocks once it opens.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board.decision_ledger import build_ledger_page
from webapp.board.settings import load_board_settings

from tests.unit._alfa_card_harness import seed
from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_defter import _card, _outcome
from tests.unit.test_alfa_defter_routes import _bulk, _defter, _horizon, _reset

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from types import ModuleType

    from fastapi.testclient import TestClient
    from webapp.board.cards import DecisionCard
    from webapp.board.decision_ledger import HorizonSummary
    from webapp.board.outcomes import Outcome

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_MIN_N = _SETTINGS.fills.min_n_for_stats  # D8: profile-driven, never a literal
_HORIZONS = _SETTINGS.outcomes.horizons_trading_days
_LIMIT = _SETTINGS.ledger.max_cards_per_page


def _sample(decision: str, measured: int) -> tuple[list[DecisionCard], list[Outcome]]:
    """``measured`` cards of one decision, each with a computed outcome."""
    cards = [_card(card_id=f"{decision}-{i}", decision=decision) for i in range(measured)]
    outcomes = [
        _outcome(card.id, horizon_days=_HORIZONS[0], excess=1.5 + i)
        for i, card in enumerate(cards)
    ]
    return cards, outcomes


def _summary(*samples: tuple[str, int]) -> HorizonSummary:
    """The first horizon's summary for the given (decision, measured) samples."""
    cards: list[DecisionCard] = []
    outcomes: list[Outcome] = []
    for decision, measured in samples:
        built_cards, built_outcomes = _sample(decision, measured)
        cards.extend(built_cards)
        outcomes.extend(built_outcomes)
    page = build_ledger_page(
        cards, outcomes, horizons=_HORIZONS, min_n=_MIN_N, limit=_LIMIT,
    )
    return page.summaries[0]


def _decisions(groups: Sequence[object]) -> list[str]:
    return [group.decision for group in groups]  # type: ignore[attr-defined]


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType, str]]:
    url = f"sqlite:///{tmp_path / 'ledger-gate.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    import webapp.main as m

    _reset(m)
    seed(url)
    yield authed_client(m.app, monkeypatch), m, url
    _reset(m)


# ---------------------------------------------------------------------------
# The gate, in the page model
# ---------------------------------------------------------------------------


def test_one_full_group_beside_a_thin_one_unlocks_nothing() -> None:
    summary = _summary(("log", _MIN_N), ("pas", 3))
    assert summary.enough is False
    assert summary.reportable_groups == ()
    # The contract's own line shape, with the tail still attached.
    assert summary.counts_text == f"3 pas, {_MIN_N} log; istatistik için yetersiz örnek"


def test_the_gate_is_shut_whichever_group_is_short() -> None:
    """Symmetry: the passed cards do not get a privilege the taken cards lack."""
    for short, full in (("log", "pas"), ("pas", "log")):
        summary = _summary((short, 3), (full, _MIN_N))
        assert summary.enough is False, short
        assert summary.reportable_groups == (), short
        assert summary.counts_text.endswith("istatistik için yetersiz örnek"), short


def test_both_groups_at_the_gate_report_their_own_medians() -> None:
    summary = _summary(("log", _MIN_N), ("pas", _MIN_N))
    assert summary.enough is True
    assert _decisions(summary.reportable_groups) == _decisions(summary.groups)
    for group in summary.reportable_groups:
        assert group.stats.enough is True
        assert group.stats.median_pct is not None
        assert group.stats.q1_pct is not None and group.stats.q3_pct is not None
    # Above the gate the line is counts WITHOUT the "yetersiz örnek" tail.
    assert summary.counts_text == f"{_MIN_N} pas, {_MIN_N} log"


def test_the_boundary_holds_one_card_either_side() -> None:
    assert _summary(("log", _MIN_N), ("pas", _MIN_N - 1)).enough is False
    assert _summary(("log", _MIN_N), ("pas", _MIN_N)).enough is True
    assert _summary(("log", _MIN_N - 1), ("pas", _MIN_N)).enough is False


def test_an_empty_control_group_keeps_the_gate_shut() -> None:
    """A median with nothing to compare it against is the case the ledger exists to prevent."""
    summary = _summary(("pas", _MIN_N))
    assert summary.enough is False
    assert summary.reportable_groups == ()
    assert summary.counts_text == f"{_MIN_N} pas, 0 log; istatistik için yetersiz örnek"


# ---------------------------------------------------------------------------
# The gate, rendered on /defter
# ---------------------------------------------------------------------------


def test_a_thin_control_group_renders_no_median(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    _bulk(url, decision="log", n=_MIN_N, measured=_MIN_N)
    _bulk(url, decision="pas", n=3, measured=3)
    summary = _horizon(_defter(client), _HORIZONS[0])
    assert f"3 pas, {_MIN_N} log; istatistik için yetersiz örnek" in summary
    assert "Medyan fark" not in summary
    assert "Çeyrekler arası aralık" not in summary
    assert "data-group=" not in summary


def test_when_both_groups_clear_the_gate_both_render(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    _bulk(url, decision="log", n=_MIN_N, measured=_MIN_N)
    _bulk(url, decision="pas", n=_MIN_N, measured=_MIN_N)
    summary = _horizon(_defter(client), _HORIZONS[0])
    assert f"{_MIN_N} pas, {_MIN_N} log" in summary
    assert "istatistik için yetersiz örnek" not in summary
    assert "Medyan fark" in summary
    assert 'data-group="log"' in summary
    assert 'data-group="pas"' in summary
    # Still no significance claim at any sample size (contract §4).
    for banned in ("t=", "t-stat", "isabet", "kazanma", "olasılık"):
        assert banned not in summary, banned
