"""Phase 5.2.A1: the board's run reader and parse cache (``webapp/board/signals.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Aggregation covers
the whole selected run, not a page capped by score. Parsed rows are cached in
process, keyed by (run_id, event_id); only new rows are parsed on each render").

Pins:
  - every row of the run is returned (well above the old 150 cap), oldest
    first, and rows of other runs are not;
  - ``alfa_print_meta`` is joined when present; a meta row written after the
    signal row is picked up on the next load without re-parsing the signal;
  - a second load parses nothing; a load after new rows parses exactly those;
  - an unparseable row is skipped, logged and never parsed again;
  - the cache keeps the requested run plus the newest cached ``live-*`` run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from webapp.board.signals import BoardSignalReader
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from pathlib import Path

_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_RUN = "live-2026-09-15"


def _seed(url: str, run_id: str, event_ids: list[str], *, start_minute: int = 0) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(event_ids) + 1, replay_safe=True)
    try:
        if store.get_run(run_id) is None:
            store.start_run(profile=load_default_profile(), universe_id="live", run_id=run_id)
        else:
            store._active_run_id = run_id
        for i, event_id in enumerate(event_ids):
            pr = build_print(
                event_id=event_id, ts=_TS + timedelta(seconds=start_minute * 60 + i),
                ticker="SPY", strike="760", dte=3,
            )
            store.add(
                EnrichedEvent(print=pr),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
        store._flush_buffers()
    finally:
        store.close()


def _add_meta(reader: BoardSignalReader, run_id: str, event_ids: list[str], side: str = "at_bid") -> None:
    ensure_telemetry_tables(reader.engine)
    with Session(reader.engine) as session:
        for event_id in event_ids:
            session.add(
                AlfaPrintMeta(
                    run_id=run_id, event_id=event_id, ticker="SPY",
                    option_chain="SPY260918C00760000", fill_side=side, option_type="call",
                    strike="760", expiry=(_TS + timedelta(days=3)).date(),
                    print_ts=_TS, written_at=_TS,
                ),
            )
        session.commit()


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'board.db'}"


def test_every_row_of_the_run_is_loaded_without_a_cap(url: str) -> None:
    ids = [f"e{i:04d}" for i in range(180)]
    _seed(url, _RUN, ids)
    _seed(url, "live-2026-09-14", ["other-1", "other-2"])
    reader = BoardSignalReader(url)
    try:
        prints = reader.load_run(_RUN)
    finally:
        reader.close()

    assert [p.event_id for p in prints] == ids  # oldest first, all of them
    assert {p.run_id for p in prints} == {_RUN}
    assert all(p.signal.ticker == "SPY" for p in prints)
    assert all(p.meta is None for p in prints)


def test_print_meta_is_joined_and_late_meta_is_picked_up(url: str) -> None:
    _seed(url, _RUN, ["a", "b", "c"])
    reader = BoardSignalReader(url)
    try:
        _add_meta(reader, _RUN, ["a"])
        first = {p.event_id: p for p in reader.load_run(_RUN)}
        assert first["a"].meta is not None
        assert first["a"].meta.fill_side == "at_bid"
        assert first["a"].meta.option_chain == "SPY260918C00760000"
        assert first["b"].meta is None

        parsed = reader.parse_attempts
        _add_meta(reader, _RUN, ["b"], side="at_ask")  # written after the signal row
        second = {p.event_id: p for p in reader.load_run(_RUN)}
        assert second["b"].meta is not None
        assert second["b"].meta.fill_side == "at_ask"
        assert second["c"].meta is None
        assert reader.parse_attempts == parsed  # meta join re-parses nothing
    finally:
        reader.close()


def test_the_meta_join_costs_one_query_per_render_not_two(url: str) -> None:
    """The print-meta join asks the table once per render.

    A probe used to run first, selecting EVERY event_id of the run to decide which
    of the missing ids were worth asking for — narrowing an IN list that narrows
    nothing, since an id with no row returns no row either way. Nothing pinned the
    query count, so an extra full scan of the run's print-meta sat in the render
    path unnoticed until the 2026-09-19 audit. This counts the SELECTs so it cannot
    come back silently.
    """
    _seed(url, _RUN, ["a", "b", "c"])
    reader = BoardSignalReader(url)
    selects: list[str] = []

    def record(
        _conn: object, _cursor: object, statement: str, _parameters: object,
        _context: object, _executemany: bool,
    ) -> None:
        text = statement.lstrip().lower()
        if text.startswith("select") and "alfa_print_meta" in text:
            selects.append(statement)

    try:
        _add_meta(reader, _RUN, ["a"])
        event.listen(reader.engine, "before_cursor_execute", record)

        reader.load_run(_RUN)
        assert len(selects) == 1, f"first render issued {len(selects)} meta selects"

        selects.clear()
        _add_meta(reader, _RUN, ["b"], side="at_ask")
        loaded = {p.event_id: p for p in reader.load_run(_RUN)}
        assert len(selects) == 1, f"second render issued {len(selects)} meta selects"
        # And the one query still did its job, or counting queries proves nothing.
        assert loaded["b"].meta is not None
        assert loaded["b"].meta.fill_side == "at_ask"
    finally:
        event.remove(reader.engine, "before_cursor_execute", record)
        reader.close()


def test_only_new_rows_are_parsed(url: str) -> None:
    _seed(url, _RUN, [f"e{i}" for i in range(20)])
    reader = BoardSignalReader(url)
    try:
        assert len(reader.load_run(_RUN)) == 20
        assert reader.parse_attempts == 20
        assert len(reader.load_run(_RUN)) == 20
        assert reader.parse_attempts == 20

        _seed(url, _RUN, ["n1", "n2", "n3"], start_minute=30)
        prints = reader.load_run(_RUN)
        assert len(prints) == 23
        assert reader.parse_attempts == 23
        assert [p.event_id for p in prints][-3:] == ["n1", "n2", "n3"]
    finally:
        reader.close()


def test_unparseable_row_is_skipped_logged_and_not_reparsed(
    url: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url, _RUN, ["good-1", "good-2"])
    reader = BoardSignalReader(url)
    try:
        with Session(reader.engine) as session:
            template = session.get(SignalRow, (_RUN, "good-1"))
            assert template is not None
            session.add(
                SignalRow(
                    run_id=_RUN, event_id="broken", ticker="SPY", ts=template.ts,
                    label=template.label, max_r=template.max_r,
                    profile_id=template.profile_id,
                    profile_content_hash=template.profile_content_hash,
                    full_record_json='{"not": "a stored signal"}',
                ),
            )
            session.commit()
        with caplog.at_level(logging.WARNING, logger="webapp.board.signals"):
            assert {p.event_id for p in reader.load_run(_RUN)} == {"good-1", "good-2"}
            assert {p.event_id for p in reader.load_run(_RUN)} == {"good-1", "good-2"}
        assert reader.parse_attempts == 3
        assert caplog.text.count("unparseable signal") == 1
    finally:
        reader.close()


def test_cache_keeps_the_requested_run_and_the_newest_live_run(url: str) -> None:
    for run_id in ("seed", "live-2026-09-14", "live-2026-09-15", "backtest-x"):
        _seed(url, run_id, [f"{run_id}-e1"])
    reader = BoardSignalReader(url)
    try:
        for run_id in ("seed", "live-2026-09-14", "live-2026-09-15", "backtest-x"):
            assert len(reader.load_run(run_id)) == 1
        assert reader.cached_runs() == {"backtest-x", "live-2026-09-15"}
        reader.load_run("live-2026-09-14")
        assert reader.cached_runs() == {"live-2026-09-14", "live-2026-09-15"}
        assert reader.load_run("no-such-run") == []
    finally:
        reader.close()
