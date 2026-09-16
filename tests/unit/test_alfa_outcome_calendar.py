"""Phase 5.2.C32-fix6: no row before the horizon can be shown to have passed.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2): "The daily
post-close job computes outcomes for each horizon in
``outcomes.horizons_trading_days`` ... once the horizon has passed."

The horizon gate is the stored SPY session calendar. When that calendar does not
reach the card's own session, the job could not place the card at all — and the
old code answered ``pending`` for that case, so a database whose close history
had not been fetched yet received a permanent ``bekliyor`` row for every card and
every horizon, including horizons still in the future. ``alfa_outcome`` is
append-only, so those rows are forever; they are honest in wording and cost no
quota, but they claim to be waiting on an event that has not happened.

Resolving now answers ``not_due`` for that case, which the job already skips
without writing. The distinction it preserves: a horizon that HAS passed with the
ticker's own closes missing still writes ``bekliyor`` (§4, "Missing closes leave
the row bekliyor"), because that row is waiting on a fetch that is genuinely due.

Pins:
  - a calendar that cannot place the card is ``not_due``, at every horizon;
  - an empty calendar writes no row at all, and nothing is lost by waiting: the
    rows appear, computed, once the calendar arrives;
  - the other side of the boundary is unchanged — with the calendar present and
    the ticker's closes missing, the row is still ``bekliyor``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from webapp.board.cards import CardRepo
from webapp.board.daily_close import build_close_index, ensure_daily_close_tables
from webapp.board.db import make_engine
from webapp.board.outcome_job import resolve_outcome, run_outcome_job
from webapp.board.outcomes import COMPUTED, PENDING, OutcomeRepo

from tests.unit.test_alfa_outcome_job import (
    _AAA_CLOSES,
    _CREATED,
    _HORIZONS,
    _NOW,
    _SETTINGS,
    _SPY_CLOSES,
    _card,
    _NoClient,
    _store_closes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from webapp.board.cards import DecisionCard

_EMPTY = build_close_index(())
# Sessions that all fall AFTER the card's own day (the card is 2026-09-16).
_LATER_SESSIONS = (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'outcome-calendar.db'}")
    ensure_daily_close_tables(eng)
    yield eng
    eng.dispose()


def _stored_card(engine: Engine) -> DecisionCard:
    card = CardRepo(engine).get_card(_card(engine, created=_CREATED))
    assert card is not None
    return card


# ---------------------------------------------------------------------------
# Resolving, with no calendar to place the card against
# ---------------------------------------------------------------------------


def test_a_calendar_that_cannot_place_the_card_is_not_due_yet(engine: Engine) -> None:
    card = _stored_card(engine)
    for horizon in _HORIZONS:
        empty = resolve_outcome(card, horizon, sessions=(), underlying=_EMPTY, spy=_EMPTY)
        assert empty.state == "not_due", horizon
        # A calendar that starts after the card's own session cannot place it either.
        later = resolve_outcome(
            card, horizon, sessions=_LATER_SESSIONS, underlying=_EMPTY, spy=_EMPTY,
        )
        assert later.state == "not_due", horizon


async def test_an_empty_calendar_writes_no_row_at_all(engine: Engine) -> None:
    card_id = _card(engine, created=_CREATED)
    report = await run_outcome_job(_NoClient(), engine, settings=_SETTINGS, now=_NOW)

    assert report.cards == 1
    assert (report.written, report.pending, report.computed, report.no_data) == (0, 0, 0, 0)
    repo = OutcomeRepo(engine)
    for horizon in _HORIZONS:
        assert repo.get_outcome(card_id, horizon) is None, horizon


async def test_the_rows_appear_once_the_calendar_arrives(engine: Engine) -> None:
    """Nothing is lost by waiting: the same run that would have written bekliyor computes."""
    card_id = _card(engine, created=_CREATED)
    first = await run_outcome_job(_NoClient(), engine, settings=_SETTINGS, now=_NOW)
    assert first.written == 0

    _store_closes(engine, "SPY", _SPY_CLOSES)
    _store_closes(engine, "AAA", _AAA_CLOSES)
    second = await run_outcome_job(_NoClient(), engine, settings=_SETTINGS, now=_NOW)

    assert second.computed == len(_HORIZONS)
    repo = OutcomeRepo(engine)
    for horizon in _HORIZONS:
        stored = repo.get_outcome(card_id, horizon)
        assert stored is not None, horizon
        assert stored.status == COMPUTED, horizon


# ---------------------------------------------------------------------------
# The other side of the boundary (contract §4: missing closes DO wait)
# ---------------------------------------------------------------------------


async def test_with_the_calendar_present_a_missing_ticker_still_waits(engine: Engine) -> None:
    """The horizon demonstrably passed, so the row is written and waits on a fetch."""
    _store_closes(engine, "SPY", _SPY_CLOSES)  # the calendar exists, the name does not
    card_id = _card(engine, ticker="BBB", created=_CREATED)
    report = await run_outcome_job(_NoClient(), engine, settings=_SETTINGS, now=_NOW)

    assert report.pending == len(_HORIZONS)
    repo = OutcomeRepo(engine)
    for horizon in _HORIZONS:
        stored = repo.get_outcome(card_id, horizon)
        assert stored is not None, horizon
        assert stored.status == PENDING, horizon
        assert stored.market_neutral_excess is None, horizon  # never a zero
