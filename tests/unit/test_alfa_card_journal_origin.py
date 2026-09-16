"""Phase 5.2.C1-fix2: a foreign origin never mutates an append-only card (review C1-R2).

Contract §3 hardens the card POSTs with an ``Origin``/``Referer`` check on top
of Basic auth. C1b then put a *second* write to ``alfa_decision_card`` behind
``POST /journal`` — the ``trade_id`` link — and that route has no such guard.
Browsers attach cached Basic credentials automatically, so a foreign page's
form POST was accepted and the card was mutated permanently.

The journal route keeps the shape the contract pins for it ("the journal route
and ``TradeRow`` are unchanged, apart from an optional hidden ``card_id`` form
field"): it does not *require* a header, so every existing client still saves
trades exactly as before. What is refused is the append-only mutation, when the
request declares another host — which is the shape a cross-site browser POST
always has, since browsers send ``Origin`` on every cross-site form POST.

Pins:
  - a foreign ``Origin``, a foreign ``Referer``, and a foreign ``Origin`` with a
    good ``Referer`` all leave the card unlinked;
  - a same-host ``Origin`` or ``Referer`` links it, and so does a request
    carrying neither header (curl, the owner's own tooling, the C1b tests);
  - the trade itself is never the casualty: it is saved in every case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.unit._alfa_card_harness import (
    ORIGIN,
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

_FOREIGN = [
    {"Origin": "https://evil.test"},
    {"Referer": "https://evil.test/x"},
    {"Origin": "https://evil.test", "Referer": "http://testserver/"},
]
_OWN = [
    ORIGIN,
    {"Referer": "http://testserver/journal/new"},
    {},  # neither header: not a browser cross-site POST, and the C1b flow keeps working
]


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    yield from open_board(tmp_path, monkeypatch, name="journal-origin.db")


def _logged_card(client: TestClient) -> str:
    return card_id_from(press(client, "log").headers["location"], "card_id")


@pytest.mark.parametrize("headers", _FOREIGN, ids=["origin", "referer", "origin over referer"])
def test_a_cross_site_journal_post_never_links_the_card(
    board: tuple[TestClient, ModuleType], headers: dict[str, str],
) -> None:
    client, m = board
    card_id = _logged_card(client)
    before = m._card_repo().get_card(card_id)
    assert before is not None

    response = client.post(
        "/journal", data=trade_form(card_id=card_id), headers=headers, follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(m._journal().list()) == 1  # the trade is saved; only the card write is refused

    after = m._card_repo().get_card(card_id)
    assert after == before
    assert after is not None
    assert after.trade_id is None


@pytest.mark.parametrize("headers", _OWN, ids=["origin", "referer", "neither header"])
def test_the_owner_s_own_save_still_links_the_card(
    board: tuple[TestClient, ModuleType], headers: dict[str, str],
) -> None:
    client, m = board
    card_id = _logged_card(client)

    assert client.post(
        "/journal", data=trade_form(card_id=card_id), headers=headers, follow_redirects=False,
    ).status_code == 303

    trades = m._journal().list()
    assert len(trades) == 1
    linked = m._card_repo().get_card(card_id)
    assert linked is not None
    assert linked.trade_id == trades[0].id
