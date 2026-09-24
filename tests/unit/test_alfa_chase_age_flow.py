"""Phase 5.2.B-fix9: the chase cell shows its quote's age, and a measured flat flow says so.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CO2 ("Every quote
shows its age") and §6 B3 (the chase line and its flow context); decision P8
(measured is not unknown).

Review findings RB-03 and FB-06:
  - the chase cell printed a live-looking executable price (``şimdi $1.60
    (ask)``) with no age next to it, while the size and move cells added in the
    same FAZ both show theirs;
  - a measured net premium of exactly zero reported ``akış baskıdan beri:
    bilinmiyor``, the same string a missing tape row produces, folding a
    measured reading into unknown.

Pins:
  - the chase cell carries the same quote age the chip does, under its own
    namespaced attribute;
  - a measured zero reads its own frozen label, a missing row still reads
    bilinmiyor, and neither moves the verdict;
  - the three contract labels are unchanged, byte for byte.
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
from webapp.board.chase import FLOW_LABELS, VERDICT_LABELS, flow_context
from webapp.board.db import make_engine
from webapp.board.honesty import ensure_clean, forbidden_words
from webapp.board.netprem import TapeSummary

from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _CONFIRMING, _reset, _Row, _rows, _seed
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_EXPIRY = date(2026, 9, 18)
_SPOT_NOW = 199.19

# FLT: a tape that sums to exactly zero. SUR: a tape pointing the row's way. NOR: no tape row.
_ROWS = (
    _Row("f1", "FLT", "call", "at_ask", "300000", 0.4211, _CONFIRMING,
         tape=0.0, quote=(1.58, 1.60)),
    _Row("f2", "SUR", "call", "at_ask", "200000", 0.5322, _CONFIRMING,
         tape=400_000.0, quote=(1.58, 1.60)),
    _Row("f3", "NOR", "call", "at_ask", "100000", 0.6433, _CONFIRMING, quote=(1.58, 1.60)),
)


def _tape(net: float) -> TapeSummary:
    return TapeSummary(
        ticker="AAA", trade_date=_TS.date(), net_call_premium=net, net_put_premium=0.0,
        minutes=30, first_tape_time=_TS, last_tape_time=_NOW, fetched_at=_NOW,
    )


def _seed_atm(url: str) -> None:
    engine = make_engine(url)
    try:
        ensure_atm_tables(engine)
        with Session(engine) as session:
            for row in _ROWS:
                session.add(AlfaAtm(
                    ticker=row.ticker, expiry=_EXPIRY, strike=100.0, stock_price=_SPOT_NOW,
                    call_bid=1.00, call_ask=1.10, call_iv=0.40,
                    put_bid=0.90, put_ask=1.00, put_iv=0.40,
                    trade_date=_TS.date(), fetched_at=_NOW - timedelta(seconds=41),
                ))
            session.commit()
    finally:
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    import webapp.main as m

    url = f"sqlite:///{tmp_path / 'flow.db'}"
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


# ---------------------------------------------------------------------------
# R-CO2 on the chase cell (RB-03)
# ---------------------------------------------------------------------------


def test_the_chase_cell_shows_the_age_of_the_quote_it_prices(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["SUR"]
    line = _cell(row, "data-chase-line")
    assert line is not None and "şimdi $1.60 (ask)" in line
    age = _cell(row, "data-chase-quote-age")
    assert age is not None
    assert re.fullmatch(r"kotasyon \d+ sn önce alındı", age), age


def test_the_chip_attributes_still_resolve_to_the_chip(client: TestClient) -> None:
    row = _rows(client.get("/alfa", params={"gate": "off"}).text)["SUR"]
    # The chase age is namespaced, so a first-match regex for the chip's own age is unaffected.
    first = re.search(r"data-quote-age>([^<]*)<", row)
    assert first is not None
    assert html.unescape(first.group(1)).startswith("kotasyon ")
    assert row.index("data-quote-age>") < row.index("data-chase-quote-age>")


# ---------------------------------------------------------------------------
# A measured zero is not unknown (FB-06, decision P8)
# ---------------------------------------------------------------------------


def test_a_measured_zero_flow_has_its_own_reading() -> None:
    assert flow_context(_tape(0.0), "up") == "flat"
    assert flow_context(None, "up") == "unknown"
    assert FLOW_LABELS["flat"] == "akış baskıdan beri: ölçüldü, yön göstermiyor"
    assert FLOW_LABELS["unknown"] == "akış baskıdan beri: bilinmiyor"


def test_the_three_contract_labels_are_unchanged() -> None:
    assert FLOW_LABELS["continuing"] == "akış baskıdan beri sürüyor"
    assert FLOW_LABELS["reversed"] == "akış baskıdan beri döndü"
    assert FLOW_LABELS["unknown"] == "akış baskıdan beri: bilinmiyor"


def test_the_rows_tell_a_flat_tape_from_a_missing_one(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    assert _cell(rows["FLT"], "data-chase-flow") == "akış baskıdan beri: ölçüldü, yön göstermiyor"
    assert _cell(rows["NOR"], "data-chase-flow") == "akış baskıdan beri: bilinmiyor"
    assert _cell(rows["SUR"], "data-chase-flow") == "akış baskıdan beri sürüyor"


def test_the_flow_reading_never_moves_the_verdict(client: TestClient) -> None:
    rows = _rows(client.get("/alfa", params={"gate": "off"}).text)
    verdicts = {ticker: _cell(row, "data-chase-verdict") for ticker, row in rows.items()}
    assert set(verdicts.values()) == {VERDICT_LABELS["reasonable"]}


def test_the_new_flow_label_is_clean(client: TestClient) -> None:
    assert ensure_clean(FLOW_LABELS["flat"]) == FLOW_LABELS["flat"]
    for gate in ({}, {"gate": "off"}):
        body = client.get("/alfa", params=gate).text
        assert forbidden_words(html.unescape(body)) == []


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
    assert "data-chase-quote-age" in response.text
    assert calls == []
