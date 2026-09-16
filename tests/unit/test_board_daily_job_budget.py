"""Phase 5.2.B-fix1: the daily jobs stay inside the UW request budget.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("It pauses
non-critical fetches when the client's last seen ``x-uw-daily-req-count``
reaches ``refresh.daily_request_soft_cap``") and §4.4 (the daily rows of the
budget); program rule 9 (a not-found answer is "no data").

Review findings FB-H1 / FB-01 / FB-02: the daily jobs ignored the soft cap, and
a failing job re-ran every cadence until ET midnight, so one broken job could
spend thousands of extra requests and reach the key's daily limit.

Pins:
  - the soft cap pauses the whole daily-job step, and pauses it again between
    two jobs of the same tick, marking nothing (the jobs stay due);
  - a failed job stops once ``refresh.daily_job_max_attempts`` attempts of that
    ET day are spent, so one broken job costs a bounded number of requests;
  - the marker counts the attempts and survives a restart;
  - the closed-market wake-up skips a job whose attempts are spent;
  - a not-found answer is recorded as no data (success), never as a failure
    that retries all day;
  - the refresher hands its own soft cap to the daily jobs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import daily_jobs as dj
from webapp.board.db import make_engine
from webapp.board.refresher import board_refresh_loop
from webapp.board.settings import load_board_settings

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_BOARD_PROFILE = _REPO / "profiles" / "board_v1.yaml"
_CALIBRATION = _REPO / "profiles" / "v5_default.yaml"
_SETTINGS = load_board_settings(_BOARD_PROFILE)
_CADENCE = _SETTINGS.refresh.cadence_seconds
_MAX_ATTEMPTS = _SETTINGS.refresh.daily_job_max_attempts
# 2026-09-15 is a Tuesday in EDT (UTC-4): 07:15 ET is 11:15 UTC.
_DUE = datetime(2026, 9, 15, 11, 15, tzinfo=UTC)
_DAY = date(2026, 9, 15)
_RTH = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_RUN = "live-2026-09-15"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'budget.db'}")


class _Client:
    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del path, params, method
        return {"data": []}


class _Reader:
    def load_run(self, run_id: str) -> list[Any]:
        del run_id
        return []


class _Cap:
    """A soft cap the test drives: ``answers`` are consumed in order, then the last repeats."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers) or [False]
        self.checks = 0

    def reached(self, now: datetime) -> bool:
        del now
        self.checks += 1
        return self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)


def _job(name: str, log: list[str], clock: time = time(7, 15)) -> dj.DailyJob:
    async def _run(ctx: dj.JobContext) -> str:
        del ctx
        log.append(name)
        return f"{name} ran"

    return dj.DailyJob(name=name, et_time=clock, run=_run)


def _failing(name: str, error: BaseException) -> dj.DailyJob:
    async def _run(ctx: dj.JobContext) -> str:
        del ctx
        raise error

    return dj.DailyJob(name=name, et_time=time(7, 15), run=_run)


def _state(status: dj.JobStatus, started_at: datetime, attempts: int = 1) -> dj.JobState:
    return dj.JobState(
        status=status, started_at=started_at, finished_at=started_at, detail=None,
        attempts=attempts,
    )


async def _tick(
    engine: Engine, jobs: list[dj.DailyJob], *, now: datetime = _DUE, cap: _Cap | None = None,
) -> dj.JobsReport:
    return await dj.run_daily_jobs(
        _Client(), engine, _SETTINGS,
        jobs=jobs, reader=_Reader(), journal=None, now=now, cap=cap,
    )


# ---------------------------------------------------------------------------
# The soft cap (FB-H1 / FB-01)
# ---------------------------------------------------------------------------


async def test_the_soft_cap_pauses_the_whole_daily_job_step(engine: Engine) -> None:
    log: list[str] = []
    jobs = [_job("a", log), _job("b", log)]
    paused = await _tick(engine, jobs, cap=_Cap(True))
    assert log == []
    assert paused.ran == ()
    assert paused.due == ("a", "b")  # still due: nothing was marked
    assert dj.read_job_states(engine, day=_DAY) == {}

    # The cap clears on a later tick and the day is caught up exactly once.
    resumed = await _tick(engine, jobs, cap=_Cap(False))
    assert log == ["a", "b"]
    assert [status for _name, status, _detail in resumed.ran] == ["success", "success"]


async def test_the_cap_stops_the_remaining_jobs_of_the_same_tick(engine: Engine) -> None:
    log: list[str] = []
    jobs = [_job("a", log), _job("b", log)]
    # Not reached for the step, not reached before "a", reached before "b".
    report = await _tick(engine, jobs, cap=_Cap(False, False, True))
    assert log == ["a"]
    assert report.ran == (("a", "success", "a ran"),)
    assert set(dj.read_job_states(engine, day=_DAY)) == {"a"}  # "b" never started: still due


async def test_without_a_cap_the_jobs_run_as_before(engine: Engine) -> None:
    log: list[str] = []
    report = await _tick(engine, [_job("a", log)])
    assert log == ["a"]
    assert report.ran == (("a", "success", "a ran"),)


# ---------------------------------------------------------------------------
# Bounded retries (FB-H1 / FB-02)
# ---------------------------------------------------------------------------


def test_a_failed_job_stops_after_the_profile_attempt_cap() -> None:
    log: list[str] = []
    job = _job("a", log)
    later = _DUE + timedelta(seconds=_CADENCE)
    last = _state("failed", _DUE, attempts=_MAX_ATTEMPTS - 1)
    spent = _state("failed", _DUE, attempts=_MAX_ATTEMPTS)
    assert dj.is_due(
        job, now=later, state=last, cadence_seconds=_CADENCE, max_attempts=_MAX_ATTEMPTS,
    ) is True
    assert dj.is_due(
        job, now=later, state=spent, cadence_seconds=_CADENCE, max_attempts=_MAX_ATTEMPTS,
    ) is False
    # Without a cap the old, unbounded behaviour is unchanged.
    assert dj.is_due(job, now=later, state=spent, cadence_seconds=_CADENCE) is True


