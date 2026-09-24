"""Phase 5.2.B3: the chase verdict on the rendered board (contract §6 B3, §5 A5).

The seeded sqlite run of ``test_board_honesty`` is reused. Every seeded print
carries the default print price ($1.50) and spot ($198 at strike 100, so
moneyness 1.98), and one ``alfa_atm`` row per ticker supplies the current spot.

Pins:
  - each verdict band renders with the contract's line;
  - a row with no quote reads ``kotasyon yok`` and never a number;
  - the since-print flow is context only: it renders next to the verdict and
    never changes it;
  - a sold-option row keeps the ask-based verdict and says so;
  - ``geç kaldın`` becomes the row's mandatory counter-argument (priority 3),
    and the fallback's checked list names the chase check;
  - R-WD1 over the page, the score stays in the audit block, and zero UW calls.
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
from webapp.board.chase import CHASE_COPY
from webapp.board.db import make_engine
from webapp.board.honesty import forbidden_words

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_EXPIRY = date(2026, 9, 18)
_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)
_SPOT_NOW = 199.19  # spot at print is 1.98 x 100 = 198.00, so +0.6%

# The print price is $1.50 on every seeded row, so the ask sets the band.
_ROWS = (
    _Row("h1", "MAK", "call", "at_ask", "500000", 0.4211, _CONFIRMING,
         tape=400_000.0, quote=(1.58, 1.60)),  # +6.7%: hâlâ makul
    _Row("h2", "DIK", "call", "at_ask", "400000", 0.5322, _CONFIRMING, quote=(1.66, 1.68)),  # +12%
    _Row("h3", "GEC", "call", "at_ask", "300000", 0.6433, _CONFIRMING, quote=(1.78, 1.80)),  # +20%
    _Row("h4", "SAT", "call", "at_bid", "200000", 0.7544, _CONFIRMING,
         tape=400_000.0, quote=(1.58, 1.60)),  # sold call: the row is aşağı
    _Row("h5", "KOT", "call", "at_ask", "100000", 0.8655, _CONFIRMING),  # no quote row
)


def _seed_atm(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_atm_tables(engine)
        with Session(engine) as session:
            for row in _ROWS:
                session.add(
                    AlfaAtm(
                        ticker=row.ticker, expiry=_EXPIRY, strike=100.0, stock_price=_SPOT_NOW,
                        call_bid=1.00, call_ask=1.10, call_iv=0.40,
                        put_bid=0.90, put_ask=1.00, put_iv=0.40,
                        trade_date=_TS.date(), fetched_at=_NOW - timedelta(seconds=41),
                    ),
                )
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'chase.db'}"
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


def test_each_verdict_band_renders_with_its_line(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    assert _cell(rows["MAK"], "data-chase-verdict") == "hâlâ makul"
    assert _cell(rows["MAK"], "data-chase-line") == (
        "baskı $1.50 → şimdi $1.60 (ask), %6.7 yukarıda; hisse baskıdan beri %+0.6"
    )
    assert 'data-chase="reasonable"' in rows["MAK"]

    assert _cell(rows["DIK"], "data-chase-verdict") == "dikkat"
    assert _cell(rows["DIK"], "data-chase-line") == (
        "baskı $1.50 → şimdi $1.68 (ask), %12 yukarıda; hisse baskıdan beri %+0.6"
    )

    assert _cell(rows["GEC"], "data-chase-verdict") == "geç kaldın"
    assert _cell(rows["GEC"], "data-chase-line") == (
        "baskı $1.50 → şimdi $1.80 (ask), %20 yukarıda; hisse baskıdan beri %+0.6"
    )


def test_a_row_without_a_quote_reads_kotasyon_yok(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["KOT"]
    assert _cell(row, "data-chase-verdict") == "kotasyon yok"
    assert _cell(row, "data-chase-line") == (
        "baskı $1.50 → işlem yapılabilir kotasyon yok; hisse baskıdan beri %+0.6"
    )
    assert 'data-chase="no_quote"' in row


def test_the_flow_context_renders_next_to_the_verdict(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    assert _cell(rows["MAK"], "data-chase-flow") == "akış baskıdan beri sürüyor"
    assert _cell(rows["SAT"], "data-chase-flow") == "akış baskıdan beri döndü"  # sold: row is aşağı
    assert _cell(rows["KOT"], "data-chase-flow") == "akış baskıdan beri: bilinmiyor"
    # Context only: MAK and SAT share the ask and the print price, so they share the verdict.
    assert _cell(rows["SAT"], "data-chase-verdict") == _cell(rows["MAK"], "data-chase-verdict")


def test_a_sold_option_row_says_the_verdict_is_ask_based(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    assert _cell(rows["SAT"], "data-chase-sold") == CHASE_COPY["sold"]
    assert "data-chase-sold" not in rows["MAK"]


def test_geç_kaldın_becomes_the_mandatory_counter_argument(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    assert _cell(rows["GEC"], "data-counter") == "AMA kovalama hükmü: geç kaldın."
    # A row with no counter-argument names the chase check among what was looked at.
    checked = _cell(rows["MAK"], "data-checked")
    assert checked is not None
    assert "kovalama (hâlâ makul)" in checked


def test_the_chase_line_adds_no_forbidden_words_and_leaks_no_score(client: TestClient) -> None:
    body = client.get("/alfa", params={"gate": "off"}).text
    assert forbidden_words(html.unescape(body)) == []
    outside = _AUDIT.sub("", body)
    for spec in _ROWS:
        assert f"{spec.score:.2f}" not in outside, spec.ticker


def test_a_failed_since_print_read_leaves_the_context_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.board.alfa_page as page_module

    def _broken(engine: object, keys: object) -> Any:
        msg = "alfa_net_prem unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(page_module, "read_net_premium_since_many", _broken)
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    assert _cell(rows["MAK"], "data-chase-flow") == "akış baskıdan beri: bilinmiyor"
    assert _cell(rows["MAK"], "data-chase-verdict") == "hâlâ makul"  # the verdict still stands


def test_the_chase_render_makes_zero_unusual_whales_calls(
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
    assert "data-chase-line" in response.text
    assert calls == []
