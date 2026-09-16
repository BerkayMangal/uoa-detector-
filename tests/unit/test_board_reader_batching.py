"""Phase 5.2.B-fix7: the render path's board readers are batched, not per row.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Render budget: p95
<= 1.5 s at 2,000 signals in the run").

Review finding RB-02: FAZ B took the page model from 6 database statements to
168 by adding three N+1 loops - one net-premium aggregate per row, one
``session.get`` per T+1 confirmation key and two catalyst queries per ticker.
Against sqlite the budget test cannot see it; on Railway Postgres each of those
statements is a network round trip.

Pins:
  - each of the three readers issues a constant number of statements, whatever
    the number of keys;
  - the batched results equal the per-key readers' results, key by key;
  - the page model's statement count does not grow with the number of rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from webapp.board import alfa_page
from webapp.board.atm import ensure_atm_tables
from webapp.board.catalysts import (
    AlfaCatalyst,
    AlfaCatalystFetch,
    ensure_catalyst_tables,
    read_board_catalysts,
    read_catalyst_chip,
)
from webapp.board.db import make_engine
from webapp.board.netprem import (
    TapeMinute,
    ensure_netprem_tables,
    read_net_premium_since,
    read_net_premium_since_many,
    upsert_tape,
)
from webapp.board.oi_confirm import (
    AlfaOiConfirm,
    ensure_oi_confirm_tables,
    load_oi_confirm,
    read_board_oi,
)
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardSignalReader

from tests.unit.test_board_honesty import _CONFIRMING, _Row, _seed

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = alfa_page.load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = _TS + timedelta(hours=1)
_TRADE_DATE = _TS.date()
_EXPIRY = date(2026, 9, 18)
_TICKERS = tuple(f"BT{i}" for i in range(8))
_ROWS = tuple(
    _Row(f"b{i}", ticker, "call", "at_ask", str(500000 - i * 1000), 0.40 + i / 100,
         _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41))
    for i, ticker in enumerate(_TICKERS)
)


class _Statements:
    """Counts the statements one engine executes inside the ``with`` block."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.count = 0

    def _hook(self, *_args: Any) -> None:
        self.count += 1

    def __enter__(self) -> _Statements:
        event.listen(self.engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._hook)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'batch.db'}"
    _seed(url, _ROWS, gamma_row=False)
    engine = make_engine(url)
    for ensure in (ensure_atm_tables, ensure_oi_confirm_tables, ensure_catalyst_tables,
                   ensure_netprem_tables):
        ensure(engine)
    with Session(engine) as session:
        for i, ticker in enumerate(_TICKERS):
            session.add(AlfaOiConfirm(
                option_symbol=f"{ticker}260918C00100000", trade_date=_TRADE_DATE, ticker=ticker,
                option_type="call", strike=100.0, expiry=_EXPIRY, flagged_size=40,
                status="acilis", created_at=_NOW, oi_t=97, oi_t1=140,
                t1_date=date(2026, 9, 16), delta_oi=43,
            ))
            for source in ("earnings", "fda"):
                session.add(AlfaCatalystFetch(
                    source=source, ticker=ticker, last_attempt_at=_NOW, last_status="ok",
                    last_success_at=_NOW,
                ))
            session.add(AlfaCatalyst(
                ticker=ticker, kind="earnings", when_key="2026-09-17", title="Q3",
                starts_at=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
                ends_at=datetime(2026, 9, 18, 4, 0, tzinfo=UTC), precision="day",
                timing="postmarket", estimated=False, detail=None, fetched_at=_NOW,
            ))
            del i
        session.add(AlfaCatalystFetch(
            source="macro", ticker="*", last_attempt_at=_NOW, last_status="ok",
            last_success_at=_NOW,
        ))
        session.commit()
    for ticker in _TICKERS:
        upsert_tape(
            engine, ticker,
            [
                TapeMinute(
                    trade_date=_TRADE_DATE, tape_time=_TS + timedelta(minutes=m),
                    net_call_premium=1000.0, net_put_premium=100.0, net_call_volume=None,
                    net_put_volume=None, call_volume=None, put_volume=None, net_delta=None,
                )
                for m in range(30)
            ],
            fetched_at=_NOW,
        )
    # The readers create their tables once per engine; do that before counting.
    read_board_oi(engine, [(f"{_TICKERS[0]}260918C00100000", _TRADE_DATE)])
    read_board_catalysts(engine, [(_TICKERS[0], _NOW)], settings=_SETTINGS, now=_NOW)
    read_net_premium_since_many(engine, [(_TICKERS[0], _TRADE_DATE, _TS)])
    yield engine
    engine.dispose()


