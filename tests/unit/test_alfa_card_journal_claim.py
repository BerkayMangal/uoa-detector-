"""Phase 5.2.C1-fix6: the journal form claims a link only when there is one (review C1-R6).

``/journal/new`` rendered "Linked to board decision card {id} — saving stores
this trade's id on that card" for any ``card_id`` in the query string, including
one naming no card, a passed card, or a card already linked. ``journal_create``
then swallowed the refused link through ``_safe``, so the owner was told a link
existed and was never told it had not happened. The board's own confirmation
path already does this correctly: ``?pas=`` renders only for a card that really
exists.

The sentence was also the one piece of generated board copy hardcoded in a
template; it now lives in the frozen dictionary with the rest and goes through
``honesty.ensure_clean``.

Pins:
  - a fresh ``log`` card is claimed, and the hidden field carries its id;
  - a made-up id, a ``pas`` card and an already-linked card are not claimed, and
    the hidden field is empty so a save cannot quote them;
  - the claim is the frozen line, formatted with the card id;
  - the form still renders with no card at all (the older flow is untouched).
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

import pytest
from webapp.board import cards
from webapp.board.honesty import ensure_clean

from tests.unit._alfa_card_harness import card_id_from, open_board, press, trade_form

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    yield from open_board(tmp_path, monkeypatch, name="journal-claim.db")


def _visible(body: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


def _claim(card_id: str) -> str:
    return cards.CARD_COPY["journal_link"].format(card_id=card_id)


def _form(client: TestClient, card_id: str) -> str:
    return client.get("/journal/new", params={"card_id": card_id}).text


def test_a_fresh_log_card_is_claimed_and_carried(board: tuple[TestClient, ModuleType]) -> None:
    client, _m = board
    card_id = card_id_from(press(client, "log").headers["location"], "card_id")

    body = _form(client, card_id)
    assert f'name="card_id" value="{card_id}"' in body
    assert f'data-card-link="{card_id}"' in body
    assert _claim(card_id) in _visible(body)


def test_a_card_that_cannot_take_a_link_is_not_claimed(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board

    # 1. An id naming no card at all.
    made_up = "b7b4af03db064537a143ab2c29a8b883"
    assert m._card_repo().get_card(made_up) is None
    bodies = {"made up": _form(client, made_up)}

    # 2. A passed card: link_trade refuses it, so the form must not promise one.
    passed = card_id_from(press(client, "pas").headers["location"], "pas")
    bodies["pas"] = _form(client, passed)

    # 3. A card already linked to a trade: saving again would not re-link it.
    linked = card_id_from(press(client, "log").headers["location"], "card_id")
    assert client.post(
        "/journal", data=trade_form(card_id=linked), follow_redirects=False,
    ).status_code == 303
    stored = m._card_repo().get_card(linked)
    assert stored is not None
    assert stored.trade_id is not None
    bodies["already linked"] = _form(client, linked)

    for case, body in bodies.items():
        assert "data-card-link" not in body, case
        assert 'name="card_id" value=""' in body, case  # a save cannot quote it
        assert "Linked to board decision card" not in _visible(body), case


def test_the_claim_is_frozen_copy_and_clean(board: tuple[TestClient, ModuleType]) -> None:
    client, _m = board
    text = cards.CARD_COPY["journal_link"]
    assert ensure_clean(text) == text

    card_id = card_id_from(press(client, "log").headers["location"], "card_id")
    rendered = _visible(_form(client, card_id))
    assert _claim(card_id) in rendered
    # Formatted from the frozen line, not written into the template.
    assert text.format(card_id=card_id) == _claim(card_id)


def test_the_form_still_renders_with_no_card(board: tuple[TestClient, ModuleType]) -> None:
    client, _m = board
    body = client.get("/journal/new").text
    assert "Log a trade" in body
    assert "data-card-link" not in body
    assert 'name="card_id" value=""' in body
