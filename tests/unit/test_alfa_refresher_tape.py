"""Phase 5.2.A3: the refresher's tape and ticker-info steps (``webapp/board/refresher.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher",
daily jobs), §4.4 and §5 A3; decision P17 (one client, soft cap).

Pins:
  - ``run_quotes_cycle`` reports the board tickers in row order;
  - ``run_tape_cycle``: one ``net-prem-ticks`` call per distinct ticker, minutes
    written; the soft cap pauses it before or mid-step; a degraded fetch writes
    nothing; daily-limit and auth errors propagate;
  - ``run_ticker_info_job``: only tickers without a row fetched today; a
    not-found answer is stored and not refetched the same day; a degraded
    fetch is retried on the next cycle; the soft cap pauses it;
  - the loop runs quotes, depth, tape, then ticker info on one client, and
    fetches ticker info once per day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from webapp.board.db import make_engine
from webapp.board.netprem import ensure_netprem_tables, read_tape_summaries
from webapp.board.quotes import ensure_quotes_tables
from webapp.board.refresher import (
    SoftCap,
    board_refresh_loop,
    run_quotes_cycle,
    run_tape_cycle,
    run_ticker_info_job,
)
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.ticker_info import ensure_ticker_info_tables, read_ticker_infos

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_BOARD_PROFILE = _REPO / "profiles" / "board_v1.yaml"
_CALIBRATION = _REPO / "profiles" / "v5_default.yaml"
_SETTINGS = load_board_settings(_BOARD_PROFILE)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)  # Tuesday, inside RTH
_RUN = "live-2026-09-15"
_CAP = _SETTINGS.refresh.daily_request_soft_cap

# Trimmed from the live NVDA net-prem-ticks response (2026-09-15).
_TAPE: list[dict[str, Any]] = [
    {
        "date": "2026-09-15", "tape_time": "2026-09-15T15:28:00.000000Z", "net_call_premium": "2214509.00",
        "net_put_premium": "-515070.00", "net_call_volume": -987, "net_put_volume": -1038,
        "call_volume": 26560, "put_volume": 9547, "net_delta": "48197.79",
    },
    {
        "date": "2026-09-15", "tape_time": "2026-09-15T15:29:00.000000Z", "net_call_premium": "-862179.00",
        "net_put_premium": "-300077.00", "net_call_volume": -3389, "net_put_volume": -409,
        "call_volume": 14124, "put_volume": 9885, "net_delta": "-47013.29",
    },
]
_INFO: dict[str, dict[str, Any]] = {
    "SPY": {"symbol": "SPY", "sector": None, "issue_type": "ETF"},
    "SMCI": {"symbol": "SMCI", "sector": "Technology", "issue_type": "Common Stock"},
}
# Phase 5.2.B2b: the loop also runs the ATM straddle step.
_EXPIRIES: list[dict[str, Any]] = [
    {"expires": "2026-09-18", "open_interest": 531720, "volume": 25428, "chains": 150},
    {"expires": "2026-09-25", "open_interest": 42125, "volume": 6353, "chains": 124},
]
# Phase 5.2.B-jobs: the daily-job clock's pre-market and after-the-open jobs, in the order
# the registry runs them on the first tick of an ET day (oi_confirm needs no call here).
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
        self.calls: list[str] = []
        self.last_daily_request_count = count
        self._count_after_call = count_after_call
        self._errors = errors or {}
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)
        self.closed = False

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append(path)
        if self._count_after_call is not None:
            self.last_daily_request_count = self._count_after_call
        for suffix, error in self._errors.items():
            if path.endswith(suffix):
                raise error
        if path.endswith(("/option-contracts", "/flow", "/atm-chains")):
            return {"data": []}
        if path.endswith("/expiry-breakdown"):
            return {"data": list(_EXPIRIES)}
        if path.endswith("/net-prem-ticks"):
            return {"data": list(_TAPE)}
        if path.endswith("/info"):
            return {"data": _INFO[path.split("/")[3]]}
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
    specs = [("e1", "SPY", "760", "500000"), ("e2", "SMCI", "37", "200000"), ("e3", "SPY", "770", "50000")]
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
    engine = make_engine(url)
    ensure_quotes_tables(engine)
    ensure_netprem_tables(engine)
    ensure_ticker_info_tables(engine)
    return engine


async def test_quotes_cycle_reports_the_board_tickers_in_row_order(url: str) -> None:
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
    assert report.tickers == ("SPY", "SMCI")


async def test_tape_cycle_fetches_each_ticker_once_and_writes_minutes(url: str) -> None:
    engine = _engine(url)
    client = _FakeClient(count=5000)
    try:
        report = await run_tape_cycle(
            client, engine, tickers=["SPY", "smci", "SPY", " "], soft_cap=SoftCap(_CAP), clock=lambda: _NOW,  # type: ignore[arg-type]
        )
        summaries = read_tape_summaries(engine, [("SPY", _NOW.date()), ("SMCI", _NOW.date())])
    finally:
        engine.dispose()
    assert client.calls == ["/api/stock/SPY/net-prem-ticks", "/api/stock/SMCI/net-prem-ticks"]
    assert (report.requests, report.written, report.degraded_fetches, report.skipped_non_critical) == (2, 4, 0, False)
    assert summaries[("SPY", _NOW.date())].minutes == 2
    assert summaries[("SPY", _NOW.date())].fetched_at == _NOW


async def test_tape_cycle_honours_the_soft_cap(url: str) -> None:
    engine = _engine(url)
    try:
        reached = SoftCap(_CAP)
        reached.observe(_CAP, _NOW)
        paused = _FakeClient()
        before = await run_tape_cycle(paused, engine, tickers=["SPY", "SMCI"], soft_cap=reached, clock=lambda: _NOW)  # type: ignore[arg-type]
        mid = _FakeClient(count_after_call=_CAP)
        during = await run_tape_cycle(mid, engine, tickers=["SPY", "SMCI"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
    finally:
        engine.dispose()
    assert (paused.calls, before.requests, before.skipped_non_critical) == ([], 0, True)
    assert (mid.calls, during.requests, during.skipped_non_critical) == (["/api/stock/SPY/net-prem-ticks"], 1, True)


async def test_degraded_tape_writes_nothing(url: str) -> None:
    engine = _engine(url)
    client = _FakeClient(errors={"/net-prem-ticks": UnusualWhalesTransientError("returned HTTP 503")})
    try:
        report = await run_tape_cycle(client, engine, tickers=["SPY"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
        summaries = read_tape_summaries(engine, [("SPY", _NOW.date())])
    finally:
        engine.dispose()
    assert (report.requests, report.written, report.degraded_fetches) == (1, 0, 1)
    assert summaries == {}


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), id="daily-limit"),
        pytest.param(UnusualWhalesAuthError("returned HTTP 401"), id="auth"),
    ],
)
async def test_tape_and_info_steps_propagate_daily_limit_and_auth(url: str, error: Exception) -> None:
    engine = _engine(url)
    try:
        with pytest.raises(type(error)):
            await run_tape_cycle(
                _FakeClient(errors={"/net-prem-ticks": error}), engine,  # type: ignore[arg-type]
                tickers=["SPY"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW,
            )
        with pytest.raises(type(error)):
            await run_ticker_info_job(
                _FakeClient(errors={"/info": error}), engine,  # type: ignore[arg-type]
                tickers=["SPY"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW,
            )
    finally:
        engine.dispose()


async def test_ticker_info_job_fetches_each_ticker_once_a_day(url: str) -> None:
    engine = _engine(url)
    try:
        first = _FakeClient()
        report = await run_ticker_info_job(first, engine, tickers=["SPY", "SMCI"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
        same_day = _FakeClient()
        again = await run_ticker_info_job(same_day, engine, tickers=["SMCI", "SPY"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
        next_day = _FakeClient()
        tomorrow = _NOW + timedelta(days=1)
        await run_ticker_info_job(next_day, engine, tickers=["SPY"], soft_cap=SoftCap(_CAP), clock=lambda: tomorrow)  # type: ignore[arg-type]
        infos = read_ticker_infos(engine, ["SPY", "SMCI"])
    finally:
        engine.dispose()
    assert first.calls == ["/api/stock/SPY/info", "/api/stock/SMCI/info"]
    assert (report.requests, report.written) == (2, 2)
    assert (same_day.calls, again.requests) == ([], 0)
    assert next_day.calls == ["/api/stock/SPY/info"]
    assert infos["SPY"].fund_or_index is True
    assert (infos["SMCI"].issue_type, infos["SMCI"].sector) == ("Common Stock", "Technology")


async def test_ticker_info_not_found_is_stored_and_degraded_is_retried(url: str) -> None:
    engine = _engine(url)
    try:
        missing = _FakeClient(errors={"/SPY/info": UnusualWhalesNotFoundError("HTTP 404", status_code=404)})
        await run_ticker_info_job(missing, engine, tickers=["SPY"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
        degraded = _FakeClient(errors={"/SMCI/info": UnusualWhalesTransientError("returned HTTP 503")})
        report = await run_ticker_info_job(degraded, engine, tickers=["SPY", "SMCI"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
        retry = _FakeClient()
        await run_ticker_info_job(retry, engine, tickers=["SPY", "SMCI"], soft_cap=SoftCap(_CAP), clock=lambda: _NOW)  # type: ignore[arg-type]
        infos = read_ticker_infos(engine, ["SPY", "SMCI"])
    finally:
        engine.dispose()
    assert (report.requests, report.written, report.degraded_fetches) == (1, 0, 1)
    assert degraded.calls == ["/api/stock/SMCI/info"]  # SPY's not-found row is not refetched today
    assert retry.calls == ["/api/stock/SMCI/info"]
    assert (infos["SPY"].issue_type, infos["SPY"].fund_or_index) == (None, False)


async def test_ticker_info_job_honours_the_soft_cap(url: str) -> None:
    engine = _engine(url)
    try:
        reached = SoftCap(_CAP)
        reached.observe(_CAP + 1, _NOW)
        client = _FakeClient()
        report = await run_ticker_info_job(client, engine, tickers=["SPY"], soft_cap=reached, clock=lambda: _NOW)  # type: ignore[arg-type]
    finally:
        engine.dispose()
    assert (client.calls, report.requests, report.skipped_non_critical) == ([], 0, True)


class _Stop(BaseException):
    """Ends the loop from the injected sleep."""


async def test_loop_runs_quotes_depth_tape_then_ticker_info_on_one_client(url: str) -> None:
    _seed(url)
    client = _FakeClient()
    made: list[object] = []
    slept: list[float] = []

    def _factory(uw_settings: object) -> _FakeClient:
        made.append(uw_settings)
        return client

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 2:
            raise _Stop

    with pytest.raises(_Stop):
        await board_refresh_loop(
            database_url=url, profile_path=_CALIBRATION, board_profile_path=_BOARD_PROFILE,
            client_factory=_factory,  # type: ignore[arg-type]
            journal_factory=lambda _url: _Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: _NOW, sleep=_sleep,
        )

    # Phase 5.2.B2b (D10): the ATM straddle step runs between depth and the tape, and the
    # expiry list is fetched once per ET day, just before that day's first atm-chains call.
    cycle = [
        "/api/stock/SPY/option-contracts",
        "/api/stock/SMCI/option-contracts",
        "/api/option-contract/SPY260918C00760000/flow",
        "/api/option-contract/SMCI260918C00037000/flow",
        "/api/stock/SPY/atm-chains",
        "/api/stock/SMCI/atm-chains",
        "/api/stock/SPY/net-prem-ticks",
        "/api/stock/SMCI/net-prem-ticks",
    ]
    expiry_list = ["/api/stock/SPY/expiry-breakdown", "/api/stock/SMCI/expiry-breakdown"]
    # Phase 5.2.B-jobs (D10): the daily-job clock runs before the RTH cycle, once per ET day.
    # Phase 5.2.B5b (D10): the regime band closes every cycle with its seven calls.
    assert client.calls == [
        *_DAILY_JOBS,
        *cycle[:4], *expiry_list, *cycle[4:],
        "/api/stock/SPY/info", "/api/stock/SMCI/info",
        *_REGIME,
        *cycle, *_REGIME,
    ]
    assert len(made) == 1
    assert client.closed is True
    engine = make_engine(url)
    try:
        assert read_tape_summaries(engine, [("SMCI", _NOW.date())])[("SMCI", _NOW.date())].minutes == 2
        assert set(read_ticker_infos(engine, ["SPY", "SMCI"])) == {"SPY", "SMCI"}
    finally:
        engine.dispose()
