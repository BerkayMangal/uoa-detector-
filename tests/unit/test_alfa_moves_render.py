"""Phase 5.2.B2b: required move vs expected move on the rendered board (contract §6 B2).

The seeded sqlite run of ``test_board_honesty`` is reused; ``alfa_atm`` rows are
written the way the refresher writes them.

Pins:
  - the line reads ``Başabaş için %X gerekir · ATM straddle bu vadeye %Y
    fiyatlıyor``, with the ATM strike offset disclosed;
  - a row whose ATM legs have no two-sided quote falls back to the labelled
    ``IV tahmini (straddle değil)``;
  - a nearest-expiry substitution is disclosed;
  - without an ATM row both halves read ``bilinmiyor``;
  - the ATM row's age is on the line (R-CO2);
  - no probability wording, R-WD1 over the page, the score stays in the audit
    block, and zero Unusual Whales calls.
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

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_EXPIRY = date(2026, 9, 18)  # the seeded rows are 3 DTE
_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)
_QUOTE = (0.39, 0.41)

# MOV: a straddle for the row's own expiry. IVF: no two-sided ATM quote, so the IV fallback.
# NXT: only a later ATM expiry. NOA: no ATM row at all.
_ROWS = (
    _Row("m1", "MOV", "call", "at_ask", "400000", 0.6137, _CONFIRMING, quote=_QUOTE),
    _Row("m2", "IVF", "call", "at_ask", "300000", 0.7248, _CONFIRMING, quote=_QUOTE),
    _Row("m3", "NXT", "call", "at_ask", "200000", 0.8319, _CONFIRMING, quote=_QUOTE),
    _Row("m4", "NOA", "call", "at_ask", "100000", 0.9412, _CONFIRMING, quote=_QUOTE),
)


def _atm_row(ticker: str, expiry: date, **over: Any) -> AlfaAtm:
    fields: dict[str, Any] = {
        "ticker": ticker, "expiry": expiry, "strike": 100.0, "stock_price": 99.0,
        "call_bid": 1.00, "call_ask": 1.10, "call_iv": 0.40,
        "put_bid": 0.90, "put_ask": 1.00, "put_iv": 0.40,
        "trade_date": _TS.date(), "fetched_at": _NOW - timedelta(seconds=41),
    }
    fields.update(over)
    return AlfaAtm(**fields)


def _seed_atm(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_atm_tables(engine)
        with Session(engine) as session:
            session.add(_atm_row("MOV", _EXPIRY))
            # No two-sided quote on either leg: the straddle cannot be priced.
            session.add(_atm_row("IVF", _EXPIRY, call_bid=None, put_ask=None))
            session.add(_atm_row("NXT", date(2026, 9, 25)))
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'moves.db'}"
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


def test_the_line_compares_the_break_even_with_the_straddle(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["MOV"]
    # (100 + 0.41) / 99 - 1 = 1.42%; (1.05 + 0.95) / 99 = 2.02%.
    assert _cell(row, "data-move-text") == (
        "Başabaş için %1.4 gerekir · ATM straddle bu vadeye %2.0 fiyatlıyor"
    )
    assert _cell(row, "data-move-disclosure") == "ATM strike 100, spot 99, fark %1.0"
    assert 'data-move="known"' in row


def test_the_atm_row_shows_its_age(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["MOV"]
    assert _cell(row, "data-move-age") == "ATM satırı 41 sn önce alındı"


def test_without_two_sided_atm_quotes_the_fallback_is_labelled(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["IVF"]
    text = _cell(row, "data-move-text")
    assert text is not None
    assert text.startswith("Başabaş için %1.4 gerekir · IV tahmini (straddle değil) bu vadeye %")
    assert "straddle bu vadeye" not in text.replace("IV tahmini (straddle değil) bu vadeye", "")


def test_a_nearest_expiry_substitution_is_disclosed(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["NXT"]
    disclosure = _cell(row, "data-move-disclosure")
    assert disclosure is not None
    assert disclosure.startswith("en yakın ATM vadesi 25.09 (bu vade yok) · ")


def test_without_an_atm_row_both_halves_read_unknown(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["NOA"]
    assert _cell(row, "data-move-text") == (
        "Başabaş için gereken hareket: bilinmiyor · ATM straddle: bilinmiyor"
    )
    assert 'data-move="unknown"' in row
    assert "data-move-age" not in row


def test_the_move_line_states_no_probability(client: TestClient) -> None:
    body = html.unescape(client.get("/alfa", params={"gate": "off"}).text)
    for row in _rows(body).values():
        line = _cell(row, "data-move-text") or ""
        for banned in ("olasılık", "ihtimal", "beklenen"):
            assert banned not in line.lower(), line
    assert forbidden_words(body) == []


def test_the_move_line_leaks_no_score(client: TestClient) -> None:
    body = client.get("/alfa", params={"gate": "off"}).text
    outside = _AUDIT.sub("", body)
    for spec in _ROWS:
        assert f"{spec.score:.2f}" not in outside, spec.ticker


def test_a_failed_atm_read_reads_unknown_and_never_breaks_the_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.board.alfa_page as page_module

    def _broken(engine: object, tickers: object) -> Any:
        msg = "alfa_atm unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(page_module, "read_board_atm", _broken)
    body = client.get("/alfa", params={"gate": "off"}).text
    assert body.count('data-move="unknown"') == len(_ROWS)
    assert "ATM straddle: bilinmiyor" in html.unescape(body)


def test_the_move_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a board render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    response = client.get("/alfa", params={"gate": "off"})
    assert response.status_code == 200
    assert "data-move-text" in response.text
    assert calls == []
