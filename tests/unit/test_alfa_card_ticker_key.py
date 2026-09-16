"""Phase 5.2.C1-fix5: a card is always findable under its own ticker (review C1-R5).

``list_cards`` upper-cased the ticker filter while ``write_card`` stored the
row's ticker verbatim, so a card written for a ticker that was not already
upper-case counted in the unfiltered list but was invisible to the filter — the
C2 pass ledger would then show a pass in its totals and not in the ticker view,
which is the one comparison the ledger exists for. Row tickers are never
normalised on their way into a ``BoardRow``; the board already has to compensate
for that elsewhere (``alfa_page`` looks delayed panels up under
``ticker.strip().upper()``).

The ticker is now normalised once, on write, and the filter normalises the same
way.

Pins:
  - a card written from a lower-case or padded ticker is stored canonically and
    is returned by the filter, however the filter is spelled;
  - no card can be invisible to a filter naming its own stored ticker;
  - the board's own press still records the row's ticker unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from webapp.board.cards import CardRepo
from webapp.board.db import make_engine

from tests.unit._alfa_card_harness import RUN, open_board, press

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    yield from open_board(tmp_path, monkeypatch, name="ticker-key.db")


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[CardRepo]:
    engine = make_engine(f"sqlite:///{tmp_path / 'ticker-key-repo.db'}")
    yield CardRepo(engine)
    engine.dispose()


def _write(repo: CardRepo, ticker: str) -> str:
    return repo.write_card(
        decision="pas",
        ticker=ticker,
        direction="yukarı",
        run_id=RUN,
        dominant_option_symbol=None,
        card={"row_view": {"ticker": ticker}},
        board_profile_hash="board-hash",
        calibration_profile_hash=None,
    )


@pytest.mark.parametrize("written", ["spy", " spy ", "Spy", "SPY"])
def test_a_card_is_found_however_its_ticker_was_spelled(repo: CardRepo, written: str) -> None:
    card_id = _write(repo, written)
    stored = repo.get_card(card_id)
    assert stored is not None
    assert stored.ticker == "SPY"

    assert len(repo.list_cards()) == 1
    for spelling in ("spy", "SPY", " spy ", "Spy"):
        found = repo.list_cards(ticker=spelling)
        assert [c.id for c in found] == [card_id], spelling


def test_no_card_is_invisible_to_a_filter_naming_its_own_ticker(repo: CardRepo) -> None:
    for ticker in ("spy", "SPY", " nvda", "NVDA "):
        _write(repo, ticker)
    every = repo.list_cards()
    assert len(every) == 4

    for card in every:
        assert card.id in {c.id for c in repo.list_cards(ticker=card.ticker)}
    # And the totals still split by ticker without losing one.
    assert len(repo.list_cards(ticker="SPY")) + len(repo.list_cards(ticker="NVDA")) == len(every)


def test_the_board_s_own_press_records_the_row_ticker(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    assert press(client, "pas", ticker="SPY").status_code == 303

    (card,) = m._card_repo().list_cards()
    assert card.ticker == "SPY"
    assert [c.id for c in m._card_repo().list_cards(ticker="spy")] == [card.id]
