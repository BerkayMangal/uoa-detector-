"""Phase 5.25.12: one ET session can span two live runs.

The live worker opens ``live-<UTC date>`` when it starts, so a restart or a
deploy during the session leaves that morning's prints in the previous run and
writes the rest into a new one. Reading "the newest run" then loses the morning
(live 2026-10-05: the 09:30-11:25 ET flow stayed in ``live-2026-10-01``).
The session is therefore read from every run that carries its ET date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from webapp.board.signals import BoardSignalReader
from webapp.live_alpha import inputs

from tests.unit.test_live_alpha_job import RUN, T0, _seed
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

SESSION = date(2026, 9, 24)


def _add(url: str, run_id: str, stamps: list[datetime], ticker: str = "NVDA") -> None:
    from tests.conftest import build_print

    st = SqliteBacktestStore(url, flush_threshold=10)
    try:
        if st.get_run(run_id) is None:
            st.start_run(profile=load_default_profile(), universe_id="live", run_id=run_id)
        else:
            st._active_run_id = run_id  # same adoption the live worker does on a same-day restart
        for i, ts in enumerate(stamps):
            st.add(
                EnrichedEvent(print=build_print(event_id=f"{run_id}-{i}", ts=ts, ticker=ticker)),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="t"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        st.close()


def test_a_session_split_across_two_runs_is_read_whole(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'split.db'}"
    engine = _seed(url)
    # The stale run carries this morning (09:45 ET) plus a print from two days
    # earlier, which belongs to another session and must not be read.
    _add(url, "live-2026-09-22", [
        datetime(2026, 9, 24, 13, 45, tzinfo=UTC),   # 09:45 ET, this session
        datetime(2026, 9, 22, 14, 0, tzinfo=UTC),    # 10:00 ET two days earlier
    ])
    runs = inputs.live_session_runs(engine, SESSION)
    assert runs is not None
    run_ids, last_ts = runs
    assert run_ids == ("live-2026-09-22", RUN)
    assert last_ts == T0 - timedelta(minutes=20)

    reader = BoardSignalReader(engine=engine)
    prints = inputs.flow_prints(reader, run_ids, et_date=SESSION)
    # 3 seeded prints of today's run + the one 09:45 ET print of the stale run;
    # the 2026-09-22 print is left out.
    assert len(prints) == 4
    assert [p.ts for p in prints] == sorted(p.ts for p in prints)
    assert prints[0].ts == datetime(2026, 9, 24, 13, 45, tzinfo=UTC)


def test_a_session_with_no_prints_is_none(tmp_path: Path) -> None:
    engine = _seed(f"sqlite:///{tmp_path / 'empty.db'}")
    assert inputs.live_session_runs(engine, date(2026, 9, 25)) is None
