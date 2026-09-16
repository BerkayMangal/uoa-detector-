"""Phase 5.2.C32-fix5: /defter reads every outcome of the cards it lists.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2).

The ledger asked ``OutcomeRepo.list_outcomes`` for the listed cards' outcomes
without a limit, so the repository applied its own module default (2000 rows).
The page needs ``ledger.max_cards_per_page`` x ``outcomes.horizons_trading_days``
rows, and at today's profile values (200 x 2 = 400) that fits — but the
relationship was implicit and unasserted, and any stored outcome the bound
dropped would render as ``henüz hesaplanmadı``: a card that WAS measured reading
as one that was not, which is the one thing the ledger's "never a zero, never a
result it does not have" rule exists to prevent.

The read is now bounded by the page itself, the same expression the outcome job
already uses for its own scan.

Pins:
  - the route passes an explicit bound that covers the page it is rendering;
  - the bound is derived from the page, so it grows with the number of listed
    cards rather than being a constant;
  - every stored outcome of every listed card renders, at every horizon, and
    nothing measured reads ``henüz hesaplanmadı``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import outcomes
from webapp.board.db import make_engine
from webapp.board.settings import load_board_settings

from tests.unit._alfa_card_harness import seed
from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_defter_routes import _blocks, _bulk, _defter, _reset

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_HORIZONS = _SETTINGS.outcomes.horizons_trading_days


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType, str]]:
    url = f"sqlite:///{tmp_path / 'defter-reads.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    import webapp.main as m

    _reset(m)
    seed(url)
    yield authed_client(m.app, monkeypatch), m, url
    _reset(m)


def _record(m: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the keyword arguments the route hands to ``list_outcomes``."""
    repo = m._outcome_repo()
    seen: dict[str, Any] = {}
    real = repo.list_outcomes

    def recording(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(repo, "list_outcomes", recording)
    return seen


def _measure_second_horizon(url: str, card_ids: list[str]) -> None:
    """Store an outcome at the SECOND profile horizon for each card."""
    engine = make_engine(url)
    try:
        repo = outcomes.OutcomeRepo(engine)
        for i, card_id in enumerate(card_ids):
            repo.write_final(
                card_id=card_id,
                horizon_days=_HORIZONS[1],
                final=outcomes.FinalOutcome(
                    status=outcomes.COMPUTED,
                    measurement=outcomes.measure(
                        direction="yukarı",
                        underlying_at_card_day=100.0,
                        underlying_at_horizon=104.0 + i,
                        spy_at_card_day=400.0,
                        spy_at_horizon=400.0,
                    ),
                ),
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


def test_the_outcome_read_is_bounded_by_the_page_not_by_a_module_default(
    board: tuple[TestClient, ModuleType, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, m, url = board
    cards = _bulk(url, decision="pas", n=3, measured=3)
    seen = _record(m, monkeypatch)

    _defter(client)
    assert sorted(seen["card_ids"]) == sorted(cards)
    limit = seen["limit"]
    assert isinstance(limit, int)
    # Enough for every listed card at every horizon, so no stored row is dropped.
    assert limit >= len(cards) * len(_HORIZONS)


def test_the_bound_grows_with_the_page(
    board: tuple[TestClient, ModuleType, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived from the page, not a constant that happens to be large enough."""
    client, m, url = board
    _bulk(url, decision="pas", n=1, measured=1)
    seen = _record(m, monkeypatch)
    _defter(client)
    small = seen["limit"]

    _bulk(url, decision="log", n=4, measured=4)
    _defter(client)
    larger = seen["limit"]
    assert larger > small
    assert larger >= 5 * len(_HORIZONS)


# ---------------------------------------------------------------------------
# What the owner sees
# ---------------------------------------------------------------------------


def test_every_stored_outcome_of_a_listed_card_renders(
    board: tuple[TestClient, ModuleType, str],
) -> None:
    client, _m, url = board
    cards = _bulk(url, decision="pas", n=3, measured=3)
    _measure_second_horizon(url, cards)

    blocks = _blocks(_defter(client))
    assert set(blocks) >= set(cards)
    for card_id in cards:
        block = blocks[card_id]
        # Both horizons were measured, so neither may read as not computed yet.
        assert block.count(outcomes.COMPUTED) == len(_HORIZONS), card_id
        assert outcomes.OUTCOME_COPY["not_computed"] not in block, card_id
