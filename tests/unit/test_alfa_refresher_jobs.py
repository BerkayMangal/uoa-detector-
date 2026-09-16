"""Phase 5.2.B-jobs: the refresher runs the daily jobs on its clock.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Daily jobs run in
the same refresher on profile clock times (ET)"), §4.4 (their daily request
rows) and §7 (scheduling of the FAZ D data jobs).

A recording fake client answers every daily-job endpoint, so each test can pin
the exact per-job request count.

Pins:
  - the first tick of an ET day runs the pre-market and after-the-open jobs,
    with their contract request counts, before the RTH cycle;
  - the previous session's dominant contract is flagged and confirmed against
    the T+1 open interest;
  - a second tick, and a restart on the same database, repeat nothing;
  - the post-close jobs run outside RTH, where no market cycle runs at all;
  - outside RTH the loop wakes for the next due job instead of sleeping past
    it, capped by ``refresh.closed_market_sleep_seconds``;
  - a weekend tick runs no job;
  - the daily limit backs the loop off; a key failure logs ERROR and waits one
    cadence; a job's own error is isolated from the market cycle.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import daily_jobs as dj
from webapp.board.db import make_engine, session_factory
from webapp.board.oi_confirm import load_oi_confirm
from webapp.board.refresher import board_refresh_loop
from webapp.board.settings import load_board_settings

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

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_BOARD_PROFILE = _REPO / "profiles" / "board_v1.yaml"
_CALIBRATION = _REPO / "profiles" / "v5_default.yaml"
_SETTINGS = load_board_settings(_BOARD_PROFILE)
_CADENCE = _SETTINGS.refresh.cadence_seconds
_CLOSED_SLEEP = _SETTINGS.refresh.closed_market_sleep_seconds

_TODAY = date(2026, 9, 15)  # Tuesday
_RTH = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)  # 11:30 ET, inside RTH
_POST_CLOSE = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)  # 17:05 ET, closed
_BEFORE_OPEN = datetime(2026, 9, 15, 11, 13, tzinfo=UTC)  # 07:13 ET, two minutes early
_SATURDAY = datetime(2026, 9, 19, 15, 30, tzinfo=UTC)
_RUN = "live-2026-09-15"
_MONDAY_RUN = "live-2026-09-14"
_MONDAY = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)

# The flagged SPY contract of the Monday run: 15000 premium at $1.50 is 100 contracts,
# so a 103-contract open-interest rise clears confirm_open_min_ratio (0.5 x 100).
_FLAGGED = "SPY260917C00757000"
_HISTORIC = {"chains": [
    {"date": "2026-09-15", "open_interest": 200, "volume": 180},
    {"date": "2026-09-14", "open_interest": 97, "volume": 140},
]}


class _FakeClient:
    """Answers every endpoint the refresher and its daily jobs call."""

    def __init__(self, errors: dict[str, BaseException] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.last_daily_request_count: int | None = None
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)
        self._errors = errors or {}
        self.closed = False

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        for fragment, error in self._errors.items():
            if fragment in path:
                raise error
        if path.endswith("/historic"):
            return dict(_HISTORIC)
        if path.endswith("/info"):
            return {"data": {"issue_type": "Common Stock", "sector": "Technology"}}
        return {"data": []}

    async def aclose(self) -> None:
        self.closed = True


class _Journal:
    def list(self, status: str | None = None) -> list[Any]:
        del status
        return []


class _Stop(BaseException):
    """Ends the loop from the injected sleep."""


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'board.db'}"


def _seed(url: str, run_id: str, specs: list[tuple[str, str, str, str]], *, ts: datetime) -> None:
    """(event_id, ticker, strike, premium) prints, all calls three days out."""
    store = SqliteBacktestStore(url, flush_threshold=len(specs) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=run_id)
        for i, (event_id, ticker, strike, premium) in enumerate(specs):
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=event_id, ts=ts + timedelta(seconds=i), ticker=ticker,
                        option_type="call", strike=strike, dte=3, premium=premium,
                    ),
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()


def _seed_today(url: str) -> None:
    _seed(url, _RUN, [("e1", "SPY", "757", "500000"), ("e2", "SMCI", "37", "200000")], ts=_RTH - timedelta(minutes=30))


async def _run_loop(
    url: str, client: _FakeClient, *, now: datetime = _RTH, max_sleeps: int = 1,
) -> list[float]:
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= max_sleeps:
            raise _Stop

    with pytest.raises(_Stop):
        await board_refresh_loop(
            database_url=url, profile_path=_CALIBRATION, board_profile_path=_BOARD_PROFILE,
            client_factory=lambda _s: client,  # type: ignore[arg-type,return-value]
            journal_factory=lambda _u: _Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: now, sleep=_sleep,
        )
    return slept


def _count(client: _FakeClient, fragment: str) -> int:
    return sum(1 for path, _params in client.calls if fragment in path)


def _states(url: str, day: date = _TODAY) -> dict[str, dj.JobState]:
    engine = make_engine(url)
    try:
        return dj.read_job_states(engine, day=day)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The morning jobs
# ---------------------------------------------------------------------------


async def test_the_first_tick_runs_the_morning_jobs_with_their_request_counts(url: str) -> None:
    _seed_today(url)
    client = _FakeClient()
    await _run_loop(url, client)

    assert _count(client, "/api/earnings/") == 2  # one per board ticker
    assert _count(client, "/fda-calendar") == 2
    assert _count(client, "/economic-calendar") == 1
    assert _count(client, "/greek-exposure") == 2  # SPY and QQQ
    assert _count(client, "/historic") == 0  # no live run on the previous session
    # The post-close jobs are not due at 11:30 ET.
    for fragment in ("/holdings", "/ohlc/1d", "/recent-trades", "/transactions", "/ftds"):
        assert _count(client, fragment) == 0, fragment
    states = _states(url)
    assert {name: s.status for name, s in states.items()} == {
        "oi_confirm": "success", "catalysts": "success", "gamma_history": "success",
    }
    assert states["catalysts"].detail is not None
    assert "requests=5" in states["catalysts"].detail


async def test_the_daily_jobs_run_before_the_market_cycle(url: str) -> None:
    _seed_today(url)
    client = _FakeClient()
    await _run_loop(url, client)
    paths = [path for path, _params in client.calls]
    first_cycle_call = next(i for i, p in enumerate(paths) if p.endswith("/option-contracts"))
    last_job_call = max(i for i, p in enumerate(paths) if "/greek-exposure" in p)
    assert last_job_call < first_cycle_call


async def test_the_previous_sessions_dominant_contract_is_confirmed(url: str) -> None:
    _seed(url, _MONDAY_RUN, [("m1", "SPY", "757", "15000")], ts=_MONDAY)
    _seed_today(url)
    client = _FakeClient()
    await _run_loop(url, client)

    historic = [(path, params) for path, params in client.calls if path.endswith("/historic")]
    assert historic == [(f"/api/option-contract/{_FLAGGED}/historic", {"limit": 5})]
    engine = make_engine(url)
    try:
        with session_factory(engine)() as session:
            view = load_oi_confirm(session, _FLAGGED, date(2026, 9, 14))
    finally:
        engine.dispose()
    assert view is not None
    assert (view.status, view.flagged_size, view.delta_oi) == ("acilis", 100, 103)
    assert view.label == "açılış (T+1 OI teyitli)"


# ---------------------------------------------------------------------------
# Repeat and catch-up
# ---------------------------------------------------------------------------


async def test_a_second_tick_repeats_no_daily_job(url: str) -> None:
    _seed_today(url)
    client = _FakeClient()
    await _run_loop(url, client, max_sleeps=3)
    assert _count(client, "/api/earnings/") == 2  # once, not once per cycle
    assert _count(client, "/greek-exposure") == 2
    assert _count(client, "/option-contracts") == 3 * 2  # the RTH cycle still runs every tick


async def test_a_restart_neither_repeats_nor_skips_the_day(url: str) -> None:
    _seed_today(url)
    # A restart before the job time: nothing runs, nothing is marked.
    early = _FakeClient()
    await _run_loop(url, early, now=_BEFORE_OPEN)
    assert early.calls == []
    assert _states(url) == {}

    # The process restarts after the time: the day is caught up exactly once.
    first = _FakeClient()
    await _run_loop(url, first)
    assert _count(first, "/api/earnings/") == 2
    # Another restart on the same database repeats nothing.
    second = _FakeClient()
    await _run_loop(url, second)
    assert _count(second, "/api/earnings/") == 0
    assert _count(second, "/greek-exposure") == 0
    assert _count(second, "/option-contracts") == 2


# ---------------------------------------------------------------------------
# Outside RTH
# ---------------------------------------------------------------------------


async def test_post_close_jobs_run_outside_rth_and_no_market_cycle_runs(url: str) -> None:
    _seed_today(url)
    client = _FakeClient()
    slept = await _run_loop(url, client, now=_POST_CLOSE)

    assert _count(client, "/holdings") == len(_SETTINGS.portfolio.focused_etfs)
    assert _count(client, "/ohlc/1d") == 2  # SPY and SMCI; SPY is added by the job itself
    assert _count(client, "/recent-trades") == 2
    assert _count(client, "/insider/transactions") == 2
    assert _count(client, "/interest-float/v2") == 2
    assert _count(client, "/ftds") == 2
    # No market cycle outside RTH.
    for fragment in ("/option-contracts", "/atm-chains", "/net-prem-ticks", "/flow"):
        assert _count(client, fragment) == 0, fragment
    assert set(_states(url)) == {
        "oi_confirm", "catalysts", "gamma_history", "etf_holdings", "daily_close", "delayed",
    }
    assert slept == [float(_CLOSED_SLEEP)]  # everything done: the next job is tomorrow


async def test_the_closed_market_sleep_wakes_for_the_next_due_job(url: str) -> None:
    _seed_today(url)
    client = _FakeClient()
    slept = await _run_loop(url, client, now=_BEFORE_OPEN)
    assert client.calls == []
    assert slept == [pytest.approx(120.0, abs=1)]  # 07:13 ET: the 07:15 job is two minutes out


async def test_a_weekend_tick_runs_no_daily_job(url: str) -> None:
    _seed_today(url)
    client = _FakeClient()
    slept = await _run_loop(url, client, now=_SATURDAY, max_sleeps=2)
    assert client.calls == []
    assert slept == [float(_CLOSED_SLEEP), float(_CLOSED_SLEEP)]
    assert _states(url, _SATURDAY.date()) == {}


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_the_daily_limit_backs_the_loop_off(url: str) -> None:
    _seed_today(url)
    error = UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit")
    client = _FakeClient(errors={"/api/earnings/": error})
    slept = await _run_loop(url, client)
    assert slept == [float(_SETTINGS.refresh.daily_limit_backoff_seconds)]
    assert len(client.calls) == 1
    assert _states(url)["catalysts"].status == "failed"


async def test_a_key_failure_is_logged_and_waits_one_cadence(
    url: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_today(url)
    client = _FakeClient(errors={"/api/earnings/": UnusualWhalesAuthError("returned HTTP 401")})
    with caplog.at_level(logging.ERROR):
        slept = await _run_loop(url, client)
    assert slept == [float(_CADENCE)]
    assert len(client.calls) == 1
    assert "UW key failure" in caplog.text
    assert "UW auth error" in caplog.text


async def test_a_failing_job_is_isolated_from_the_market_cycle(
    url: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_today(url)
    broken = RuntimeError("alfa_catalyst unavailable")
    client = _FakeClient(errors={"/economic-calendar": broken})
    with caplog.at_level(logging.ERROR, logger="webapp.board.daily_jobs"):
        await _run_loop(url, client)
    assert "board daily job catalysts failed" in caplog.text
    assert _count(client, "/greek-exposure") == 2  # the next job still ran
    assert _count(client, "/option-contracts") == 2  # and so did the RTH cycle
    states = _states(url)
    assert states["catalysts"].status == "failed"
    assert states["gamma_history"].status == "success"


async def test_a_failed_job_is_not_retried_inside_one_cadence(url: str) -> None:
    _seed_today(url)
    broken = RuntimeError("alfa_catalyst unavailable")
    client = _FakeClient(errors={"/economic-calendar": broken})
    await _run_loop(url, client, max_sleeps=2)
    # Two ticks on the same clock: the failed job waits a full cadence before retrying.
    assert _count(client, "/economic-calendar") == 1

    later = _FakeClient(errors={"/economic-calendar": broken})
    await _run_loop(url, later, now=_RTH + timedelta(seconds=_CADENCE))
    assert _count(later, "/economic-calendar") == 1


async def test_the_loop_creates_every_daily_job_table_at_startup(url: str) -> None:
    from sqlalchemy import inspect

    _seed_today(url)
    # A Saturday tick: no job runs, so only the startup ensure_* calls can create them.
    await _run_loop(url, _FakeClient(), now=_SATURDAY)
    engine: Engine = make_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {
        "alfa_job_run", "alfa_oi_confirm", "alfa_catalyst", "alfa_catalyst_fetch",
        "alfa_regime", "alfa_etf_holding", "alfa_daily_close", "alfa_delayed",
    } <= names
