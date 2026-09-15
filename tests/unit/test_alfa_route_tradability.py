"""Phase 5.2.A2: ``GET /alfa`` renders the tradability chip and the cost gate.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CO1/R-CO2, §4.1
(no UW calls at render), §5 A2 ("Row face", "Gate"); decisions P15, P16.

Signals are seeded through ``SqliteBacktestStore`` into a tmp sqlite file;
quotes and depth go straight into ``alfa_quote``/``alfa_contract_depth``, as
the refresher writes them.

Pins:
  - gate on (the default): sections main → ``kotasyon yok`` → ``İŞLENMEZ``,
    each chip with its state, reason (``BBB %17 makas``), cost cells, quote
    age and ``(varsayılan değer)``;
  - ``?gate=off``: one list with every row and its chip;
  - rows are never hidden; the forms keep the gate state and the run;
  - ``canlı alınabilir fiyat`` never appears and the chip shows no mid;
  - confirmed owner values drop the default marker;
  - a failed quote read is flagged and every chip reads ``kotasyon yok``;
  - zero Unusual Whales calls with quotes present, in both gate modes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.copy_tr import GATE_LABEL
from webapp.board.db import make_engine
from webapp.board.quotes import (
    DepthSnapshot,
    QuoteSnapshot,
    ensure_quotes_tables,
    upsert_depths,
    upsert_quotes,
)
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi.testclient import TestClient

_RUN = "live-2026-09-15"
_SIGNAL_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_SECTION = re.compile(r'data-section="(\w+)"')
_ROW = re.compile(r'data-row data-ticker="([^"]+)"')

# ticker, strike, premium, chain, quote (bid, ask) or None
_BOARD = [
    ("AAA", "100", "300000", "AAA260918C00100000", (0.39, 0.41)),  # İŞLENİR (5.0%)
    ("BBB", "50", "200000", "BBB260918C00050000", (1.83, 2.17)),  # İŞLENMEZ (17%)
    ("CCC", "20", "100000", "CCC260918C00020000", None),  # kotasyon yok
    ("DDD", "10", "50000", "DDD260918C00010000", (0.97, 1.03)),  # DAR (6%)
]


def _seed(url: str) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(_BOARD) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (ticker, strike, premium, _chain, _quote) in enumerate(_BOARD):
            pr = build_print(
                event_id=f"t{i}", ts=_SIGNAL_TS + timedelta(seconds=i), ticker=ticker,
                option_type="call", strike=strike, dte=3, premium=premium,
            )
            store.add(
                EnrichedEvent(print=pr, combined_score_post_penalty=0.7777),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()

    now = datetime.now(UTC)
    engine = make_engine(url)
    try:
        ensure_telemetry_tables(engine)
        ensure_quotes_tables(engine)
        with Session(engine) as session:
            for i, (ticker, strike, _premium, chain, _quote) in enumerate(_BOARD):
                session.add(
                    AlfaPrintMeta(
                        run_id=_RUN, event_id=f"t{i}", ticker=ticker, option_chain=chain,
                        fill_side="at_ask", option_type="call", strike=strike,
                        expiry=(_SIGNAL_TS + timedelta(days=3)).date(),
                        print_ts=_SIGNAL_TS, written_at=_SIGNAL_TS,
                    ),
                )
            session.commit()
        upsert_quotes(
            engine,
            [
                QuoteSnapshot(
                    option_symbol=chain, ticker=ticker, nbbo_bid=quote[0], nbbo_ask=quote[1],
                    last_price=quote[1], volume=500, open_interest=1000,
                    last_tape_time=now - timedelta(minutes=4), returned=True,
                )
                for ticker, _strike, _premium, chain, quote in _BOARD
                if quote is not None
            ],
            fetched_at=now - timedelta(seconds=41),
        )
        upsert_depths(
            engine,
            [
                DepthSnapshot(
                    option_symbol="AAA260918C00100000", nbbo_bid_size=137, nbbo_ask_size=108,
                    nbbo_bid_time=now - timedelta(minutes=4), nbbo_ask_time=now - timedelta(minutes=4),
                ),
            ],
            fetched_at=now - timedelta(seconds=60),
        )
    finally:
        engine.dispose()


def _reset(m: Any) -> None:
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = f"sqlite:///{tmp_path / 'board.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    import webapp.main as m

    _reset(m)
    _seed(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _section_html(body: str, key: str) -> str:
    marker = f'data-section="{key}"'
    start = body.index(marker)
    following = body.find('data-section="', start + len(marker))
    return body[start: following if following != -1 else len(body)]


def _row_html(body: str, ticker: str) -> str:
    marker = f'data-ticker="{ticker}"'
    for chunk in body.split("<article")[1:]:
        if marker in chunk:
            return chunk.split("</article>")[0]
    raise AssertionError(f"no row for {ticker}")


def _chip_html(row_html: str) -> str:
    start = row_html.index("data-chip=")
    return row_html[start: row_html.index('<div class="grid', start)]


def test_gate_on_by_default_sections_and_chips(board: TestClient) -> None:
    body = board.get("/alfa").text

    assert GATE_LABEL in body
    assert 'data-gate="on"' in body
    assert _SECTION.findall(body) == ["main", "no_quote", "untradable"]
    assert _ROW.findall(_section_html(body, "main")) == ["AAA", "DDD"]
    assert _ROW.findall(_section_html(body, "no_quote")) == ["CCC"]
    assert _ROW.findall(_section_html(body, "untradable")) == ["BBB"]

    aaa = _row_html(body, "AAA")
    assert 'data-chip="tradable"' in aaa
    assert "İŞLENİR" in aaa
    assert "data-spread>%5<" in aaa
    assert "bid $0.39 / ask $0.41" in aaa
    assert "data-round-trip>$3.30<" in aaa
    assert "$41 · sermayenin %0.41 kadarı" in aaa
    assert "137 kontrat (son işlem anında)" in aaa
    assert re.search(r"kotasyon \d+ sn önce alındı", aaa)
    assert re.search(r"son işlem \d+ dk önce", aaa)
    assert aaa.count("(varsayılan değer)") == 2  # next to the cost and the size cell

    bbb = _row_html(body, "BBB")
    assert 'data-chip="untradable"' in bbb
    assert "İŞLENMEZ" in bbb
    assert "BBB %17 makas" in bbb

    ddd = _row_html(body, "DDD")
    assert 'data-chip="narrow"' in ddd
    assert "DDD %6 makas" in ddd

    ccc = _row_html(body, "CCC")
    assert 'data-chip="no_quote"' in ccc
    assert "CCC için kotasyon kaydı yok" in ccc


def test_gate_off_lists_every_row_with_its_chip(board: TestClient) -> None:
    body = board.get("/alfa", params={"gate": "off"}).text
    assert 'data-gate="off"' in body
    assert _SECTION.findall(body) == ["all"]
    assert _ROW.findall(body) == ["AAA", "BBB", "CCC", "DDD"]
    assert body.count("data-chip=") == 4


def test_rows_are_never_hidden(board: TestClient) -> None:
    for params in ({}, {"gate": "off"}):
        body = board.get("/alfa", params=params).text
        assert sorted(_ROW.findall(body)) == ["AAA", "BBB", "CCC", "DDD"]


def test_forms_keep_the_gate_state_and_the_run(board: TestClient) -> None:
    on = board.get("/alfa").text
    gate_form = on[on.index('data-form="gate"'):]
    gate_form = gate_form[: gate_form.index("</form>")]
    assert f'name="run" value="{_RUN}"' in gate_form
    assert 'name="gate" value="off"' in gate_form

    off = board.get("/alfa", params={"gate": "off"}).text
    run_form = off[off.index('data-form="run"'):]
    run_form = run_form[: run_form.index("</form>")]
    assert 'name="gate" value="off"' in run_form


def test_no_live_price_wording_and_no_mid_on_the_chip(board: TestClient) -> None:
    body = board.get("/alfa").text
    assert "canlı alınabilir fiyat" not in body
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        chip = _chip_html(_row_html(body, ticker)).lower()
        assert "mid" not in chip
        assert "orta fiyat" not in chip
    assert "0.7777" not in body


def test_confirmed_owner_values_drop_the_default_marker(
    board: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.main as m

    settings = m._board_settings()
    confirmed = settings.model_copy(
        update={"sizing": settings.sizing.model_copy(update={"values_confirmed_by_owner": True})},
    )
    monkeypatch.setattr(m, "_board_settings", lambda: confirmed)
    body = board.get("/alfa").text
    assert "data-row" in body
    assert "(varsayılan değer)" not in body


def test_failed_quote_read_is_flagged_and_never_clean(
    board: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.board.alfa_page as page_module

    def _broken(engine: object, symbols: object) -> Any:
        msg = "quote tables unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(page_module, "read_board_quotes", _broken)
    body = board.get("/alfa").text
    assert 'data-state="quotes-failed"' in body
    assert body.count('data-chip="no_quote"') == 4
    assert 'data-chip="tradable"' not in body


def test_gate_page_makes_zero_unusual_whales_calls(
    board: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"GET /alfa must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    for params in ({}, {"gate": "off"}):
        response = board.get("/alfa", params=params)
        assert response.status_code == 200
        assert 'data-chip="tradable"' in response.text
    assert calls == []
