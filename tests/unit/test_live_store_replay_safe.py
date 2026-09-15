"""Phase 4.43 regression tests: a replayed signal must not wedge live ingestion.

The live worker's flow source re-emits its last 10 minutes of alerts whenever
it is rebuilt: on every in-process restart, and on every Railway redeploy (a
new process with a new store over the same Postgres). Those alerts carry the
same ``event_id`` as rows already stored, and ``(run_id, event_id)`` is the
signal primary key. A plain store fails that commit, keeps the rejected row
buffered and re-sends it with every later write, so the dashboard freezes for
the rest of the day. The live store is replay-safe: stored duplicates are
skipped, and a failed flush is discarded instead of retried forever.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from webapp import worker
from webapp.repo import SignalRepo

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_RUN_ID = "live-2026-09-14"


def _event(event_id: str) -> EnrichedEvent:
    op = OptionsPrint(
        event_id=event_id,
        timestamp=datetime(2026, 9, 14, 16, 31, tzinfo=UTC),
        ticker="SPY",
        option_type="put",
        strike=Decimal("758"),
        expiry=date(2026, 9, 18),
        dte=4,
        spot_price=Decimal("764"),
        premium_paid=Decimal("250000"),
        option_price=Decimal("3.50"),
        implied_volatility=0.18,
        bid=Decimal("3.45"),
        ask=Decimal("3.55"),
        fill_side="at_ask",
        exchange="UNKNOWN",
        is_iso=True,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("unusual_whales",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


def _add(store: SqliteBacktestStore, event_id: str) -> None:
    store.add(
        _event(event_id),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _start_live_run(store: SqliteBacktestStore) -> None:
    store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN_ID)


def _adopt_live_run(store: SqliteBacktestStore) -> None:
    # Mirrors run_live_worker._ensure_run on a same-day restart: the run row
    # already exists, so the worker adopts it instead of starting a new one.
    store._active_run_id = _RUN_ID


def _runs_seen_by_dashboard(url: str) -> dict[str, int]:
    return {r.run_id: r.count for r in SignalRepo(url).runs()}


def _run_counter(store: SqliteBacktestStore) -> int:
    meta = store.get_run(_RUN_ID)
    assert meta is not None
    return meta.total_signals_processed


def test_redeploy_replay_skips_signals_already_stored(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'live.db'}"
    before = worker._open_live_store(url)
    _start_live_run(before)
    _add(before, "uw-SPY-a")
    _add(before, "uw-SPY-b")
    before.close()

    after = worker._open_live_store(url)  # new process, same database
    try:
        _adopt_live_run(after)
        _add(after, "uw-SPY-a")  # backfill replay
        _add(after, "uw-SPY-b")
        _add(after, "uw-SPY-c")  # genuinely new flow
        assert _runs_seen_by_dashboard(url) == {_RUN_ID: 3}
        assert _run_counter(after) == 3
    finally:
        after.close()


def test_in_process_restart_replay_skips_signals_already_stored(tmp_path: Path) -> None:
    """The worker keeps one store across restarts; the rebuilt source replays."""
    url = f"sqlite:///{tmp_path / 'live.db'}"
    store = worker._open_live_store(url)
    try:
        _start_live_run(store)
        _add(store, "uw-SPY-a")
        store.finish_run()  # Pipeline.run's finally on the way out
        _adopt_live_run(store)
        _add(store, "uw-SPY-a")
        _add(store, "uw-SPY-b")
        assert _runs_seen_by_dashboard(url) == {_RUN_ID: 2}
        assert _run_counter(store) == 2
    finally:
        store.close()


def test_failed_flush_does_not_wedge_live_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'live.db'}"
    store = worker._open_live_store(url)
    try:
        _start_live_run(store)
        real_commit = Session.commit
        failures = iter([True])

        def commit_failing_once(session: Session) -> None:
            if next(failures, False):
                raise OperationalError("COMMIT", {}, Exception("connection dropped"))
            real_commit(session)

        monkeypatch.setattr(Session, "commit", commit_failing_once)
        with pytest.raises(OperationalError):
            _add(store, "uw-SPY-a")
        _add(store, "uw-SPY-b")
        assert _runs_seen_by_dashboard(url) == {_RUN_ID: 1}
        _add(store, "uw-SPY-a")  # the rebuilt source replays the dropped signal
        assert _runs_seen_by_dashboard(url) == {_RUN_ID: 2}
        assert _run_counter(store) == 2
    finally:
        store.close()


def test_default_store_still_rejects_a_duplicate_signal(tmp_path: Path) -> None:
    """Backtests keep the strict default: a duplicate there is a data bug."""
    url = f"sqlite:///{tmp_path / 'backtest.db'}"
    store = SqliteBacktestStore(url, flush_threshold=1)
    try:
        _start_live_run(store)
        _add(store, "uw-SPY-a")
        with pytest.raises(IntegrityError):
            _add(store, "uw-SPY-a")
    finally:
        store._signal_buffer.clear()  # the rejected row would fail close() too
        store.close()
