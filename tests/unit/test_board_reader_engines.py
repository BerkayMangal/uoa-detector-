"""Phase 5.2.B-fix8: every render-path reader survives a fresh database, and once per engine.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Page renders read
Postgres only") and §4.2 (board tables are created, never altered).

Review findings RB-04 and RB-05:
  - ``netprem.read_net_premium_since_many`` and ``ticker_info.read_ticker_infos``
    raised ``OperationalError`` on a database whose board tables do not exist
    yet, unlike the six readers that create their own. build_alfa_page caught it,
    so the page degraded - but every render logged a traceback, permanently on
    any deploy without LIVE_TICKERS, since the refresher is what creates them.
  - the "checked once" guard was a one-slot list, so two Engines for the same
    URL (the web app's and the refresher's, which live in one process)
    alternating through it re-issued catalog DDL on every single call.

Pins:
  - each of the eight render-path sources returns cleanly on a brand-new
    database and creates what it reads;
  - alternating two engines checks each once, then never again.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import event, inspect
from webapp.board import alfa_page
from webapp.board.db import make_engine
from webapp.board.evidence import EvidenceRequest, read_evidence_inputs
from webapp.board.oi_confirm import read_board_oi
from webapp.board.settings import load_board_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_NOW = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
_DAY = date(2026, 9, 15)
_SYMBOL = "SPY260918C00700000"


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    yield engine
    engine.dispose()


def _sources(engine: Engine) -> dict[str, Callable[[], Any]]:
    return {
        "atm": lambda: alfa_page.db_atm_source(engine)(["SPY"]),
        "flow_since": lambda: alfa_page.db_flow_since_source(engine)([("SPY", _DAY, _NOW)]),
        "oi": lambda: alfa_page.db_oi_source(engine)([(_SYMBOL, _DAY)]),
        "catalyst": lambda: alfa_page.db_catalyst_source(engine, _SETTINGS)([("SPY", _NOW)], _NOW),
        "regime": lambda: alfa_page.db_regime_source(engine)(_NOW),
        "holdings": lambda: alfa_page.db_holdings_source(engine)(),
        "sector": lambda: alfa_page.db_sector_source(engine)(["SPY"]),
        "quotes": lambda: alfa_page.db_quote_source(engine)([_SYMBOL]),
    }


@pytest.mark.parametrize("name", list(_sources(make_engine("sqlite://"))))
def test_every_render_source_reads_a_fresh_database(fresh: Engine, name: str) -> None:
    # Before the fix, "flow_since" and "sector" raised OperationalError here.
    assert _sources(fresh)[name]() is not None


def test_the_tables_the_readers_need_are_created_on_first_read(fresh: Engine) -> None:
    for call in _sources(fresh).values():
        call()
    names = set(inspect(fresh).get_table_names())
    assert {"alfa_net_prem", "alfa_ticker_info", "alfa_quote", "alfa_atm"} <= names


class _Statements:
    """Records the statements one engine executes inside the ``with`` block."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sql: list[str] = []

    def _hook(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.sql.append(statement)

    def __enter__(self) -> _Statements:
        event.listen(self.engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._hook)

    @property
    def probed_tables(self) -> bool:
        """sqlite's table-existence check, which the one-per-engine guard should skip."""
        return any("table_info" in sql or "sqlite_master" in sql for sql in self.sql)


def test_two_alternating_engines_are_each_checked_once(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'two.db'}"
    web = make_engine(url)
    refresher = make_engine(url)  # the refresher makes its own engine for the same database
    try:
        keys = [(_SYMBOL, _DAY)]
        read_board_oi(web, keys)
        read_board_oi(refresher, keys)
        for engine in (web, refresher, web, refresher):
            with _Statements(engine) as counter:
                read_board_oi(engine, keys)
            assert counter.probed_tables is False, "the table check repeated"
            assert len(counter.sql) == 1
    finally:
        web.dispose()
        refresher.dispose()


def test_the_evidence_reader_also_settles_for_both_engines(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'ev.db'}"
    web = make_engine(url)
    refresher = make_engine(url)
    try:
        requests = [EvidenceRequest(event_id="e1", ticker="SPY", trade_date=_DAY)]
        read_evidence_inputs(web, "live-2026-09-15", requests)
        read_evidence_inputs(refresher, "live-2026-09-15", requests)
        for engine in (web, refresher, web, refresher):
            with _Statements(engine) as counter:
                read_evidence_inputs(engine, "live-2026-09-15", requests)
            assert counter.probed_tables is False, "the table check repeated"
    finally:
        web.dispose()
        refresher.dispose()
