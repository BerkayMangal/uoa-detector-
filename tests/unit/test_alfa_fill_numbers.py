"""Phase 5.2.C32-fix1: a fill price must be a finite number.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (``alfa_fill`` is
append-only) and §5 (slippage).

``inf`` and ``nan`` pass a bare ``<= 0`` check, and a form field typed ``float``
accepts ``inf``, ``1e400`` and ``nan``. Before this fix such a price was frozen
into the append-only table, rendered as ``+$inf`` on the card page, and would
have made that side's median and interquartile range ``inf`` or ``nan`` forever
once the sample reached ``fills.min_n_for_stats`` — with no repair path, since
the repository exposes no update and no delete.

Pins:
  - the finite-and-positive gate itself, in both directions;
  - the repository refuses a non-finite price or size and writes nothing —
    the last line of defence for the append-only table;
  - the route refuses the same numbers with the frozen ``bad_numbers`` line and
    writes nothing, so no ``inf`` ever reaches the card page;
  - a card snapshot carrying a non-finite quote yields no assumption at all
    (``json.loads`` accepts ``Infinity``), so the route refuses the fill rather
    than measuring against it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from webapp.board import fills
from webapp.board.db import make_engine
from webapp.board.fills import AssumedQuote, FillRepo, assumed_quote_from_card, usable_numbers

from tests.unit._alfa_card_harness import ORIGIN
from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_fill_routes import _NOW, _fill, _log_card, _reset, _seed

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    from fastapi.testclient import TestClient
    from sqlalchemy.engine import Engine

_INF = float("inf")
_NAN = float("nan")
_QUOTE = AssumedQuote(bid=0.99, ask=1.01, mid=1.00, age_seconds=41)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'fill-numbers.db'}")
    yield eng
    eng.dispose()


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ModuleType]]:
    url = f"sqlite:///{tmp_path / 'fill-numbers-route.db'}"
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
# The gate itself
# ---------------------------------------------------------------------------


def test_usable_numbers_is_the_finite_and_positive_gate() -> None:
    assert usable_numbers(1.06, 1.0) is True
    assert usable_numbers(0.01) is True
    for bad in (0.0, -1.0, _INF, -_INF, _NAN):
        assert usable_numbers(bad) is False, bad
        # One bad value spoils the pair, whichever side it is on.
        assert usable_numbers(1.06, bad) is False, bad
        assert usable_numbers(bad, 1.06) is False, bad


# ---------------------------------------------------------------------------
# The repository: the last line of defence for an append-only table
# ---------------------------------------------------------------------------


def test_write_fill_refuses_a_non_finite_price_or_size(engine: Engine) -> None:
    repo = FillRepo(engine)
    for bad in (_INF, -_INF, _NAN):
        with pytest.raises(ValueError, match="finite"):
            repo.write_fill(
                card_id="card-1", trade_id=None, side="giriş",
                fill_price=bad, contracts=1.0, quote=_QUOTE,
            )
        with pytest.raises(ValueError, match="finite"):
            repo.write_fill(
                card_id="card-1", trade_id=None, side="giriş",
                fill_price=1.06, contracts=bad, quote=_QUOTE,
            )
    assert repo.list_fills() == ()  # nothing was appended by any of them


def test_a_snapshot_with_a_non_finite_quote_yields_no_assumption() -> None:
    """``json.loads`` accepts ``Infinity``; a non-finite bid or ask is not a quote."""
    for bid, ask in ((_INF, 1.01), (0.99, _INF), (_NAN, 1.01), (0.99, _NAN)):
        snapshot = {"row_view": {"chip": {"bid": bid, "ask": ask, "quote_age_seconds": 41}}}
        assert assumed_quote_from_card(snapshot) is None, (bid, ask)
    # The same shape with real numbers still reads, so the guard is not blanket.
    good = {"row_view": {"chip": {"bid": 0.99, "ask": 1.01, "quote_age_seconds": 41}}}
    assert assumed_quote_from_card(good) == _QUOTE


# ---------------------------------------------------------------------------
# The route: nothing written, and no "inf" on the page
# ---------------------------------------------------------------------------


def test_the_route_refuses_a_non_finite_price_and_writes_nothing(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    for typed in ("inf", "-inf", "1e400", "nan", "Infinity"):
        response = _fill(client, card_id, price=typed)
        assert response.status_code == 400, (typed, response.status_code)
        assert response.text == fills.FILL_COPY["bad_numbers"]
        assert m._fill_repo().list_fills() == (), typed


def test_the_route_refuses_a_non_finite_contract_count(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    for typed in ("inf", "1e400", "nan"):
        response = _fill(client, card_id, contracts=typed)
        assert response.status_code == 400, (typed, response.status_code)
        assert response.text == fills.FILL_COPY["bad_numbers"]
        assert m._fill_repo().list_fills() == (), typed


def test_no_refused_number_ever_reaches_the_card_page(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    assert _fill(client, card_id, price="inf").status_code == 400
    assert _fill(client, card_id, price="1e400").status_code == 400
    body = client.get(f"/kart/{card_id}").text
    assert "inf" not in body.lower().replace("info", "")  # no +$inf, no %inf anywhere
    assert m._fill_repo().list_fills() == ()

    # And a real fill still records on the very same card, so the guard is a
    # filter on the numbers and not on the route.
    assert _fill(client, card_id, price="1.06").status_code == 303
    (stored,) = m._fill_repo().list_fills()
    assert stored.fill_price == 1.06
    assert client.post(
        "/alfa/fill",
        data={
            "fill_card_id": card_id, "fill_side": "giriş",
            "fill_price": "1.06", "fill_contracts": "1",
        },
        headers=ORIGIN, follow_redirects=False,
    ).status_code == 303
