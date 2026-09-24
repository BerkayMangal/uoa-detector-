"""Phase 5.2.B-fix3: an unread catalyst chip reads bilinmiyor, never a measured zero.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-UN1 ("An unknown
family ... is never painted as clean"), §5 A5 (the fallback lists what was
checked) and §6 B4 (the catalyst chip).

Review findings FB-H3 / FB-03: with the catalyst sources never fetched, the row
still said ``vade içi katalizör (0)`` in the ``Bakılanlar`` list and carried
``data-catalyst="yok"``, next to a chip whose own text reads ``bilinmiyor``.
That is the unknown-as-clean-zero conflation R-UN1 forbids, and on a fresh
deploy - before the 07:15 ET catalysts job has ever succeeded - it was every
row.

Pins:
  - a row whose chip could not be read renders ``data-catalyst="bilinmiyor"``
    and names the check as ``vade içi katalizör (bilinmiyor)``;
  - a row whose sources were fetched and found nothing still reads ``yok`` and
    counts ``(0)``;
  - ``build_opening_view`` reports the same three-way reading;
  - R-WD1 over the rendered page.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.orm import Session
from webapp.board.alfa_page import build_opening_view, expiry_close
from webapp.board.catalysts import (
    AlfaCatalyst,
    AlfaCatalystFetch,
    ensure_catalyst_tables,
    read_board_catalysts,
)
from webapp.board.db import make_engine
from webapp.board.honesty import ensure_clean, forbidden_words
from webapp.board.narrative import CHECK_TEMPLATES
from webapp.board.settings import load_board_settings

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_EXPIRY = date(2026, 9, 18)

# KNO: every catalyst source fetched, nothing scheduled. UNK: never fetched.
_ROWS = (
    _Row("c1", "KNO", "call", "at_ask", "300000", 0.4211, _CONFIRMING,
         tape=400_000.0, quote=(0.39, 0.41)),
    _Row("c2", "UNK", "call", "at_ask", "200000", 0.5322, _CONFIRMING,
         tape=400_000.0, quote=(0.39, 0.41)),
)


def _seed_fetch_coverage(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_catalyst_tables(engine)
        with Session(engine) as session:
            for source, ticker in (("earnings", "KNO"), ("fda", "KNO"), ("macro", "*")):
                session.add(AlfaCatalystFetch(
                    source=source, ticker=ticker, last_attempt_at=_NOW,
                    last_status="ok", last_success_at=_NOW,
                ))
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'catalyst.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    _seed_fetch_coverage(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _cell(row_html: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", row_html)
    return None if found is None else html.unescape(found.group(1))


def test_an_unread_chip_renders_unknown_not_no_catalyst(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    unknown = _cell(rows["UNK"], 'data-catalyst="bilinmiyor"')
    # Its earnings and FDA sources were never fetched; the macro calendar is market-wide,
    # so that part alone is a real "yok". One unknown part is enough: the count is not a zero.
    assert unknown == "Vade içinde katalizör: Kazanç: bilinmiyor · FDA: bilinmiyor · Makro: yok"
    assert 'data-catalyst="yok"' not in rows["UNK"]


def test_a_fetched_source_with_nothing_scheduled_still_reads_no(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    none_found = _cell(rows["KNO"], 'data-catalyst="yok"')
    assert none_found == "Vade içinde katalizör: Kazanç: yok · FDA: yok · Makro: yok"


def test_the_checked_list_says_unknown_for_an_unread_chip(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    unread = _cell(rows["UNK"], "<p data-checked")
    assert unread is not None
    assert "vade içi katalizör (bilinmiyor)" in unread
    assert "vade içi katalizör (0)" not in unread
    # A chip that was actually read still counts.
    checked = _cell(rows["KNO"], "<p data-checked")
    assert checked is not None
    assert "vade içi katalizör (0)" in checked


def test_the_view_reports_the_three_way_reading(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'view.db'}")
    try:
        ensure_catalyst_tables(engine)
        unread = read_board_catalysts(
            engine, [("AAA", expiry_close(_EXPIRY))], settings=_SETTINGS, now=_NOW,
        )[("AAA", expiry_close(_EXPIRY))]
        with Session(engine) as session:
            for source, ticker in (("earnings", "AAA"), ("fda", "AAA"), ("macro", "*")):
                session.add(AlfaCatalystFetch(
                    source=source, ticker=ticker, last_attempt_at=_NOW,
                    last_status="ok", last_success_at=_NOW,
                ))
            session.add(AlfaCatalyst(
                ticker="AAA", kind="earnings", when_key="2026-09-17", title="Q3",
                starts_at=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 4, 0, tzinfo=UTC), precision="day",
                timing="postmarket", estimated=False, detail=None, fetched_at=_NOW,
            ))
            session.commit()
        read = read_board_catalysts(
            engine, [("AAA", expiry_close(_EXPIRY))], settings=_SETTINGS, now=_NOW,
        )[("AAA", expiry_close(_EXPIRY))]
    finally:
        engine.dispose()

    no_chip = build_opening_view(None, None, expired=False)
    assert no_chip.catalyst_known is False
    unread_view = build_opening_view(None, unread, expired=False)
    assert unread_view.catalyst_known is False  # fetched nothing: the count is not a zero
    read_view = build_opening_view(None, read, expired=False)
    assert read_view.catalyst_known is True
    assert read_view.catalyst_in_window == ("Kazanç",)


def test_the_unknown_check_label_is_frozen_and_clean() -> None:
    label = CHECK_TEMPLATES["catalyst_unknown"]
    assert label == "vade içi katalizör (bilinmiyor)"
    assert ensure_clean(label) == label


def test_the_catalyst_reading_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/alfa", params=gate).text
        assert forbidden_words(html.unescape(body)) == []
