"""Phase 5.2.C32-fix7: the cross-card slippage summary carries no card-level quote.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §5 (C3), which asks for
the median and interquartile range "next to the assumed spread" — wording written
for one sample paired with the quote it was measured against.

On ``/kart`` the summary's sample is every recorded fill (``summary_scope``),
while the spread and bid/ask printed inside the same box were THIS card's. A
reader cannot see that scope change: a median printed under one card's quote
reads as a median of that quote. The card's own quote now appears only where it
is exactly that — in the ``Dolum gir`` form, as the assumption the owner is about
to test, and on each fill row, as the assumption that fill was measured against.

Pins:
  - the summary box holds no card-level spread or bid/ask, and no quote at all;
  - nothing was lost: the form still shows this card's quote, its age and its
    spread, and each fill row still carries the quote it was measured against;
  - the summary still names its scope and shows the count gate;
  - the page stays clean of forbidden words.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

import pytest
from webapp.board import fills
from webapp.board.honesty import forbidden_words

from tests.unit._alfa_card_harness import ORIGIN, card_id_from
from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_fill_routes import (
    _NOW,
    _QUOTE_AGE,
    _fill,
    _log_card,
    _reset,
    _seed,
    _visible,
    _write_fills,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType]]:
    url = f"sqlite:///{tmp_path / 'card-scope.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    import webapp.main as m

    _reset(m)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _seed(url)
    yield authed_client(m.app, monkeypatch), m
    _reset(m)


def _summary_box(body: str) -> str:
    """The slippage summary box, from its marker to its closing disclaimer."""
    found = re.search(r"data-slippage-summary.*?data-no-significance[^<]*<", body, re.S)
    assert found is not None, "no slippage summary on the page"
    return html.unescape(found.group(0))


def _form_block(body: str) -> str:
    found = re.search(r"data-fill-block.*?data-fill-help", body, re.S)
    assert found is not None, "no fill form on the page"
    return html.unescape(found.group(0))


# ---------------------------------------------------------------------------
# The summary box: one sample, no card's quote
# ---------------------------------------------------------------------------


def test_the_summary_box_holds_no_card_level_quote(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    _write_fills(m, card_id, 3)

    body = client.get(f"/kart/{card_id}").text
    assert "data-summary-assumed-spread" not in body
    assert "data-summary-assumed-bid-ask" not in body
    box = _summary_box(body)
    # No quote of any kind inside the box: no bid/ask pair, no spread label.
    assert "bid $" not in box
    assert "ask $" not in box
    assert fills.FILL_COPY["assumed_spread"] not in box
    # What the box does say: which sample it covers, and the count gate.
    assert fills.FILL_COPY["stats_title"] in box
    assert "tüm dolum kayıtları" in box
    assert "3 dolum kaydı; istatistik için yetersiz örnek" in html.unescape(box)


def test_the_cards_own_quote_is_still_shown_where_it_belongs(
    board: tuple[TestClient, ModuleType],
) -> None:
    """Nothing was lost: the form is where this card's assumption lives."""
    client, _m = board
    card_id = _log_card(client)
    assert _fill(client, card_id).status_code == 303

    body = client.get(f"/kart/{card_id}").text
    form = _form_block(body)
    assert f"kart anındaki kotasyon, {_QUOTE_AGE} sn yaşında" in form
    assert "bid $0.99 / ask $1.01" in form
    assert fills.FILL_COPY["assumed_spread"] in form
    assert "data-assumed-spread" in body
    # And each recorded fill still carries the quote it was measured against.
    assert "data-fill-assumed" in body
    text = _visible(body)
    assert f"kart anındaki kotasyon, {_QUOTE_AGE} sn yaşında" in text


def test_a_pas_card_shows_the_summary_without_claiming_a_quote(
    board: tuple[TestClient, ModuleType],
) -> None:
    """A pas card has no form, so the box must not be the page's quote by default."""
    client, _m = board
    response = client.post(
        "/alfa/card",
        data={
            "decision": "pas", "card_run_id": "live-2026-09-16",
            "card_ticker": "SPY", "card_direction": "up",
        },
        headers=ORIGIN, follow_redirects=False,
    )
    assert response.status_code == 303
    card_id = card_id_from(response.headers["location"], "pas")

    body = client.get(f"/kart/{card_id}").text
    assert 'data-state="not-logged"' in body
    box = _summary_box(body)
    assert "bid $" not in box
    assert fills.FILL_COPY["assumed_spread"] not in box
    assert forbidden_words(_visible(body)) == []
