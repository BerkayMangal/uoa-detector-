"""Phase 5.2.C32-fix4: a trade's own fills survive a busy fill table.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §5 (C3, "On a logged
card or journal trade, ``Dolum gir`` records the actual fill price, contracts
and side").

The journal read every fill the repository would return — a bounded, newest-first
read of the whole table — and then kept the ones belonging to this page's cards
in Python. Once other cards had written more fills than that bound, a linked
trade's own records fell outside the window and silently stopped rendering,
while the same records still listed on ``/kart``. Nothing was lost in the
database; the page simply stopped showing it. The filter is now in SQL, so the
row budget is spent on the page's own cards.

Pins:
  - ``list_fills(card_ids=...)`` filters in SQL, and an empty set selects
    nothing rather than everything;
  - a linked trade's fill still renders on ``/journal`` with more than the
    repository's default bound of unrelated fills in the table;
  - the card page still lists that same fill, and a journal with no cards is
    untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from webapp.board import fills as fills_module
from webapp.board.db import make_engine
from webapp.board.fills import AssumedQuote, FillRepo

from tests.unit._alfa_card_harness import trade_form
from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_fill_routes import _NOW, _fill, _log_card, _reset, _seed

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient
    from sqlalchemy.engine import Engine

_CAP = fills_module._DEFAULT_LIST_LIMIT  # the repository's own newest-first bound
_QUOTE = AssumedQuote(bid=0.99, ask=1.01, mid=1.00, age_seconds=41)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'journal-fills.db'}")
    yield eng
    eng.dispose()


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType]]:
    url = f"sqlite:///{tmp_path / 'journal-fills-route.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    import webapp.main as m

    _reset(m)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _seed(url)
    yield authed_client(m.app, monkeypatch), m
    _reset(m)


# ---------------------------------------------------------------------------
# The SQL filter
# ---------------------------------------------------------------------------


def test_list_fills_filters_by_a_set_of_cards(engine: Engine) -> None:
    repo = FillRepo(engine)
    ids = {
        card: repo.write_fill(
            card_id=card, trade_id=None, side="giriş",
            fill_price=1.06, contracts=1.0, quote=_QUOTE,
        )
        for card in ("card-1", "card-2", "card-3")
    }
    found = repo.list_fills(card_ids=["card-1", "card-3"])
    assert {fill.id for fill in found} == {ids["card-1"], ids["card-3"]}
    assert repo.list_fills(card_ids=["card-2"])[0].id == ids["card-2"]
    assert repo.list_fills(card_ids=["no-such-card"]) == ()
    # An empty set selects NOTHING: it must never fall back to the whole table.
    assert repo.list_fills(card_ids=[]) == ()
    assert repo.list_fills(card_ids=[""]) == ()
    assert len(repo.list_fills()) == 3  # the unfiltered read is unchanged


def test_the_set_filter_spends_the_row_budget_on_those_cards(engine: Engine) -> None:
    """The page's own cards are read even when the rest of the table is larger than the bound."""
    repo = FillRepo(engine)
    wanted = repo.write_fill(
        card_id="mine", trade_id=None, side="giriş",
        fill_price=1.06, contracts=1.0, quote=_QUOTE,
    )
    for i in range(_CAP + 5):
        repo.write_fill(
            card_id="other", trade_id=None, side="giriş",
            fill_price=1.00 + i / 1000, contracts=1.0, quote=_QUOTE,
        )
    assert [fill.id for fill in repo.list_fills(card_ids=["mine"])] == [wanted]
    # Whereas the unfiltered read cannot reach it: it is the oldest row of many.
    assert wanted not in {fill.id for fill in repo.list_fills()}


# ---------------------------------------------------------------------------
# Rendered on /journal
# ---------------------------------------------------------------------------


def test_a_linked_trades_fill_renders_despite_a_busy_table(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    assert client.post(
        "/journal", data=trade_form(card_id=card_id), follow_redirects=False,
    ).status_code == 303
    assert _fill(client, card_id).status_code == 303
    (recorded,) = m._fill_repo().list_fills()

    # Noise: more unrelated fills than the repository's newest-first bound.
    other = _log_card(client)
    repo = m._fill_repo()
    for i in range(_CAP + 5):
        repo.write_fill(
            card_id=other, trade_id=None, side="giriş",
            fill_price=1.00 + i / 1000, contracts=1.0, quote=_QUOTE,
        )

    body = client.get("/journal").text
    assert f'data-fill-block data-card="{card_id}"' in body  # the block still renders
    assert f'data-fill="{recorded.id}"' in body  # and so does the record itself
    # The card page shows the same record, as it did before.
    assert f'data-fill="{recorded.id}"' in client.get(f"/kart/{card_id}").text


def test_a_journal_without_cards_is_untouched(
    board: tuple[TestClient, ModuleType],
) -> None:
    """D10: the pre-FAZ-C journal still renders exactly as it did."""
    client, m = board
    assert client.post("/journal", data=trade_form(), follow_redirects=False).status_code == 303
    body = client.get("/journal").text
    assert body.count('action="/alfa/fill"') == 0
    assert "data-card-link" not in body
    assert m._fill_repo().list_fills() == ()
