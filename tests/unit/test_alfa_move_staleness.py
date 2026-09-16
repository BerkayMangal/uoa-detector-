"""Phase 5.2.B-fix4: the required-move line refuses a stale ATM row.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CO1 ("Cost always
uses real prices") and §6 B2 (required move against the current spot from
``alfa_atm.stock_price``).

Review finding FB-H4: ``build_move_view`` passed ``alfa_atm`` rows of unbounded
age into the comparison, so a cost statement (``Başabaş için %X gerekir``) could
be priced off a spot hours or days old - the very row B3's chase verdict refuses
as stale, on the same board row. ``alfa_atm`` rows survive restarts and
weekends, so a Monday pre-open board rendered Friday's straddle.

Pins:
  - a row whose only ATM row is older than ``tradability.max_quote_age_seconds``
    reads ``bilinmiyor`` on both halves and is marked unknown;
  - the age of that stale row still renders, so the reader sees why;
  - the cutoff is the same one the chase spot uses, to the second;
  - the move and the chase spot now agree on one row;
  - R-WD1 over the page, and zero Unusual Whales calls.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.atm import AlfaAtm, ensure_atm_tables
from webapp.board.db import make_engine
from webapp.board.honesty import forbidden_words
from webapp.board.settings import load_board_settings

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_MAX_AGE = _SETTINGS.tradability.max_quote_age_seconds
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_EXPIRY = date(2026, 9, 18)
_QUOTE = (0.39, 0.41)

# FRS: a fresh ATM row. EDG: exactly at the cutoff (still fresh). STL: one second past it.
_ROWS = (
    _Row("s1", "FRS", "call", "at_ask", "300000", 0.4211, _CONFIRMING, quote=_QUOTE),
    _Row("s2", "EDG", "call", "at_ask", "200000", 0.5322, _CONFIRMING, quote=_QUOTE),
    _Row("s3", "STL", "call", "at_ask", "100000", 0.6433, _CONFIRMING, quote=_QUOTE),
)
_AGES = {"FRS": 41, "EDG": _MAX_AGE, "STL": _MAX_AGE + 1}


def _seed_atm(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_atm_tables(engine)
        with Session(engine) as session:
            for ticker, age in _AGES.items():
                session.add(AlfaAtm(
                    ticker=ticker, expiry=_EXPIRY, strike=100.0, stock_price=99.0,
                    call_bid=1.00, call_ask=1.10, call_iv=0.40,
                    put_bid=0.90, put_ask=1.00, put_iv=0.40,
                    trade_date=_TS.date(), fetched_at=_NOW - timedelta(seconds=age),
                ))
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'stale.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    _seed_atm(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _cell(row_html: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", row_html)
    return None if found is None else html.unescape(found.group(1))


def test_a_stale_atm_row_gives_no_break_even_number(client: TestClient) -> None:
    row = _rows(client.get("/", params={"gate": "off"}).text)["STL"]
    assert _cell(row, "data-move-text") == (
        "Başabaş için gereken hareket: bilinmiyor · ATM straddle: bilinmiyor"
    )
    assert 'data-move="unknown"' in row


def test_the_stale_row_still_shows_its_age(client: TestClient) -> None:
    row = _rows(client.get("/", params={"gate": "off"}).text)["STL"]
    age = _cell(row, "data-move-age")
    assert age is not None
    assert age.startswith("ATM satırı 15 dk önce alındı")  # why it reads unknown


def test_the_cutoff_matches_the_chase_spot_to_the_second(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    # At the cutoff the row is still fresh, exactly as chase.current_spot treats it.
    fresh = _cell(rows["EDG"], "data-move-text")
    assert fresh is not None
    assert fresh.startswith("Başabaş için %") and "ATM straddle bu vadeye %" in fresh
    assert 'data-move="known"' in rows["EDG"]
    assert _cell(rows["FRS"], "data-move-text") == fresh


def test_the_move_and_the_chase_spot_agree_on_one_row(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    stale_chase = _cell(rows["STL"], "data-chase-line")
    assert stale_chase is not None
    assert "hisse hareketi bilinmiyor" in stale_chase  # B3 already refused the same row
    fresh_chase = _cell(rows["FRS"], "data-chase-line")
    assert fresh_chase is not None
    assert "hisse baskıdan beri %" in fresh_chase


def test_the_stale_reading_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


def test_the_stale_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a board render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    response = client.get("/", params={"gate": "off"})
    assert response.status_code == 200
    assert "data-move-text" in response.text
    assert calls == []
