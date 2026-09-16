"""Phase 5.2.B-jobs: the daily-job clock (``webapp/board/daily_jobs.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 (daily jobs on
profile clock times in ET), §4.4 and §7 (scheduling only).

Pins:
  - the registry is ONE ordered list whose times come from the board profile,
    and FAZ C can append its outcome job with one entry;
  - due and not-due at the exact ET boundary, in EDT and in EST;
  - a weekend never runs a job; a weekday market holiday is not modelled and
    still runs (the documented gap, as in ``sources/market_hours.py``);
  - the marker table stops a repeat and survives a restart, and a day whose
    job never ran is picked up later the same day (catch-up);
  - a failed or crashed attempt retries at most once per
    ``refresh.cadence_seconds``;
  - the closed-market wake-up: seconds until the next due job;
  - job tickers are the live run's board tickers plus the open journal
    tickers, and a broken journal degrades to the board tickers;
  - the previous ET session's dominant contracts become flagged contracts with
    their aggregated session size;
  - one job's exception is isolated and marked failed; the daily limit and a
    key failure propagate, another 4xx does not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import inspect
from webapp.board import daily_jobs as dj
from webapp.board.db import make_engine
from webapp.board.settings import BoardSettings, load_board_settings

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
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from webapp.board.signals import BoardPrint

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CADENCE = _SETTINGS.refresh.cadence_seconds
# 2026-09-15 is a Tuesday in EDT (UTC-4): 07:15 ET is 11:15 UTC.
_EDT_DUE = datetime(2026, 9, 15, 11, 15, tzinfo=UTC)
# 2027-01-12 is a Tuesday in EST (UTC-5): 07:15 ET is 12:15 UTC.
_EST_DUE = datetime(2027, 1, 12, 12, 15, tzinfo=UTC)
_SATURDAY = datetime(2026, 9, 19, 20, 0, tzinfo=UTC)
_RUN = "live-2026-09-15"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")


def _job(name: str = "test", clock: time = time(7, 15), *, trading_day_only: bool = True) -> dj.DailyJob:
    async def _run(ctx: dj.JobContext) -> str:
        del ctx
        return "ok"

    return dj.DailyJob(name=name, et_time=clock, run=_run, trading_day_only=trading_day_only)


def _state(status: dj.JobStatus, started_at: datetime) -> dj.JobState:
    return dj.JobState(status=status, started_at=started_at, finished_at=started_at, detail=None)


class _Reader:
    def __init__(self, prints: list[BoardPrint] | None = None) -> None:
        self.prints = prints or []
        self.runs: list[str] = []

    def load_run(self, run_id: str) -> list[BoardPrint]:
        self.runs.append(run_id)
        return self.prints


class _Journal:
    def __init__(self, tickers: list[str] | None = None, *, broken: bool = False) -> None:
        self._tickers = tickers or []
        self._broken = broken

    def list(self, status: str | None = None) -> list[Any]:
        if self._broken:
            msg = "journal table unavailable"
            raise RuntimeError(msg)
        assert status == "open"
        return [type("T", (), {"ticker": t})() for t in self._tickers]


class _Client:
    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del path, params, method
        return {"data": []}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_the_registry_is_one_ordered_list_built_from_the_profile() -> None:
    jobs = dj.build_registry(_SETTINGS)
    assert [j.name for j in jobs] == [
        "oi_confirm", "catalysts", "gamma_history", "etf_holdings", "daily_close", "delayed",
    ]
    pre_market = time.fromisoformat(_SETTINGS.opening_closing.job_time_et)
    post_close = time.fromisoformat(_SETTINGS.delayed.job_time_et)
    assert [j.et_time for j in jobs] == [
        pre_market, pre_market, dj.SESSION_OPEN_ET, post_close, post_close, post_close,
    ]
    assert all(j.trading_day_only for j in jobs)
    assert len({j.name for j in jobs}) == len(jobs)


def test_faz_c_appends_its_outcome_job_with_one_entry() -> None:
    outcomes = _job("outcomes", time.fromisoformat(_SETTINGS.outcomes.job_time_et))
    extended = (*dj.build_registry(_SETTINGS), outcomes)
    assert [j.name for j in extended][-1] == "outcomes"
    assert extended[-1].et_time == time(17, 30)


# ---------------------------------------------------------------------------
# The due rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "due"),
    [
        pytest.param(_EDT_DUE, True, id="edt-at-the-time"),
        pytest.param(_EDT_DUE - timedelta(minutes=1), False, id="edt-one-minute-before"),
        pytest.param(_EDT_DUE + timedelta(hours=9), True, id="edt-later-the-same-day"),
        pytest.param(_EST_DUE, True, id="est-at-the-time"),
        pytest.param(_EST_DUE - timedelta(minutes=1), False, id="est-one-minute-before"),
    ],
)
def test_due_at_the_et_boundary_in_both_dst_states(now: datetime, due: bool) -> None:
    assert dj.is_due(_job(), now=now, state=None, cadence_seconds=_CADENCE) is due


def test_a_weekend_never_runs_a_trading_day_job() -> None:
    assert dj.is_trading_day(_SATURDAY.date()) is False
    assert dj.is_due(_job(), now=_SATURDAY, state=None, cadence_seconds=_CADENCE) is False
    assert dj.due_jobs(dj.build_registry(_SETTINGS), now=_SATURDAY, states={}, cadence_seconds=_CADENCE) == []
    always = _job("always", trading_day_only=False)
    assert dj.is_due(always, now=_SATURDAY, state=None, cadence_seconds=_CADENCE) is True


def test_a_weekday_market_holiday_is_not_modelled_and_still_runs() -> None:
    # Christmas Day 2026 falls on a Friday. Holidays are not modelled here, exactly as
    # uoa_detector/sources/market_hours.py documents for the RTH gate: the cost is one
    # wasted set of requests whose jobs then store nothing.
    christmas = datetime(2026, 12, 25, 13, 0, tzinfo=UTC)  # 08:00 ET
    assert dj.is_trading_day(christmas.date()) is True
    assert dj.is_due(_job(), now=christmas, state=None, cadence_seconds=_CADENCE) is True


def test_a_success_marker_stops_a_repeat_and_a_missing_one_is_caught_up() -> None:
    done = _state("success", _EDT_DUE)
    assert dj.is_due(_job(), now=_EDT_DUE + timedelta(hours=6), state=done, cadence_seconds=_CADENCE) is False
    # A restart at 18:00 ET with no marker still runs the morning job (catch-up).
    assert dj.is_due(_job(), now=_EDT_DUE + timedelta(hours=11), state=None, cadence_seconds=_CADENCE) is True


@pytest.mark.parametrize("status", ["failed", "running"])
def test_a_failed_or_crashed_attempt_retries_once_per_cadence(status: dj.JobStatus) -> None:
    attempt = _state(status, _EDT_DUE)
    job = _job()
    just_before = _EDT_DUE + timedelta(seconds=_CADENCE - 1)
    assert dj.is_due(job, now=just_before, state=attempt, cadence_seconds=_CADENCE) is False
    assert dj.is_due(
        job, now=_EDT_DUE + timedelta(seconds=_CADENCE), state=attempt, cadence_seconds=_CADENCE,
    ) is True


def test_due_jobs_keeps_registry_order() -> None:
    jobs = dj.build_registry(_SETTINGS)
    # 17:05 ET on a Tuesday: every job's time has passed.
    evening = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)
    assert [j.name for j in dj.due_jobs(jobs, now=evening, states={}, cadence_seconds=_CADENCE)] == [
        j.name for j in jobs
    ]


# ---------------------------------------------------------------------------
# The closed-market wake-up
# ---------------------------------------------------------------------------


def test_seconds_until_next_job_is_zero_when_one_is_due() -> None:
    jobs = dj.build_registry(_SETTINGS)
    assert dj.seconds_until_next_job(jobs, now=_EDT_DUE, states={}, cadence_seconds=_CADENCE) == 0.0


def test_seconds_until_next_job_counts_down_to_the_first_time() -> None:
    jobs = dj.build_registry(_SETTINGS)
    wait = dj.seconds_until_next_job(
        jobs, now=_EDT_DUE - timedelta(seconds=120), states={}, cadence_seconds=_CADENCE,
    )
    assert wait == 120.0


def test_seconds_until_next_job_skips_the_weekend_after_a_success() -> None:
    friday_done = datetime(2026, 9, 18, 11, 20, tzinfo=UTC)  # 07:20 ET, Friday
    states = {"oi_confirm": _state("success", friday_done)}
    wait = dj.seconds_until_next_job(
        [_job("oi_confirm")], now=friday_done, states=states, cadence_seconds=_CADENCE,
    )
    assert wait is not None
    # Monday 07:15 ET, three days later minus the five minutes already elapsed.
    assert wait == pytest.approx(3 * 24 * 3600 - 300, abs=1)


def test_seconds_until_next_job_waits_a_cadence_after_a_failure() -> None:
    states = {"oi_confirm": _state("failed", _EDT_DUE)}
    wait = dj.seconds_until_next_job(
        [_job("oi_confirm")], now=_EDT_DUE, states=states, cadence_seconds=_CADENCE,
    )
    assert wait == float(_CADENCE)


def test_seconds_until_next_job_is_none_without_jobs() -> None:
    assert dj.seconds_until_next_job([], now=_EDT_DUE, states={}, cadence_seconds=_CADENCE) is None


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def test_markers_round_trip_and_survive_a_restart(engine: Engine, tmp_path: Path) -> None:
    day = date(2026, 9, 15)
    dj.mark_started(engine, job="catalysts", day=day, now=_EDT_DUE)
    running = dj.read_job_states(engine, day=day)["catalysts"]
    assert (running.status, running.finished_at) == ("running", None)
    dj.mark_finished(
        engine, job="catalysts", day=day, now=_EDT_DUE + timedelta(seconds=5),
        status="success", detail="events=3 requests=5",
    )
    engine.dispose()

    # A Railway restart: a new process, a new engine, the same database.
    restarted = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    try:
        states = dj.read_job_states(restarted, day=day)
        assert states["catalysts"].succeeded is True
        assert states["catalysts"].detail == "events=3 requests=5"
        assert states["catalysts"].started_at == _EDT_DUE
        assert dj.read_job_states(restarted, day=day + timedelta(days=1)) == {}
    finally:
        restarted.dispose()


def test_the_marker_detail_is_truncated(engine: Engine) -> None:
    day = date(2026, 9, 15)
    dj.mark_finished(
        engine, job="delayed", day=day, now=_EDT_DUE, status="failed", detail="x" * 5000,
    )
    detail = dj.read_job_states(engine, day=day)["delayed"].detail
    assert detail is not None and len(detail) == 500


def test_the_marker_table_is_created_once(engine: Engine) -> None:
    dj.ensure_job_run_tables(engine)
    dj.ensure_job_run_tables(engine)
    assert "alfa_job_run" in inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _seed_run(url: str, run_id: str, specs: list[tuple[str, str, str, str]], *, ts: datetime) -> None:
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


def _reader_for(url: str) -> Any:
    from webapp.board.signals import BoardSignalReader

    return BoardSignalReader(url)


def test_job_tickers_are_the_board_rows_then_the_open_journal(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'tickers.db'}"
    _seed_run(url, _RUN, [("e1", "SPY", "757", "500000"), ("e2", "SMCI", "37", "200000")],
              ts=datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    engine = make_engine(url)
    reader = _reader_for(url)
    try:
        tickers = dj.job_tickers(
            engine, reader, _Journal(["nvda", "SPY", " "]),
            settings=_SETTINGS, now=datetime(2026, 9, 15, 15, 30, tzinfo=UTC),
        )
    finally:
        reader.close()
        engine.dispose()
    assert tickers == ("SPY", "SMCI", "NVDA")


def test_job_tickers_degrade_to_the_board_when_the_journal_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    url = f"sqlite:///{tmp_path / 'tickers.db'}"
    _seed_run(url, _RUN, [("e1", "SPY", "757", "500000")], ts=datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    engine = make_engine(url)
    reader = _reader_for(url)
    try:
        with caplog.at_level("WARNING", logger="webapp.board.daily_jobs"):
            tickers = dj.job_tickers(
                engine, reader, _Journal(broken=True),
                settings=_SETTINGS, now=datetime(2026, 9, 15, 15, 30, tzinfo=UTC),
            )
    finally:
        reader.close()
        engine.dispose()
    assert tickers == ("SPY",)
    assert "open journal read failed" in caplog.text


def test_job_tickers_fall_back_to_the_latest_live_run(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'tickers.db'}"
    _seed_run(url, "live-2026-09-14", [("e1", "SPY", "757", "500000")],
              ts=datetime(2026, 9, 14, 15, 0, tzinfo=UTC))
    engine = make_engine(url)
    reader = _reader_for(url)
    try:
        # No run on today's ET date: the latest live run still names the tickers.
        tickers = dj.job_tickers(
            engine, reader, None, settings=_SETTINGS, now=datetime(2026, 9, 15, 11, 15, tzinfo=UTC),
        )
    finally:
        reader.close()
        engine.dispose()
    assert tickers == ("SPY",)


def test_previous_session_flags_carry_the_aggregated_session_size(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'flags.db'}"
    monday = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
    # Two prints on the same contract ($1.50 each): 500000 + 300000 premium.
    _seed_run(url, "live-2026-09-14", [
        ("e1", "SPY", "757", "500000"), ("e2", "SPY", "757", "300000"), ("e3", "SMCI", "37", "90000"),
    ], ts=monday)
    engine = make_engine(url)
    reader = _reader_for(url)
    try:
        flags = dj.previous_session_flags(
            engine, reader, settings=_SETTINGS, now=datetime(2026, 9, 15, 11, 15, tzinfo=UTC),
        )
    finally:
        reader.close()
        engine.dispose()
    assert [f.option_symbol for f in flags] == ["SPY260917C00757000", "SMCI260917C00037000"]
    assert [f.trade_date for f in flags] == [date(2026, 9, 14), date(2026, 9, 14)]
    # 800000 / (1.50 x 100) = 5333.33 -> 5333 contracts; 90000 / 150 = 600.
    assert [f.flagged_size for f in flags] == [5333, 600]
    assert [f.ticker for f in flags] == ["SPY", "SMCI"]


def test_previous_session_flags_are_empty_without_a_run(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'flags.db'}"
    _seed_run(url, _RUN, [("e1", "SPY", "757", "500000")], ts=datetime(2026, 9, 15, 15, 0, tzinfo=UTC))
    engine = make_engine(url)
    reader = _reader_for(url)
    try:
        flags = dj.previous_session_flags(
            engine, reader, settings=_SETTINGS, now=datetime(2026, 9, 15, 11, 15, tzinfo=UTC),
        )
    finally:
        reader.close()
        engine.dispose()
    assert flags == []


def test_the_previous_session_of_a_monday_is_the_friday() -> None:
    assert dj.previous_session(date(2026, 9, 14)) == date(2026, 9, 11)
    assert dj.previous_session(date(2026, 9, 15)) == date(2026, 9, 14)
    assert dj.next_trading_day(date(2026, 9, 18)) == date(2026, 9, 21)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def _failing(name: str, error: BaseException) -> dj.DailyJob:
    async def _run(ctx: dj.JobContext) -> str:
        del ctx
        raise error

    return dj.DailyJob(name=name, et_time=time(7, 15), run=_run)


def _recording(name: str, log: list[str]) -> dj.DailyJob:
    async def _run(ctx: dj.JobContext) -> str:
        del ctx
        log.append(name)
        return f"{name} ran"

    return dj.DailyJob(name=name, et_time=time(7, 15), run=_run)


async def _tick(
    engine: Engine, jobs: list[dj.DailyJob], *, now: datetime = _EDT_DUE,
    settings: BoardSettings = _SETTINGS,
) -> dj.JobsReport:
    return await dj.run_daily_jobs(
        _Client(), engine, settings,
        jobs=jobs, reader=_Reader(), journal=None, now=now,
    )


async def test_nothing_due_runs_nothing(engine: Engine) -> None:
    log: list[str] = []
    report = await _tick(engine, [_recording("a", log)], now=_EDT_DUE - timedelta(minutes=5))
    assert (report.due, report.ran, log) == ((), (), [])


async def test_a_due_job_runs_once_and_is_marked(engine: Engine) -> None:
    log: list[str] = []
    jobs = [_recording("a", log), _recording("b", log)]
    first = await _tick(engine, jobs)
    second = await _tick(engine, jobs)
    assert first.due == ("a", "b")
    assert first.ran == (("a", "success", "a ran"), ("b", "success", "b ran"))
    assert log == ["a", "b"]
    assert second.ran == ()  # the success markers stop the repeat
    states = dj.read_job_states(engine, day=date(2026, 9, 15))
    assert {name: s.status for name, s in states.items()} == {"a": "success", "b": "success"}


async def test_one_failing_job_does_not_stop_the_others(
    engine: Engine, caplog: pytest.LogCaptureFixture,
) -> None:
    log: list[str] = []
    jobs = [_failing("a", RuntimeError("alfa_catalyst unavailable")), _recording("b", log)]
    with caplog.at_level("ERROR", logger="webapp.board.daily_jobs"):
        report = await _tick(engine, jobs)
    assert [status for _name, status, _detail in report.ran] == ["failed", "success"]
    assert log == ["b"]
    assert "board daily job a failed" in caplog.text
    states = dj.read_job_states(engine, day=date(2026, 9, 15))
    assert states["a"].status == "failed"
    assert states["a"].detail is not None and states["a"].detail.startswith("error:")
    # Retried only after one cadence.
    assert dj.is_due(jobs[0], now=_EDT_DUE, state=states["a"], cadence_seconds=_CADENCE) is False


async def test_the_daily_limit_propagates_and_marks_the_job_failed(engine: Engine) -> None:
    error = UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit")
    with pytest.raises(UnusualWhalesDailyLimitError):
        await _tick(engine, [_failing("a", error), _recording("b", [])])
    states = dj.read_job_states(engine, day=date(2026, 9, 15))
    assert states["a"].status == "failed"
    assert "b" not in states  # the tick stopped; the loop backs off


async def test_a_key_failure_propagates_and_another_4xx_does_not(engine: Engine) -> None:
    log: list[str] = []
    bad_key = UnusualWhalesAuthError("UnusualWhales GET /x returned HTTP 401: unauthorized")
    with pytest.raises(UnusualWhalesAuthError):
        await _tick(engine, [_failing("a", bad_key), _recording("b", log)])
    assert log == []

    other = UnusualWhalesAuthError("UnusualWhales GET /x returned HTTP 400: bad symbol")
    report = await _tick(engine, [_failing("c", other), _recording("d", log)])
    assert [status for _name, status, _detail in report.ran] == ["failed", "success"]
    assert log == ["d"]


async def test_a_not_found_is_no_data_not_a_key_failure(engine: Engine) -> None:
    log: list[str] = []
    missing = UnusualWhalesNotFoundError(
        "UnusualWhales GET /x returned HTTP 404: not found", status_code=404,
    )
    report = await _tick(engine, [_failing("a", missing), _recording("b", log)])
    assert [status for _name, status, _detail in report.ran] == ["failed", "success"]
    assert log == ["b"]
