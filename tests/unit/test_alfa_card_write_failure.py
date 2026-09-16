"""Phase 5.2.C1-fix4: a card that could not be written never claims otherwise (review C1-R4).

When the card write raised, the press landed on the app's generic error page,
which says "Nothing is lost. Refresh in a moment." That page was written for
read-only views, where it is true. On a decision it is the worst sentence the
board can produce: the pass was **not** recorded, the whole phase exists because
an unrecorded pass is lost forever, and the copy actively tells the owner not to
press again.

So the card POST answers a failed write itself, with a frozen line that says
nothing was recorded. The read-only pages keep the generic page, unchanged.

Pins:
  - a failed write answers 500 with ``CARD_COPY["write_failed"]``, does not say
    "Nothing is lost", and leaves no card behind;
  - the answer leaks no database URL, credential or traceback;
  - the line is frozen copy and passes the honesty guard;
  - a read-only page that fails still gets the generic page, so this fix stayed
    where it belongs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from webapp.board import cards
from webapp.board.honesty import ensure_clean, forbidden_words

from tests.unit._alfa_card_harness import ORIGIN, RUN, open_board
from tests.unit._webapp_auth import set_web_auth

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

_PRESS = {"decision": "pas", "card_run_id": RUN, "card_ticker": "SPY", "card_direction": "up"}


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    yield from open_board(tmp_path, monkeypatch, name="write-failure.db")


def _boom(*_args: object, **_kwargs: object) -> str:
    """Fail the way a database does: with the connection string in the message."""
    msg = f"connection to {os.environ['DATABASE_URL']} failed: password=hunter2"
    raise RuntimeError(msg)


def _browser(m: ModuleType, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client that hands back the 500 page instead of re-raising, as a browser sees it."""
    return TestClient(m.app, headers=set_web_auth(monkeypatch), raise_server_exceptions=False)


def test_a_failed_write_says_nothing_was_recorded(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, m = board
    monkeypatch.setattr(cards.CardRepo, "write_card", _boom)

    response = _browser(m, monkeypatch).post(
        "/alfa/card", data=_PRESS, headers=ORIGIN, follow_redirects=False,
    )
    assert response.status_code == 500
    assert response.text == cards.CARD_COPY["write_failed"]
    # The read-only page's promise must never appear on a decision that failed.
    assert "Nothing is lost" not in response.text
    assert m._card_repo().list_cards() == ()


def test_the_failure_answer_leaks_nothing(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _client, m = board
    monkeypatch.setattr(cards.CardRepo, "write_card", _boom)

    body = _browser(m, monkeypatch).post(
        "/alfa/card", data=_PRESS, headers=ORIGIN, follow_redirects=False,
    ).text
    for secret in ("hunter2", "sqlite:///", str(tmp_path), "Traceback", "RuntimeError", "password"):
        assert secret not in body, secret


def test_the_failure_line_is_frozen_and_clean() -> None:
    text = cards.CARD_COPY["write_failed"]
    assert ensure_clean(text) == text
    assert forbidden_words(text) == []
    # It is not a confirmation: it must not read like the recorded-pass line.
    assert text != cards.CARD_COPY["pas_recorded"]


def test_a_read_only_page_still_gets_the_generic_error_page(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix stays on the decision route: a failed GET keeps the page written for it."""
    _client, m = board

    def _fail(*_args: object, **_kwargs: object) -> object:
        msg = "board read failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(m, "_build_board_page", _fail)
    response = _browser(m, monkeypatch).get("/")
    assert response.status_code == 500
    assert "Temporarily unavailable" in response.text
    assert cards.CARD_COPY["write_failed"] not in response.text
