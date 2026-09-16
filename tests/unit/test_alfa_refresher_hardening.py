"""Phase 5.2.A-fix2: board refresher hardening (review RT-1, RT-2, RT-3, RT-5).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher",
"Page renders read Postgres only"), §4.4 (request budget) and §5 A2.

Pins:
  - RT-1: quote and depth upserts are one statement per batch, whatever the
    contract count; the cycle's synchronous database work runs in worker
    threads, off the shared event loop; the PostgreSQL statement is an
    ``INSERT ... ON CONFLICT DO UPDATE``; the generic path (other dialects)
    inserts and updates correctly;
  - RT-2: the refresher targets the live run holding today's prints, found by
    print time, never ``live-<calendar date>``: day-2 prints under the day-1
    run id are refreshed, a run whose newest print is on an earlier day is
    not, and a non-live run is ignored;
  - RT-3: a 4xx other than 401/403 on one call degrades that ticker or
    contract only; quotes, depth, tape and ticker info are isolated steps; a
    key failure (401/403) still propagates;
  - RT-5: UW calls per cycle stay inside the budget formula
    tickers (quotes) + extra chunks + K (depth) + tickers (tape), plus tickers
    (info) on the first cycle of the day.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import webapp.board.quotes as quotes_module
import webapp.board.refresher as refresher_module
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from webapp.board.db import make_engine
from webapp.board.netprem import ensure_netprem_tables
from webapp.board.quotes import (
    AlfaQuote,
    QuoteSnapshot,
    ensure_quotes_tables,
    read_quotes,
    upsert_quotes,
)
from webapp.board.refresher import (
    SoftCap,
    board_refresh_loop,
    current_live_run,
    is_key_failure,
    run_quotes_cycle,
    run_tape_cycle,
    run_ticker_info_job,
)
from webapp.board.settings import BoardSettings, load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.ticker_info import ensure_ticker_info_tables

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesAuthError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_BOARD_PROFILE = _REPO / "profiles" / "board_v1.yaml"
_CALIBRATION = _REPO / "profiles" / "v5_default.yaml"
_SETTINGS = load_board_settings(_BOARD_PROFILE)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)  # Tuesday, inside RTH
_DAY2 = datetime(2026, 9, 16, 15, 30, tzinfo=UTC)


def _settings(**refresh: object) -> BoardSettings:
    return _SETTINGS.model_copy(update={"refresh": _SETTINGS.refresh.model_copy(update=refresh)})


def _seed(url: str, run_id: str, contracts: Sequence[tuple[str, int]], *, ts: datetime) -> None:
    """One print per contract: (ticker, strike) pairs, calls expiring in 3 days, legacy rows."""
    store = SqliteBacktestStore(url, flush_threshold=len(contracts) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=run_id)
        for i, (ticker, strike) in enumerate(contracts):
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=f"{run_id}-{i}", ts=ts + timedelta(seconds=i), ticker=ticker,
                        option_type="call", strike=str(strike), dte=3, premium=str(100000 + i),
                    ),
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()


class _Client:
    """Quotes every requested symbol; serves flow, tape and info; optional errors by path suffix."""

    def __init__(self, errors: dict[str, BaseException] | None = None) -> None:
        self.calls: list[str] = []
        self.last_daily_request_count: int | None = None
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)
        self._errors = errors or {}

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append(path)
        for suffix, error in self._errors.items():
            if path.endswith(suffix):
                raise error
        if path.endswith("/option-contracts"):
            return {
                "data": [
                    {
                        "option_symbol": s, "nbbo_bid": "1.00", "nbbo_ask": "1.04", "last_price": "1.02",
                        "volume": 10, "open_interest": 100, "last_tape_time": "2026-09-15T15:20:00Z",
                    }
                    for s in (params or {})["option_symbol[]"]
                ],
            }
        if path.endswith("/flow"):
            return {
                "data": [
                    {
                        "nbbo_bid_size": 20, "nbbo_ask_size": 30,
                        "nbbo_bid_time": "2026-09-15T15:20:00Z", "nbbo_ask_time": "2026-09-15T15:20:00Z",
                    },
                ],
            }
        if path.endswith("/net-prem-ticks"):
            return {
                "data": [
                    {
                        "date": "2026-09-15", "tape_time": "2026-09-15T15:29:00Z", "net_call_premium": "1000.0",
                        "net_put_premium": "-500.0", "net_call_volume": 1, "net_put_volume": 1,
                        "call_volume": 2, "put_volume": 2, "net_delta": "3.5",
                    },
                ],
            }
        if path.endswith("/info"):
            return {"data": {"issue_type": "Common Stock", "sector": "Technology"}}
        raise AssertionError(path)

    async def aclose(self) -> None:
        return None


class _Journal:
    def list(self, status: str | None = None) -> list[Any]:
        return []


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'board.db'}"


def _engine(url: str) -> Engine:
    engine = make_engine(url)
    ensure_quotes_tables(engine)
    ensure_netprem_tables(engine)
    ensure_ticker_info_tables(engine)
    return engine


async def _cycle(url: str, client: _Client, *, settings: BoardSettings = _SETTINGS, now: datetime = _NOW) -> Any:
    engine = _engine(url)
    try:
        return await run_quotes_cycle(
            client, engine, settings,  # type: ignore[arg-type]
            reader=BoardSignalReader(engine=engine), journal=_Journal(),
            soft_cap=SoftCap(settings.refresh.daily_request_soft_cap), clock=lambda: now,
        )
    finally:
        engine.dispose()


def _paths(client: _Client, suffix: str) -> list[str]:
    return [p for p in client.calls if p.endswith(suffix)]


# ---------------------------------------------------------------------------
# RT-1
# ---------------------------------------------------------------------------


async def test_quote_and_depth_writes_are_one_statement_per_batch(url: str) -> None:
    contracts = [("SPY", 400 + i) for i in range(60)] + [("SMCI", 30 + i) for i in range(5)]
    _seed(url, "live-2026-09-15", contracts, ts=_NOW - timedelta(hours=1))
    settings = _settings(max_symbols_per_request=25, exit_depth_top_k=2)
    engine = _engine(url)
    statements: list[str] = []

    def _record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    client = _Client()
    try:
        report = await run_quotes_cycle(
            client, engine, settings,  # type: ignore[arg-type]
            reader=BoardSignalReader(engine=engine), journal=_Journal(),
            soft_cap=SoftCap(settings.refresh.daily_request_soft_cap), clock=lambda: _NOW,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        engine.dispose()

    quote_calls = len(_paths(client, "/option-contracts"))
    depth_calls = len(_paths(client, "/flow"))
    assert (quote_calls, depth_calls) == (3 + 1, 2)  # SPY 25/25/10, SMCI 5
    assert report.quotes_written == len(contracts)
    quote_statements = [s for s in statements if "alfa_quote" in s]
    depth_statements = [s for s in statements if "alfa_contract_depth" in s]
    assert len(quote_statements) == quote_calls
    assert len(depth_statements) == depth_calls
    assert all(s.lstrip().upper().startswith("INSERT") for s in quote_statements + depth_statements)


async def test_cycle_database_work_runs_off_the_event_loop(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700), ("SMCI", 37)], ts=_NOW - timedelta(hours=1))
    loop_thread = threading.get_ident()
    threads: dict[str, set[int]] = {}

    def _spy(name: str) -> None:
        real = getattr(refresher_module, name)

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            threads.setdefault(name, set()).add(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(refresher_module, name, _wrapped)

    for name in ("_load_board", "upsert_quotes", "board_order", "upsert_depths", "upsert_tape",
                 "tickers_needing_info", "upsert_ticker_infos"):
        _spy(name)

    engine = _engine(url)
    client = _Client()
    try:
        report = await run_quotes_cycle(
            client, engine, _SETTINGS,  # type: ignore[arg-type]
            reader=BoardSignalReader(engine=engine), journal=_Journal(),
            soft_cap=SoftCap(_SETTINGS.refresh.daily_request_soft_cap), clock=lambda: _NOW,
        )
        await run_tape_cycle(client, engine, tickers=report.tickers, soft_cap=SoftCap(10**6), clock=lambda: _NOW)  # type: ignore[arg-type]
        await run_ticker_info_job(client, engine, tickers=report.tickers, soft_cap=SoftCap(10**6), clock=lambda: _NOW)  # type: ignore[arg-type]
    finally:
        engine.dispose()

    assert set(threads) == {
        "_load_board", "upsert_quotes", "board_order", "upsert_depths", "upsert_tape",
        "tickers_needing_info", "upsert_ticker_infos",
    }
    for name, idents in threads.items():
        assert loop_thread not in idents, name


def test_postgres_upsert_is_a_single_on_conflict_statement() -> None:
    rows: list[dict[str, object]] = [
        {"option_symbol": "SPY260918C00700000", "ticker": "SPY", "nbbo_bid": 1.0, "fetched_at": _NOW},
        {"option_symbol": "SPY260918C00701000", "ticker": "SPY", "nbbo_bid": 1.1, "fetched_at": _NOW},
    ]
    stmt = quotes_module._upsert_statement("postgresql", AlfaQuote.__table__, "option_symbol", rows)  # type: ignore[arg-type]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert sql.startswith("INSERT INTO alfa_quote")
    assert "ON CONFLICT (option_symbol) DO UPDATE SET" in sql
    assert "nbbo_bid = excluded.nbbo_bid" in sql
    assert "option_symbol = excluded.option_symbol" not in sql


def test_generic_dialect_path_inserts_then_updates(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quotes_module, "_upsert_statement", lambda *args: None)
    engine = _engine(url)
    try:
        first = QuoteSnapshot("SPY260918C00700000", "SPY", 1.0, 1.1, 1.05, 10, 100, None, True)
        upsert_quotes(engine, [first], fetched_at=_NOW - timedelta(minutes=5))
        revised = QuoteSnapshot("SPY260918C00700000", "SPY", 1.2, 1.3, 1.25, 12, 100, None, True)
        added = QuoteSnapshot("SPY260918C00701000", "SPY", None, None, None, 0, 5, None, True)
        assert upsert_quotes(engine, [revised, added], fetched_at=_NOW) == 2
        stored = read_quotes(engine, ["SPY260918C00700000", "SPY260918C00701000"])
    finally:
        engine.dispose()
    assert (stored["SPY260918C00700000"].nbbo_bid, stored["SPY260918C00700000"].fetched_at) == (1.2, _NOW)
    assert stored["SPY260918C00701000"].nbbo_bid is None


# ---------------------------------------------------------------------------
# RT-2
# ---------------------------------------------------------------------------


async def test_day_two_prints_under_the_day_one_run_id_are_refreshed(url: str) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700), ("SMCI", 37)], ts=_DAY2 - timedelta(hours=1))
    client = _Client()
    report = await _cycle(url, client, now=_DAY2)
    assert report.run_id == "live-2026-09-15"
    assert set(report.tickers) == {"SPY", "SMCI"}
    assert len(_paths(client, "/option-contracts")) == 2
    assert report.requests > 0


async def test_a_run_whose_newest_print_is_on_an_earlier_day_is_not_refreshed(url: str) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700)], ts=_NOW - timedelta(hours=1))
    client = _Client()
    report = await _cycle(url, client, now=_DAY2)
    assert (report.run_id, report.tickers, report.requests) == (None, (), 0)
    assert client.calls == []


def test_newest_live_run_by_print_time_wins_and_other_runs_are_ignored(url: str) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700)], ts=_DAY2 - timedelta(hours=3))
    _seed(url, "live-2026-09-16", [("SPY", 701)], ts=_DAY2 - timedelta(hours=2))
    _seed(url, "seed", [("SPY", 702)], ts=_DAY2 - timedelta(minutes=5))
    engine = make_engine(url)
    try:
        assert current_live_run(engine, _DAY2) == "live-2026-09-16"
        # After the ET date changes, yesterday's run is no longer today's board.
        assert current_live_run(engine, _DAY2 + timedelta(days=1)) is None
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# RT-3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "key_failure"),
    [
        ("UnusualWhales GET /x returned HTTP 401: unauthorized", True),
        ("UnusualWhales GET /x returned HTTP 403: forbidden", True),
        ("returned HTTP 401", True),
        ("auth error without a status", True),
        ("UnusualWhales GET /x returned HTTP 400: bad symbol", False),
        ("UnusualWhales GET /x returned HTTP 409: conflict", False),
    ],
)
def test_key_failure_is_401_403_or_no_status(message: str, key_failure: bool) -> None:
    assert is_key_failure(UnusualWhalesAuthError(message)) is key_failure


async def test_a_400_on_one_ticker_degrades_only_that_ticker(url: str) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700), ("SMCI", 37)], ts=_NOW - timedelta(hours=1))
    bad = UnusualWhalesAuthError("UnusualWhales GET /api/stock/SPY/option-contracts returned HTTP 400: bad symbol")
    client = _Client(errors={"/SPY/option-contracts": bad})
    report = await _cycle(url, client)
    assert sorted(_paths(client, "/option-contracts")) == [
        "/api/stock/SMCI/option-contracts", "/api/stock/SPY/option-contracts",
    ]
    assert len(_paths(client, "/flow")) == 2
    assert (report.degraded_fetches, report.quotes_written, report.failed_steps) == (1, 1, ())

    engine = _engine(url)
    try:
        tape = await run_tape_cycle(
            _Client(errors={"/SPY/net-prem-ticks": bad}), engine,  # type: ignore[arg-type]
            tickers=["SPY", "SMCI"], soft_cap=SoftCap(10**6), clock=lambda: _NOW,
        )
        info = await run_ticker_info_job(
            _Client(errors={"/SPY/info": bad}), engine,  # type: ignore[arg-type]
            tickers=["SPY", "SMCI"], soft_cap=SoftCap(10**6), clock=lambda: _NOW,
        )
    finally:
        engine.dispose()
    assert (tape.requests, tape.degraded_fetches, tape.written) == (2, 1, 1)
    assert (info.requests, info.degraded_fetches, info.written) == (2, 1, 1)


async def test_a_401_still_propagates_from_the_per_ticker_loops(url: str) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700)], ts=_NOW - timedelta(hours=1))
    with pytest.raises(UnusualWhalesAuthError):
        await _cycle(url, _Client(errors={"/option-contracts": UnusualWhalesAuthError("returned HTTP 401")}))


async def test_a_failing_quotes_step_does_not_skip_depth(
    url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700), ("SMCI", 37)], ts=_NOW - timedelta(hours=1))

    def _broken(*args: object, **kwargs: object) -> int:
        msg = "alfa_quote write failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(refresher_module, "upsert_quotes", _broken)
    client = _Client()
    with caplog.at_level(logging.ERROR, logger="webapp.board.refresher"):
        report = await _cycle(url, client)
    assert report.failed_steps == ("quotes",)
    assert len(_paths(client, "/flow")) == 2
    assert "board refresher cycle failed at step quotes" in caplog.text


class _Stop(BaseException):
    """Ends the loop from the injected sleep."""


async def test_a_failing_tape_step_does_not_skip_ticker_info(
    url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url, "live-2026-09-15", [("SPY", 700), ("SMCI", 37)], ts=_NOW - timedelta(hours=1))

    async def _broken_tape(*args: object, **kwargs: object) -> object:
        msg = "alfa_net_prem unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(refresher_module, "run_tape_cycle", _broken_tape)
    client = _Client()

    async def _sleep(_seconds: float) -> None:
        raise _Stop

    with caplog.at_level(logging.ERROR, logger="webapp.board.refresher"), pytest.raises(_Stop):
        await board_refresh_loop(
            database_url=url, profile_path=_CALIBRATION, board_profile_path=_BOARD_PROFILE,
            client_factory=lambda _s: client,  # type: ignore[arg-type,return-value]
            journal_factory=lambda _u: _Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: _NOW, sleep=_sleep,
        )
    assert "board refresher cycle failed at step tape" in caplog.text
    assert len(_paths(client, "/info")) == 2


# ---------------------------------------------------------------------------
# RT-5
# ---------------------------------------------------------------------------


async def test_uw_calls_per_cycle_stay_inside_the_budget_formula(url: str) -> None:
    max_symbols = 20
    top_k = 3
    contracts = [("SPY", 400 + i) for i in range(45)] + [
        (ticker, 50 + i) for ticker in ("NVDA", "SMCI", "AMD") for i in range(4)
    ]
    _seed(url, "live-2026-09-15", contracts, ts=_NOW - timedelta(hours=1))
    settings = _settings(max_symbols_per_request=max_symbols, exit_depth_top_k=top_k)
    per_ticker: dict[str, int] = {}
    for ticker, _strike in contracts:
        per_ticker[ticker] = per_ticker.get(ticker, 0) + 1
    tickers = len(per_ticker)
    extra_chunks = sum(math.ceil(n / max_symbols) - 1 for n in per_ticker.values())

    engine = _engine(url)
    client = _Client()
    cycles: list[int] = []
    try:
        for _ in range(2):
            before = len(client.calls)
            report = await run_quotes_cycle(
                client, engine, settings,  # type: ignore[arg-type]
                reader=BoardSignalReader(engine=engine), journal=_Journal(),
                soft_cap=SoftCap(settings.refresh.daily_request_soft_cap), clock=lambda: _NOW,
            )
            await run_tape_cycle(client, engine, tickers=report.tickers, soft_cap=SoftCap(10**6), clock=lambda: _NOW)  # type: ignore[arg-type]
            await run_ticker_info_job(client, engine, tickers=report.tickers, soft_cap=SoftCap(10**6), clock=lambda: _NOW)  # type: ignore[arg-type]
            cycles.append(len(client.calls) - before)
    finally:
        engine.dispose()

    steady = tickers + extra_chunks + top_k + tickers
    assert cycles == [steady + tickers, steady]