def test_one_broken_job_costs_a_bounded_number_of_attempts_in_one_day() -> None:
    log: list[str] = []
    jobs = [_job("delayed", log, time.fromisoformat(_SETTINGS.delayed.job_time_et))]
    states: dict[str, dj.JobState] = {}
    attempts = 0
    now = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # 06:00 ET
    end = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)  # ET midnight
    while now < end:
        for job in dj.due_jobs(
            jobs, now=now, states=states, cadence_seconds=_CADENCE, max_attempts=_MAX_ATTEMPTS,
        ):
            attempts += 1
            states[job.name] = _state("failed", now, attempts=attempts)
        now += timedelta(seconds=_CADENCE)
    # Before the fix this was 84 attempts, i.e. about 3,360 wasted requests (4N at N=10).
    assert attempts == _MAX_ATTEMPTS


def test_the_closed_market_wake_up_skips_a_job_whose_attempts_are_spent() -> None:
    log: list[str] = []
    jobs = [_job("a", log)]
    now = _DUE + timedelta(hours=2)
    states = {"a": _state("failed", now - timedelta(seconds=_CADENCE), attempts=_MAX_ATTEMPTS)}
    wait = dj.seconds_until_next_job(
        jobs, now=now, states=states, cadence_seconds=_CADENCE, max_attempts=_MAX_ATTEMPTS,
    )
    assert wait is not None
    # Tomorrow's 07:15 ET, not one more cadence today.
    assert wait == pytest.approx(float(24 * 3600 - 2 * 3600), abs=1)


async def test_the_marker_counts_attempts_and_a_spent_job_runs_nothing(engine: Engine) -> None:
    broken = RuntimeError("alfa_catalyst unavailable")
    jobs = [_failing("a", broken)]
    for attempt in range(_MAX_ATTEMPTS):
        await _tick(engine, jobs, now=_DUE + timedelta(seconds=_CADENCE * attempt))
        state = dj.read_job_states(engine, day=_DAY)["a"]
        assert state.attempts == attempt + 1
        assert state.status == "failed"
    spent = await _tick(engine, jobs, now=_DUE + timedelta(seconds=_CADENCE * _MAX_ATTEMPTS))
    assert spent.due == () and spent.ran == ()
    # The count is stored, not in-process: it is read back from alfa_job_run.
    assert dj.read_job_states(engine, day=_DAY)["a"].attempts == _MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# A not-found answer is no data (program rule 9)
# ---------------------------------------------------------------------------


async def test_a_not_found_job_is_recorded_as_no_data_and_not_retried(engine: Engine) -> None:
    missing = UnusualWhalesNotFoundError(
        "UnusualWhales GET /x returned HTTP 404: not found", status_code=404,
    )
    report = await _tick(engine, [_failing("a", missing)])
    assert [status for _name, status, _detail in report.ran] == ["success"]
    state = dj.read_job_states(engine, day=_DAY)["a"]
    assert state.succeeded is True
    assert state.detail is not None and state.detail.startswith("no data")
    # A second tick on the same ET day spends nothing more on a source with no data.
    again = await _tick(engine, [_failing("a", missing)], now=_DUE + timedelta(seconds=_CADENCE))
    assert again.ran == ()


# ---------------------------------------------------------------------------
# The refresher hands over its own cap (FB-01)
# ---------------------------------------------------------------------------


class _CountingClient:
    """A refresher client whose key has already passed the daily soft cap."""

    def __init__(self, count: int) -> None:
        self.calls: list[str] = []
        self.last_daily_request_count: int | None = count
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        self.calls.append(path)
        return {"data": []}

    async def aclose(self) -> None:
        return


class _Journal:
    def list(self, status: str | None = None) -> list[Any]:
        del status
        return []


class _Stop(BaseException):
    """Ends the loop from the injected sleep."""


def _seed_today(url: str) -> None:
    store = SqliteBacktestStore(url, flush_threshold=4)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (event_id, ticker) in enumerate((("e1", "SPY"), ("e2", "SMCI"))):
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=event_id, ts=_RTH - timedelta(minutes=30 - i), ticker=ticker,
                        option_type="call", strike="757", dte=3, premium="500000",
                    ),
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()


async def test_the_refresher_pauses_its_daily_jobs_at_the_soft_cap(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'loop.db'}"
    _seed_today(url)
    client = _CountingClient(_SETTINGS.refresh.daily_request_soft_cap + 1)

    async def _sleep(seconds: float) -> None:
        del seconds
        raise _Stop

    with pytest.raises(_Stop):
        await board_refresh_loop(
            database_url=url, profile_path=_CALIBRATION, board_profile_path=_BOARD_PROFILE,
            client_factory=lambda _s: client,  # type: ignore[arg-type,return-value]
            journal_factory=lambda _u: _Journal(),  # type: ignore[arg-type,return-value]
            clock=lambda: _RTH, sleep=_sleep,
        )
    # No daily-job endpoint was called while the key is past the soft cap.
    for fragment in ("/api/earnings/", "/fda-calendar", "/economic-calendar", "/greek-exposure"):
        assert not any(fragment in path for path in client.calls), fragment
    engine = make_engine(url)
    try:
        assert dj.read_job_states(engine, day=_DAY) == {}
    finally:
        engine.dispose()
