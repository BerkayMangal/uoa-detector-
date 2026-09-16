"""Phase 5.2.C1-fix1: a passed card can never acquire a trade id (review C1-R1).

``CardRepo.link_trade`` is the single write onto a written card the contract
allows, and §3 puts it inside the **Log flow**: "the ``trade_id`` link is set
once, on a card that has none". Before this fix it never read the card's
decision, so a ``pas`` card could be turned into one carrying a trade id — and
the board itself puts that card's id in the address bar after "Pas geç"
(``/?run=..&pas=<card_id>``), from where it reaches ``/journal/new?card_id=``
and the journal POST. The table is append-only and the repo has no repair path,
so the damage would be permanent, and it would corrupt the log-vs-pas
comparison the pass ledger (§4) exists to make.

Pins:
  - the repo refuses to link a ``pas`` card and still links a ``log`` card;
  - a journal save quoting a pas card's id keeps the trade and leaves the card
    exactly as written — decision, trade_id, snapshot and timestamp;
  - ``is_linkable`` answers the same question ``link_trade`` acts on, so the
    journal form cannot promise a link the write would refuse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from webapp.board.cards import CardRepo, is_linkable
from webapp.board.db import make_engine

from tests.unit._alfa_card_harness import (
    RUN,
    card_id_from,
    open_board,
    press,
    trade_form,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    yield from open_board(tmp_path, monkeypatch, name="link-guard.db")


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[CardRepo]:
    engine = make_engine(f"sqlite:///{tmp_path / 'link-guard-repo.db'}")
    yield CardRepo(engine)
    engine.dispose()


def _write(repo: CardRepo, decision: str) -> str:
    return repo.write_card(
        decision=decision,  # type: ignore[arg-type]
        ticker="SPY",
        direction="yukarı",
        run_id=RUN,
        dominant_option_symbol="SPY260918C00760000",
        card={"row_view": {"ticker": "SPY"}},
        board_profile_hash="board-hash",
        calibration_profile_hash="calib-hash",
    )


def test_the_repo_refuses_to_link_a_pas_card(repo: CardRepo) -> None:
    passed = _write(repo, "pas")
    before = repo.get_card(passed)
    assert before is not None

    assert repo.link_trade(passed, "trade-1") is False
    after = repo.get_card(passed)
    assert after == before  # decision, trade_id, snapshot and created_at all unchanged
    assert after is not None
    assert after.trade_id is None

    # The Log flow is untouched: a log card still links, once.
    logged = _write(repo, "log")
    assert repo.link_trade(logged, "trade-1") is True
    linked = repo.get_card(logged)
    assert linked is not None
    assert linked.trade_id == "trade-1"


def test_is_linkable_answers_what_link_trade_does(repo: CardRepo) -> None:
    passed = repo.get_card(_write(repo, "pas"))
    logged_id = _write(repo, "log")
    logged = repo.get_card(logged_id)
    assert passed is not None
    assert logged is not None

    assert is_linkable(logged) is True
    assert is_linkable(passed) is False
    assert repo.link_trade(logged_id, "trade-1") is True

    # Once linked, neither the check nor the write will do it again.
    relinked = repo.get_card(logged_id)
    assert relinked is not None
    assert is_linkable(relinked) is False
    assert repo.link_trade(logged_id, "trade-2") is False


def test_a_journal_save_quoting_a_pas_card_leaves_it_untouched(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = card_id_from(press(client, "pas").headers["location"], "pas")
    before = m._card_repo().get_card(card_id)
    assert before is not None
    assert before.decision == "pas"

    # The pas card's id is in the owner's address bar; the journal form takes any id.
    response = client.post("/journal", data=trade_form(card_id=card_id), follow_redirects=False)
    assert response.status_code == 303
    assert len(m._journal().list()) == 1  # the trade is never the casualty

    after = m._card_repo().get_card(card_id)
    assert after == before
    assert after is not None
    assert after.decision == "pas"
    assert after.trade_id is None
