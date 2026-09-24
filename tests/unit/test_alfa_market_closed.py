"""Phase 5.3: a closed market and a stale feed are different facts.

On a Saturday the board renders twenty rows of ``kotasyon yok`` and, before this,
never said why. Both a shut exchange and a broken quote writer produce exactly
that page, and only one of them is a fault. The owner reads the same screen in
both cases and cannot tell them apart.

Pins:
  - the page states the market is closed and dates what is on screen;
  - the text never implies a live quote;
  - an open market says nothing (no banner, no new state);
  - the page model stays pure — it is told, it does not read a clock;
  - the route derives the flag from ``sources.market_hours.is_market_open``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from webapp.board.alfa_page import ALFA_COPY, AlfaPage

from tests.unit._alfa_card_harness import open_board, reset_singletons
from uoa_detector.sources.market_hours import is_market_open

if TYPE_CHECKING:
    from pathlib import Path

# A Saturday and a Wednesday inside the RTH window, both in UTC.
_SATURDAY = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)
_WEDNESDAY_RTH = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)


def _page(*, closed: bool, session: date | None) -> AlfaPage:
    return AlfaPage(
        rows=(), views=(), sections=(), print_count=0, load_failed=False,
        session_date=session, today=date(2026, 9, 19), market_closed=closed,
    )


def test_a_closed_market_says_so_and_dates_what_is_on_screen() -> None:
    text = _page(closed=True, session=date(2026, 9, 18)).market_closed_text
    assert "Piyasa kapalı" in text
    assert "2026-09-18" in text, "the owner must see which session he is looking at"


def test_the_closed_text_never_implies_a_live_quote() -> None:
    """R-CO1's spirit: the page may not suggest a price it does not have."""
    text = _page(closed=True, session=date(2026, 9, 18)).market_closed_text
    lowered = text.lower()
    assert "canlı kotasyonla değil" in lowered
    # And it must not claim anything is current.
    assert "şu an" not in lowered
    assert "şimdi" not in lowered


def test_without_a_session_date_it_admits_that_instead_of_guessing() -> None:
    text = _page(closed=True, session=None).market_closed_text
    assert text == ALFA_COPY["market_closed_undated"]
    assert "bilinmiyor" in text.lower()


def test_an_open_market_sets_no_flag() -> None:
    """The default is False, so an open session renders exactly what it rendered before."""
    assert _page(closed=False, session=date(2026, 9, 18)).market_closed is False
    assert AlfaPage(
        rows=(), views=(), sections=(), print_count=0, load_failed=False,
    ).market_closed is False


def test_the_flag_the_route_would_compute_matches_the_calendar() -> None:
    """The route passes ``not is_market_open(now)``; this is that arithmetic, pinned.

    If the market-hours window or the weekend rule ever changes, the board's
    closed banner changes with it rather than drifting on its own copy of the
    calendar.
    """
    assert is_market_open(_SATURDAY) is False
    assert is_market_open(_WEDNESDAY_RTH) is True
    # Which is what the page is told, in each case.
    assert _page(closed=not is_market_open(_SATURDAY), session=date(2026, 9, 18)).market_closed
    assert not _page(
        closed=not is_market_open(_WEDNESDAY_RTH), session=date(2026, 9, 16),
    ).market_closed


def test_a_failed_read_still_reports_the_closed_market() -> None:
    """A load failure and a closed market can both be true; one must not hide the other."""
    page = AlfaPage(
        rows=(), views=(), sections=(), print_count=0, load_failed=True,
        session_date=None, today=date(2026, 9, 19), market_closed=True,
    )
    assert page.load_failed is True
    assert page.market_closed is True


def test_the_rendered_page_shows_the_closed_state_on_a_saturday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assertion that actually protects the feature: it is in the HTML.

    The properties above can all pass while the template renders nothing. This
    drives the real route twice — once with the page clock on a Saturday, once
    inside a Wednesday session — and reads the rendered state attribute.
    """
    gen = open_board(tmp_path, monkeypatch, name="closed.db")
    client, module = next(gen)
    try:
        monkeypatch.setattr(module, "_now", lambda: _SATURDAY)
        closed = client.get("/alfa").text
        assert 'data-state="market-closed"' in closed
        assert "Piyasa kapalı" in closed

        monkeypatch.setattr(module, "_now", lambda: _WEDNESDAY_RTH)
        during = client.get("/alfa").text
        assert 'data-state="market-closed"' not in during
        assert "Piyasa kapalı" not in during
    finally:
        reset_singletons(module)