def test_read_board_oi_is_one_query_for_every_key(engine: Engine) -> None:
    keys = [(f"{t}260918C00100000", _TRADE_DATE) for t in _TICKERS]
    with _Statements(engine) as counter:
        batched = read_board_oi(engine, keys)
    assert counter.count == 1
    with Session(engine) as session:
        one_by_one = {
            key: load_oi_confirm(session, *key) for key in keys
        }
    assert {k: v.status for k, v in batched.items()} == {
        k: v.status for k, v in one_by_one.items() if v is not None
    }
    assert len(batched) == len(_TICKERS)


def test_read_net_premium_since_many_is_one_query_for_every_key(engine: Engine) -> None:
    keys = [
        (ticker, _TRADE_DATE, _TS + timedelta(minutes=i))
        for i, ticker in enumerate(_TICKERS)
    ]
    with _Statements(engine) as counter:
        batched = read_net_premium_since_many(engine, keys)
    assert counter.count == 1
    for ticker, trade_date, since in keys:
        alone = read_net_premium_since(engine, ticker, trade_date, since)
        assert alone is not None
        summary = batched[(ticker, trade_date, since)]
        assert summary.minutes == alone.minutes
        assert summary.net_call_premium == pytest.approx(alone.net_call_premium)
        assert summary.net_put_premium == pytest.approx(alone.net_put_premium)
        assert summary.first_tape_time == alone.first_tape_time
        assert summary.last_tape_time == alone.last_tape_time


def test_read_board_catalysts_loads_every_ticker_in_two_queries(engine: Engine) -> None:
    windows = [(ticker, _NOW + timedelta(days=3)) for ticker in _TICKERS]
    with _Statements(engine) as counter:
        chips = read_board_catalysts(engine, windows, settings=_SETTINGS, now=_NOW)
    assert counter.count == 2  # the events and the fetch coverage, once each
    with Session(engine) as session:
        for ticker, end in windows:
            alone = read_catalyst_chip(
                session, ticker=ticker, window_start=_NOW, window_end=end, settings=_SETTINGS,
            )
            assert chips[(ticker, end)].text == alone.text
            assert chips[(ticker, end)].in_window == alone.in_window


def test_the_page_model_statement_count_does_not_grow_with_rows(engine: Engine) -> None:
    reader = BoardSignalReader(engine=engine)
    prints = reader.load_run(_RUN)
    few = [p for p in prints if p.signal.ticker in _TICKERS[:2]]
    many = [p for p in prints if p.signal.ticker in _TICKERS]
    assert len(few) == 2 and len(many) == len(_TICKERS)

    def _statements(rows: list[Any]) -> int:
        with _Statements(engine) as counter:
            page = alfa_page.build_alfa_page(
                rows, _SETTINGS, now=_NOW, spread_cutoff_pct=_CUTOFF,
                quote_source=alfa_page.db_quote_source(engine),
                evidence_source=alfa_page.db_evidence_source(engine),
                atm_source=alfa_page.db_atm_source(engine),
                flow_since_source=alfa_page.db_flow_since_source(engine),
                oi_source=alfa_page.db_oi_source(engine),
                catalyst_source=alfa_page.db_catalyst_source(engine, _SETTINGS),
                holdings_source=alfa_page.db_holdings_source(engine),
                sector_source=alfa_page.db_sector_source(engine),
            )
        assert len(page.views) == len(rows)
        return counter.count

    _statements(few)  # warm the one-per-engine table checks
    assert _statements(many) == _statements(few)
