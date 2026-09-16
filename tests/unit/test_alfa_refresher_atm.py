"""Phase 5.2.B2b: the refresher's ATM straddle step (``webapp/board/refresher.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher"),
§4.4 (ATM straddle: ``atm-chains`` every 300 s plus ``expiry-breakdown`` once a
day, about 790 requests/day at 10 tickers) and §6 B2.

Fixtures are trimmed from the live SPY/SMCI ``expiry-breakdown`` and
``atm-chains`` responses captured 2026-09-15.

Pins:
  - the cycle reports each row's dominant-contract expiry, in board order;
  - ``expiry-breakdown`` runs once per ET day and survives a restart, because
    the stored ``alfa_atm_expiry`` rows decide whether it is due;
  - ``atm-chains`` runs every cycle and asks for the dominant expiry first;
  - stored rows are what the render path reads;
  - the soft cap pauses the step before the breakdown and again before the
    chains;
  - daily-limit and key failures propagate; another 4xx degrades the step;
  - with no board rows the step makes no call;
  - in the loop the step sits between depth and the tape, and its failure is
    isolated from the tape and ticker-info steps;
  - the exact per-cycle request count.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import webapp.board.refresher as refresher_module
from sqlalchemy import inspect
from webapp.board.atm import ensure_atm_tables, load_listed_expiries, read_board_atm
from webapp.board.db import make_engine, session_factory
from webapp.board.netprem import ensure_netprem_tables
from webapp.board.quotes import ensure_quotes_tables
from webapp.board.refresher import (
    SoftCap,
    board_refresh_loop,
    grouped_expiries,
    run_atm_cycle,
    run_quotes_cycle,
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
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
)

_REPO = Path(__file__).resolve().parents[2]
_BOARD_PROFILE = _REPO / "profiles" / "board_v1.yaml"
_CALIBRATION = _REPO / "profiles" / "v5_default.yaml"
_SETTINGS = load_board_settings(_BOARD_PROFILE)
_CAP = _SETTINGS.refresh.daily_request_soft_cap
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)  # Tuesday, inside RTH; ET date 2026-09-15
_RUN = "live-2026-09-15"
_EXPIRY = date(2026, 9, 18)

_EXPIRIES: list[dict[str, Any]] = [
    {"expires": "2026-09-18", "open_interest": 531720, "volume": 25428, "chains": 150},
    {"expires": "2026-09-25", "open_interest": 42125, "volume": 6353, "chains": 124},
    {"expires": "2026-10-02", "open_interest": 27903, "volume": 1537, "chains": 94},
]
_CHAINS: dict[str, list[dict[str, Any]]] = {
    "SPY": [
        {"option_symbol": "SPY260918C00757000", "bid": "6.94", "ask": "6.97",
         "iv": "0.1434521360211295", "stock_price": "757.42", "tape_time": "2026-09-15T15:29:58Z",
         "date": "2026-09-15"},
        {"option_symbol": "SPY260918P00757000", "bid": "6.10", "ask": "6.13",
         "iv": "0.1448794138487157", "stock_price": "757.42", "tape_time": "2026-09-15T15:29:54Z",
         "date": "2026-09-15"},
    ],
    "SMCI": [
        {"option_symbol": "SMCI260918C00036500", "bid": "1.14", "ask": "1.18",
         "iv": "0.818124754691178", "stock_price": "36.695", "tape_time": "2026-09-15T15:29:58Z",
         "date": "2026-09-15"},
        {"option_symbol": "SMCI260918P00036500", "bid": "0.97", "ask": "1.02",
         "iv": "0.825296790656841", "stock_price": "36.695", "tape_time": "2026-09-15T15:29:54Z",
         "date": "2026-09-15"},
    ],
}


# Phase 5.2.B-jobs: the daily-job clock's calls on the first tick of an ET day, in order.
_DAILY_JOBS = [
    "/api/earnings/SPY", "/api/earnings/SMCI",
    "/api/market/fda-calendar", "/api/market/fda-calendar", "/api/market/economic-calendar",
    "/api/stock/SPY/greek-exposure", "/api/stock/QQQ/greek-exposure",
]
_DAILY_JOB_PATHS: frozenset[str] = frozenset(_DAILY_JOBS)
# Phase 5.2.B5b: the regime band's seven calls, in the order the cycle makes them.
_REGIME = [
    "/api/market/market-tide",
    "/api/stock/SPY/spot-exposures", "/api/stock/QQQ/spot-exposures",
    "/api/stock/SPY/gex-levels", "/api/stock/QQQ/gex-levels",
    "/api/stock/SPY/volatility/term-structure", "/api/stock/VIX/volatility/term-structure",
]
_REGIME_PATHS: frozenset[str] = frozenset(_REGIME)


class _FakeClient:
    def __init__(
        self,
        *,
        count: int | None = None,
        count_after_call: int | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.last_daily_request_count = count
        self._count_after_call = count_after_call
        self._errors = errors or {}
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)
        self.closed = False

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        if self._count_after_call is not None:
            self.last_daily_request_count = self._count_after_call
        for suffix, error in self._errors.items():
            if path.endswith(suffix):
                raise error
        if path.endswith("/expiry-breakdown"):
            return {"data": list(_EXPIRIES)}
        if path.endswith("/atm-chains"):
            return {"data": list(_CHAINS.get(path.split("/")[3], []))}
        if path.endswith(("/option-contracts", "/flow", "/net-prem-ticks")):
            return {"data": []}
        if path.endswith("/info"):
            return {"data": {"issue_type": "ETF", "sector": None}}
        if path in _DAILY_JOB_PATHS or path in _REGIME_PATHS or path.startswith("/api/earnings/"):
            return {"data": []}
        raise AssertionError(path)

    async def aclose(self) -> None:
        self.closed = True


class _Journal:
    def list(self, status: str | None = None) -> list[Any]:
        return []


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'board.db'}"


def _seed(url: str) -> None:
    specs = [("e1", "SPY", "757", "500000"), ("e2", "SMCI", "36.5", "200000")]
    store = SqliteBacktestStore(url, flush_threshold=len(specs) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (eid, ticker, strike, premium) in enumerate(specs):
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=eid, ts=_NOW - timedelta(minutes=30, seconds=-i), ticker=ticker,
                        option_type="call", strike=strike, dte=3, premium=premium,
                    ),
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()


def _engine(url: str) -> Any:
    """The tables the loop creates at startup, before its first cycle."""
    engine = make_engine(url)
    ensure_quotes_tables(engine)
    ensure_netprem_tables(engine)
    ensure_ticker_info_tables(engine)
    ensure_atm_tables(engine)
    return engine


def _paths(client: _FakeClient, suffix: str) -> list[str]:
    return [path for path, _params in client.calls if path.endswith(suffix)]


async def _atm(
    engine: Any,
    client: _FakeClient,
    *,
    wanted: dict[str, list[date]] | None = None,
    settings: BoardSettings = _SETTINGS,
    soft_cap: SoftCap | None = None,
    now: datetime = _NOW,
) -> Any:
    return await run_atm_cycle(
        client, engine, settings,  # type: ignore[arg-type]
        wanted=wanted if wanted is not None else {"SPY": [_EXPIRY], "SMCI": [_EXPIRY]},
        soft_cap=soft_cap or SoftCap(_CAP),
        clock=lambda: now,
    )


# ---------------------------------------------------------------------------
# What the cycle asks for
# ---------------------------------------------------------------------------


async def test_quotes_cycle_reports_the_dominant_expiries_in_board_order(url: str) -> None:
    _seed(url)
    engine = _engine(url)
    try:
        report = await run_quotes_cycle(
            _FakeClient(), engine, _SETTINGS,  # type: ignore[arg-type]
            reader=BoardSignalReader(engine=engine), journal=_Journal(),
            soft_cap=SoftCap(_CAP), clock=lambda: _NOW,
        )
    finally:
        engine.dispose()
    assert report.wanted_expiries == (("SPY", _EXPIRY), ("SMCI", _EXPIRY))
    assert grouped_expiries(report.wanted_expiries) == {"SPY": [_EXPIRY], "SMCI": [_EXPIRY]}


def test_grouped_expiries_keeps_board_order_and_drops_repeats() -> None:
    later = date(2026, 9, 25)
    pairs = (("SPY", _EXPIRY), ("SMCI", later), ("SPY", later), ("SPY", _EXPIRY))
    grouped = grouped_expiries(pairs)
    assert list(grouped) == ["SPY", "SMCI"]
    assert grouped["SPY"] == [_EXPIRY, later]
    assert grouped["SMCI"] == [later]


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


async def test_the_expiry_list_is_fetched_once_per_et_day(url: str) -> None:
    engine = _engine(url)
    try:
        first = _FakeClient()
        report = await _atm(engine, first)
        same_day = _FakeClient()
        await _atm(engine, same_day, now=_NOW + timedelta(hours=2))
        next_day = _FakeClient()
        await _atm(engine, next_day, now=_NOW + timedelta(days=1))
        sessions = session_factory(engine)
        with sessions() as session:
            listed = load_listed_expiries(session, "SPY")
    finally:
        engine.dispose()
    assert _paths(first, "/expiry-breakdown") == [
        "/api/stock/SPY/expiry-breakdown", "/api/stock/SMCI/expiry-breakdown",
    ]
    assert _paths(first, "/atm-chains") == [
        "/api/stock/SPY/atm-chains", "/api/stock/SMCI/atm-chains",
    ]
    assert report.requests == 4
    assert _paths(same_day, "/expiry-breakdown") == []  # the stored list survives the cycle
    assert len(_paths(same_day, "/atm-chains")) == 2
    assert len(_paths(next_day, "/expiry-breakdown")) == 2  # a new ET day asks again
    assert listed == (date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 2))


async def test_atm_chains_asks_for_the_dominant_expiry_first(url: str) -> None:
    one_slot = _SETTINGS.model_copy(
        update={"refresh": _SETTINGS.refresh.model_copy(update={"atm_expiries": 1})},
    )
    engine = _engine(url)
    client = _FakeClient()
    try:
        await _atm(
            engine, client, wanted={"SPY": [date(2026, 10, 2)]}, settings=one_slot,
        )
    finally:
        engine.dispose()
    (params,) = [p for path, p in client.calls if path.endswith("/atm-chains")]
    assert params == {"expirations[]": ["2026-10-02"]}  # the dominant expiry takes the only slot


async def test_stored_rows_are_what_the_render_path_reads(url: str) -> None:
    engine = _engine(url)
    try:
        report = await _atm(engine, _FakeClient())
        rows = read_board_atm(engine, ["SPY", "SMCI", "NVDA"])
    finally:
        engine.dispose()
    assert report.written == 2
    assert set(rows) == {"SPY", "SMCI"}
    (spy,) = rows["SPY"]
    assert (spy.expiry, spy.strike, spy.stock_price) == (_EXPIRY, 757.0, 757.42)
    assert (spy.call_bid, spy.call_ask, spy.put_bid, spy.put_ask) == (6.94, 6.97, 6.10, 6.13)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


async def test_the_soft_cap_pauses_the_step_before_and_between_the_fetches(url: str) -> None:
    engine = _engine(url)
    try:
        reached = SoftCap(_CAP)
        reached.observe(_CAP, _NOW)
        paused = _FakeClient()
        before = await _atm(engine, paused, soft_cap=reached)

        mid = _FakeClient(count_after_call=_CAP)
        during = await _atm(engine, mid, soft_cap=SoftCap(_CAP))
    finally:
        engine.dispose()
    assert (paused.calls, before.requests, before.skipped_non_critical) == ([], 0, True)
    assert _paths(mid, "/expiry-breakdown") == [
        "/api/stock/SPY/expiry-breakdown", "/api/stock/SMCI/expiry-breakdown",
    ]
    assert _paths(mid, "/atm-chains") == []  # the cap was reached before the chains
    assert (during.requests, during.skipped_non_critical) == (2, True)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), id="daily-limit"),
        pytest.param(UnusualWhalesAuthError("returned HTTP 401"), id="auth"),
    ],
)
async def test_daily_limit_and_key_failures_propagate(url: str, error: Exception) -> None:
    engine = _engine(url)
    try:
        with pytest.raises(type(error)):
            await _atm(engine, _FakeClient(errors={"/expiry-breakdown": error}))
    finally:
        engine.dispose()


async def test_another_4xx_degrades_the_step_and_the_cycle_continues(
    url: str, caplog: pytest.LogCaptureFixture,
) -> None:
    bad = UnusualWhalesAuthError("UnusualWhales GET /api/stock/SPY/atm-chains returned HTTP 400: bad symbol")
    engine = _engine(url)
    try:
        with caplog.at_level(logging.WARNING, logger="webapp.board.refresher"):
            report = await _atm(engine, _FakeClient(errors={"/atm-chains": bad}))
    finally:
        engine.dispose()
    assert (report.degraded_fetches, report.written) == (1, 0)
    assert report.requests == 2  # the expiry list still landed
    assert "marked degraded" in caplog.text


async def test_without_board_rows_the_step_makes_no_call(url: str) -> None:
    engine = _engine(url)
    client = _FakeClient()
    try:
        report = await _atm(engine, client, wanted={})
    finally:
        engine.dispose()
    assert (client.calls, report.requests, report.skipped_non_critical) == ([], 0, False)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class _Stop(BaseException):
    """Ends the loop from the injected sleep."""


async def _run_loop(url: str, client: _FakeClient, *, max_sleeps: int = 1) -> None:
    async def _sleep(_seconds: float) -> None:
        raise _Stop

    slept = 0

    async def _counting_sleep(_seconds: float) -> None:
        nonlocal slept
        slept += 1
        if slept >= max_sleeps:
            raise _Stop

    with pytest.raises(_Stop):
        await board_refresh_loop(
            database_url=url, profile_path=_CALIBRATION, board_profile_path=_BOARD_PROFILE,
            client_factory=lambda _s: client,  # type: ignore[arg-type,return-value]
            journal_factory=lambda _u: _Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: _NOW, sleep=_counting_sleep if max_sleeps > 1 else _sleep,
        )


async def test_the_loop_creates_the_atm_tables_at_startup(url: str) -> None:
    """A fresh database: the loop creates alfa_atm and alfa_atm_expiry before its first cycle."""
    _seed(url)
    await _run_loop(url, _FakeClient())
    engine = make_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"alfa_atm", "alfa_atm_expiry"} <= names


async def test_the_loop_runs_atm_between_depth_and_the_tape(url: str) -> None:
    _seed(url)
    client = _FakeClient()
    await _run_loop(url, client)
    # Phase 5.2.B-jobs (D10): the daily-job clock runs before the RTH cycle, once per ET day.
    assert [path for path, _p in client.calls] == [
        *_DAILY_JOBS,
        "/api/stock/SPY/option-contracts",
        "/api/stock/SMCI/option-contracts",
        "/api/option-contract/SPY260918C00757000/flow",
        "/api/option-contract/SMCI260918C00036500/flow",
        "/api/stock/SPY/expiry-breakdown",
        "/api/stock/SMCI/expiry-breakdown",
        "/api/stock/SPY/atm-chains",
        "/api/stock/SMCI/atm-chains",
        "/api/stock/SPY/net-prem-ticks",
        "/api/stock/SMCI/net-prem-ticks",
        "/api/stock/SPY/info",
        "/api/stock/SMCI/info",
        # Phase 5.2.B5b (D10): the regime band closes the cycle.
        *_REGIME,
    ]


async def test_a_failing_atm_step_does_not_skip_the_tape(
    url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url)

    async def _broken(*args: object, **kwargs: object) -> object:
        msg = "alfa_atm unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(refresher_module, "run_atm_cycle", _broken)
    client = _FakeClient()
    with caplog.at_level(logging.ERROR, logger="webapp.board.refresher"):
        await _run_loop(url, client)
    assert "board refresher cycle failed at step atm" in caplog.text
    assert len(_paths(client, "/net-prem-ticks")) == 2
    assert len(_paths(client, "/info")) == 2


async def test_uw_calls_per_cycle_stay_inside_the_atm_budget(url: str) -> None:
    """Steady state per cycle: N quotes + K depth + N atm-chains + N tape.

    The expiry list and ticker info are once-a-day, so the first cycle of a day
    adds 2N. Contract section 4.4 budgets the ATM straddle at about 790 requests
    a day: 78 RTH cycles x 10 tickers plus 10 breakdown calls.
    """
    _seed(url)
    client = _FakeClient()
    await _run_loop(url, client, max_sleeps=3)
    tickers = 2
    depth = 2  # one dominant contract per row, inside exit_depth_top_k
    # Phase 5.2.B5b (D10): the regime band adds seven per-cycle calls, whatever the ticker count.
    steady = tickers + depth + tickers + tickers + len(_REGIME)
    # Phase 5.2.B-jobs (D10): plus the daily-job clock's once-a-day calls on the first tick.
    assert len(client.calls) == 3 * steady + 2 * tickers + len(_DAILY_JOBS)
    assert len(_paths(client, "/atm-chains")) == 3 * tickers
    assert len(_paths(client, "/expiry-breakdown")) == tickers
