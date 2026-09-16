"""Phase 5.2.B6b: portfolio overlap on the rendered board (contract §6 B6).

The seeded sqlite run of ``test_board_honesty`` is reused, with two open journal
trades (one of them expired), one focused-ETF holdings snapshot and the sectors
of the board tickers.

Pins:
  - the capital header states the open premium at risk in dollars and percent,
    and carries ``(varsayılan değer)`` while the owner values are unconfirmed;
  - ``zaten bu bahittesin`` renders only on the row whose ticker and side-aware
    direction match an open trade, with that trade beside it;
  - an expired open trade renders ``(vadesi geçti)`` and is never closed;
  - the single-bet strip names the strong ETF cluster with its holdings date,
    and the weak same-sector link separately, never merged into the cluster;
  - a failed journal read renders no capital header at all, rather than "$0";
  - the strip never enters the evidence counts, the strength label, the
    clean-candidate rule or the counter-argument choice;
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
from webapp.board.alfa_page import build_alfa_page
from webapp.board.db import make_engine
from webapp.board.etf_holdings import AlfaEtfHolding, ensure_etf_holding_tables
from webapp.board.honesty import forbidden_words
from webapp.board.settings import load_board_settings
from webapp.board.ticker_info import TickerInfoSnapshot, upsert_ticker_infos
from webapp.journal import JournalRepo

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_UPDATED = date(2026, 9, 11)

_ROWS = (
    _Row("p1", "NVDA", "call", "at_ask", "300000", 0.4321, _CONFIRMING, quote=(0.39, 0.41)),
    _Row("p2", "AMD", "call", "at_ask", "200000", 0.5432, _CONFIRMING, quote=(0.39, 0.41)),
    _Row("p3", "SPY", "call", "at_ask", "100000", 0.6543, _CONFIRMING, quote=(0.39, 0.41)),
)
# SMH holdings: NVDA and AMD clear portfolio.cluster_min_member_weight_pct (3%), INTC does not.
_HOLDINGS = [("NVDA", 22.09), ("TSM", 9.75), ("AVGO", 5.83), ("AMD", 5.78), ("INTC", 2.10)]


def _seed_portfolio(url: str) -> None:
    repo = JournalRepo(url)
    repo.add(
        entry_ts=_TS - timedelta(days=5), ticker="NVDA", direction="bullish", instrument="call",
        contracts=2.0, entry_price=4.10, strike=220.0, expiry="2026-10-16", thesis="",
    )
    repo.add(  # production holds trades opened in June; an expired one is shown, never closed
        entry_ts=_TS - timedelta(days=80), ticker="AMD", direction="bullish", instrument="call",
        contracts=1.0, entry_price=2.00, strike=170.0, expiry="2026-06-25", thesis="",
    )
    engine = make_engine(url)
    try:
        ensure_etf_holding_tables(engine)
        with Session(engine) as session:
            for ticker, weight in _HOLDINGS:
                session.add(AlfaEtfHolding(
                    etf="SMH", ticker=ticker, updated=_UPDATED, weight_pct=weight,
                    sector="Technology", short_name=ticker, fetched_at=_TS,
                ))
            session.commit()
        upsert_ticker_infos(
            engine,
            [
                TickerInfoSnapshot("NVDA", "Common Stock", "Technology"),
                TickerInfoSnapshot("AMD", "Common Stock", "Technology"),
                TickerInfoSnapshot("SPY", "ETF", None),
            ],
            fetched_at=_TS,
        )
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'portfolio.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _reset(m)
    _seed(url, _ROWS, gamma_row=False)
    _seed_portfolio(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _cell(body: str, attribute: str) -> str | None:
    found = re.search(rf"{attribute}>([^<]*)<", body)
    return None if found is None else html.unescape(found.group(1))


def _strip(body: str) -> str:
    found = re.search(r"<section[^>]*data-portfolio>(.*?)</section>", body, re.S)
    assert found is not None, "the portfolio strip did not render"
    return found.group(1)


# ---------------------------------------------------------------------------
# Capital header
# ---------------------------------------------------------------------------


def test_the_capital_header_states_the_open_premium_at_risk(client: TestClient) -> None:
    strip = _strip(client.get("/").text)
    # Phase 5.2.B-fix2 (D10, review FB-H2): 2 x $4.10 x 100 = $820 of a $10,000 default
    # capital. The AMD leg expired on 25.06.2026, so its $200 entry premium is no longer
    # counted as premium at risk; the header discloses it instead of dropping it.
    assert _cell(strip, "data-capital") == (
        "Açıktaki prim riski: $820 · sermayenin %8.2 (varsayılan değer)"
        " · 1 işlemin vadesi geçti ($200 hariç)"
    )


def test_confirmed_owner_values_drop_the_default_marker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.main as m

    confirmed = _SETTINGS.model_copy(
        update={"sizing": _SETTINGS.sizing.model_copy(update={"values_confirmed_by_owner": True})},
    )
    monkeypatch.setattr(m, "_board_settings", lambda: confirmed)
    body = client.get("/").text
    # Phase 5.2.B-fix2 (D10, review FB-H2): the expired AMD leg left the at-risk sum.
    assert _cell(_strip(body), "data-capital") == (
        "Açıktaki prim riski: $820 · sermayenin %8.2 · 1 işlemin vadesi geçti ($200 hariç)"
    )
    assert "(varsayılan değer)" not in body


def test_a_failed_journal_read_renders_no_capital_header() -> None:
    def _broken() -> Any:
        msg = "trade table unavailable"
        raise RuntimeError(msg)

    page = build_alfa_page([], _SETTINGS, now=_NOW, trades_source=_broken)
    assert page.capital is None  # "$0 at risk" would be a claim, not an unknown


# ---------------------------------------------------------------------------
# The badge
# ---------------------------------------------------------------------------


def test_the_badge_renders_only_on_the_matching_row(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _cell(rows["NVDA"], "data-overlap-badge") == "zaten bu bahittesin"
    assert _cell(rows["AMD"], "data-overlap-badge") == "zaten bu bahittesin"
    assert "data-overlap-badge" not in rows["SPY"]


def test_the_matching_trade_is_shown_and_an_expired_one_is_marked(client: TestClient) -> None:
    rows = _rows(client.get("/", params={"gate": "off"}).text)
    assert _cell(rows["NVDA"], "data-overlap-detail") == (
        "Eşleşen açık işlem: NVDA call 220 16.10.2026 · 2 kontrat @ $4.10"
    )
    amd = _cell(rows["AMD"], "data-overlap-detail")
    assert amd is not None
    assert "(vadesi geçti)" in amd
    assert amd.startswith("Eşleşen açık işlem: AMD call 170 25.06.2026 (vadesi geçti)")


# ---------------------------------------------------------------------------
# The single-bet strip
# ---------------------------------------------------------------------------


def test_the_strong_cluster_names_its_etf_and_holdings_date(client: TestClient) -> None:
    strip = _strip(client.get("/").text)
    assert _cell(strip, 'data-cluster="SMH"') == (
        "Tek bahis: AMD, NVDA (SMH 11.09.2026 tarihli; her biri ≥ %3)"
    )


def test_the_weak_sector_link_is_shown_but_never_merged(client: TestClient) -> None:
    strip = _strip(client.get("/").text)
    assert _cell(strip, "data-sector-link") == (
        "Aynı sektör (zayıf bağ, kümeye katılmaz): AMD, NVDA · Technology"
    )
    # The weak link is its own line: it never joins the cluster's member list.
    cluster = _cell(strip, 'data-cluster="SMH"')
    assert cluster is not None and "sektör" not in cluster


# ---------------------------------------------------------------------------
# Isolation and honesty
# ---------------------------------------------------------------------------


def test_the_strip_renders_outside_every_row_and_adds_no_family(client: TestClient) -> None:
    body = client.get("/").text
    assert body.index("data-portfolio>") < body.index("<article")
    assert "data-family" not in _strip(body)
    for article in body.split("<article")[1:]:
        assert "data-portfolio>" not in article.split("</article>")[0]


def test_the_overlap_never_changes_a_row_reading(client: TestClient) -> None:
    import webapp.main as m

    with_journal = _rows(client.get("/", params={"gate": "off"}).text)
    # The same board with no journal, holdings or sector source behind it.
    monkeypatch_free = build_alfa_page([], m._board_settings(), now=_NOW)
    assert monkeypatch_free.capital is None
    for ticker, row in with_journal.items():
        word = re.search(r"data-evidence-word>([^<]*)<", row)
        assert word is not None, ticker
        # The six counted families only: dealer gamma, dark pool and sector lehte, price
        # confirmation nötr, Akış and Açık pozisyon bilinmiyor. The strip adds none of them.
        assert html.unescape(word.group(1)) == "3 lehte · 0 aleyhte · 2 bilinmiyor", ticker
        assert 'data-strength="moderate">Orta<' in row, ticker
        counter = re.search(r"<p data-counter>([^<]*)</p>", row)
        assert counter is not None and "bahittesin" not in html.unescape(counter.group(1)), ticker


def test_the_strip_adds_no_forbidden_words(client: TestClient) -> None:
    for gate in ({}, {"gate": "off"}):
        body = client.get("/", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


def test_the_portfolio_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a board render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    response = client.get("/")
    assert response.status_code == 200
    assert "data-capital" in response.text
    assert calls == []
