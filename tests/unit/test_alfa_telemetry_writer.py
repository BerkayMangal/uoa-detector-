"""Phase 5.2.A0c: the Alfa Board telemetry writer (``webapp/board/telemetry.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 and §4.2;
decisions P7 (side tables) and P11 (fill side is recorded).

Pins:
  - one ``alfa_print_meta`` row per (run, event) and one
    ``alfa_stage_telemetry`` row per (run, event, stage), with the branch,
    the metadata JSON and the profile hash;
  - the UW option chain is taken from the flow-poll event id only when it
    encodes the print's own expiry, type and strike;
  - replay idempotency: an existing key is skipped, never rewritten, and a
    partial earlier write is completed;
  - exception isolation: an unreachable database, a failing commit, a failing
    run-id source or a malformed record never raise into the pipeline, and a
    later write recovers;
  - ``close()`` is idempotent and a write after close is dropped, not raised;
  - end to end: a real ``Pipeline`` replaying the same prints writes exactly
    one meta row per signal row, and a failing writer leaves ingestion intact.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from webapp.board.telemetry import (
    AlfaPrintMeta,
    AlfaStageTelemetry,
    AlfaTelemetryWriter,
    option_chain_from_event_id,
)
from webapp.repo import SignalRepo

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.observability.decision_record import (
    SignalDecisionRecord,
    StageExecutionEntry,
)
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

if TYPE_CHECKING:
    from pathlib import Path

_RUN = "live-2026-09-14"
_TS = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)  # 11:00 EDT, regular session
_WRITTEN = datetime(2026, 9, 14, 15, 0, 5, tzinfo=UTC)
_CHAIN = "SPY260918P00758000"
_UW_EVENT = f"uw-SPY-{_CHAIN}-2026-09-14T15:00:00.123456Z"
_LOGGER = "webapp.board.telemetry"

_STAGES = [
    StageExecutionEntry(stage_name="m39_time_of_day", latency_ms=0.1),
    StageExecutionEntry(
        stage_name="m21_dealer_gamma", latency_ms=1.0,
        metadata={"branch": "no_data", "provider_returned": "no"},
    ),
    StageExecutionEntry(
        stage_name="m23_price_confirmation", latency_ms=2.0,
        metadata={"branch": "put_confirmed", "price_change_pct": "-0.8"},
    ),
]


def _print(event_id: str = _UW_EVENT, *, fill_side: Any = "at_bid", strike: str = "758") -> Any:
    return build_print(
        event_id=event_id, ts=_TS, ticker="SPY", option_type="put",
        strike=strike, dte=4, spot="760", fill_side=fill_side,
    )


def _record(
    event_id: str = _UW_EVENT,
    *,
    stages: list[StageExecutionEntry] | None = None,
    fill_side: Any = "at_bid",
    profile_hash: str = "hash-v5",
) -> SignalDecisionRecord:
    return SignalDecisionRecord(
        decision_emitted_at=_TS,
        profile_id="v5_default",
        profile_content_hash=profile_hash,
        event=EnrichedEvent(print=_print(event_id, fill_side=fill_side)),
        stage_executions=list(_STAGES if stages is None else stages),
        score_breakdown={},
        decision=LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        size=PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _url(tmp_path: Path, name: str = "alfa.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _writer(url: str, run_id: str | None = _RUN) -> AlfaTelemetryWriter:
    return AlfaTelemetryWriter(
        database_url=url, run_id_source=lambda: run_id, clock=lambda: _WRITTEN,
    )


def _rows(url: str, model: Any) -> list[Any]:
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            return list(session.scalars(select(model)))
    finally:
        engine.dispose()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


def test_writes_print_meta_and_one_row_per_stage(tmp_path: Path) -> None:
    url = _url(tmp_path)
    writer = _writer(url)
    writer.write(_record())
    writer.close()

    (meta,) = _rows(url, AlfaPrintMeta)
    assert (meta.run_id, meta.event_id) == (_RUN, _UW_EVENT)
    assert meta.ticker == "SPY"
    assert meta.option_chain == _CHAIN
    assert meta.fill_side == "at_bid"
    assert meta.option_type == "put"
    assert meta.strike == "758"
    assert meta.expiry == date(2026, 9, 18)
    assert _utc(meta.print_ts) == _TS
    assert _utc(meta.written_at) == _WRITTEN

    rows = {r.stage_name: r for r in _rows(url, AlfaStageTelemetry)}
    assert set(rows) == {"m39_time_of_day", "m21_dealer_gamma", "m23_price_confirmation"}
    assert rows["m39_time_of_day"].branch is None
    assert rows["m39_time_of_day"].metadata_json is None
    assert rows["m21_dealer_gamma"].branch == "no_data"
    assert json.loads(rows["m21_dealer_gamma"].metadata_json) == {
        "branch": "no_data", "provider_returned": "no",
    }
    assert rows["m23_price_confirmation"].branch == "put_confirmed"
    for row in rows.values():
        assert row.run_id == _RUN
        assert row.event_id == _UW_EVENT
        assert row.degraded is False  # no wrappers bound to this writer
        assert row.profile_content_hash == "hash-v5"


@pytest.mark.parametrize(
    ("event_id", "strike", "expected"),
    [
        (_UW_EVENT, "758", _CHAIN),
        (f"uw-SPY-{_CHAIN}-2026-09-14T15:00:00Z", "758", _CHAIN),
        (_UW_EVENT, "757", None),  # chain strike differs from the print
        ("uw-SPY-SPY260919P00758000-2026-09-14T15:00:00Z", "758", None),  # other expiry
        ("uw-SPY-SPY260918C00758000-2026-09-14T15:00:00Z", "758", None),  # a call chain
        ("01a0a5b0-93b3-7f03-a2ce-06345b81d513", "758", None),  # REST flow id, no chain
        ("evt-test", "758", None),
    ],
)
def test_option_chain_is_taken_only_when_it_matches_the_print(
    event_id: str, strike: str, expected: str | None,
) -> None:
    assert option_chain_from_event_id(_print(event_id, strike=strike)) == expected


def test_fractional_strike_chain_matches(tmp_path: Path) -> None:
    print_ = build_print(
        event_id="uw-SMCI-SMCI260918C00036500-2026-09-15T15:26:21Z",
        ts=datetime(2026, 9, 15, 15, 26, tzinfo=UTC), ticker="SMCI",
        option_type="call", strike="36.5", dte=3,
    )
    assert option_chain_from_event_id(print_) == "SMCI260918C00036500"


# ---------------------------------------------------------------------------
# Replay idempotency
# ---------------------------------------------------------------------------


def test_replay_skips_existing_keys_and_never_rewrites(tmp_path: Path) -> None:
    url = _url(tmp_path)
    writer = _writer(url)
    writer.write(_record())
    replay_stages = [
        StageExecutionEntry(
            stage_name="m21_dealer_gamma", latency_ms=9.0,
            metadata={"branch": "timeout"},
        ),
    ]
    writer.write(_record(stages=replay_stages, fill_side="at_ask", profile_hash="other"))
    writer.write(_record())
    writer.close()

    (meta,) = _rows(url, AlfaPrintMeta)
    assert meta.fill_side == "at_bid"
    rows = {r.stage_name: r for r in _rows(url, AlfaStageTelemetry)}
    assert len(rows) == 3
    assert rows["m21_dealer_gamma"].branch == "no_data"
    assert rows["m21_dealer_gamma"].profile_content_hash == "hash-v5"


def test_replay_completes_a_partial_earlier_write(tmp_path: Path) -> None:
    url = _url(tmp_path)
    writer = _writer(url)
    writer.write(_record(stages=_STAGES[:1]))
    writer.write(_record())
    writer.close()

    assert len(_rows(url, AlfaPrintMeta)) == 1
    assert {r.stage_name for r in _rows(url, AlfaStageTelemetry)} == {
        "m39_time_of_day", "m21_dealer_gamma", "m23_price_confirmation",
    }


def test_same_event_in_another_run_is_a_new_key(tmp_path: Path) -> None:
    url = _url(tmp_path)
    first = _writer(url, run_id="live-2026-09-14")
    first.write(_record())
    first.close()
    second = _writer(url, run_id="live-2026-09-15")
    second.write(_record())
    second.close()

    assert {m.run_id for m in _rows(url, AlfaPrintMeta)} == {"live-2026-09-14", "live-2026-09-15"}
    assert len(_rows(url, AlfaStageTelemetry)) == 2 * len(_STAGES)


# ---------------------------------------------------------------------------
# Lifecycle and exception isolation
# ---------------------------------------------------------------------------


def test_no_active_run_writes_nothing_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    url = _url(tmp_path)
    writer = _writer(url, run_id=None)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        writer.write(_record())
    writer.close()

    assert not (tmp_path / "alfa.db").exists()
    assert "no active run" in caplog.text


def test_close_is_idempotent_and_a_write_after_close_is_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    url = _url(tmp_path)
    writer = _writer(url)
    writer.write(_record())
    writer.close()
    writer.close()
    assert writer.closed is True

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        writer.write(_record("uw-SPY-SPY260918P00758000-2026-09-14T15:01:00Z"))
    writer.close()

    assert len(_rows(url, AlfaPrintMeta)) == 1
    assert "closed" in caplog.text


def test_close_without_any_write_is_safe() -> None:
    writer = AlfaTelemetryWriter(database_url="sqlite://", run_id_source=lambda: _RUN)
    writer.close()
    writer.close()


def test_unreachable_database_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    url = f"sqlite:///{tmp_path / 'missing-dir' / 'alfa.db'}"
    writer = _writer(url)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        writer.write(_record())
    writer.close()

    assert "alfa telemetry write failed" in caplog.text
    assert "live ingestion unaffected" in caplog.text


def test_a_later_write_recovers_after_the_database_comes_back(tmp_path: Path) -> None:
    directory = tmp_path / "later"
    url = f"sqlite:///{directory / 'alfa.db'}"
    writer = _writer(url)
    writer.write(_record())  # directory missing: dropped
    directory.mkdir()
    second = "uw-SPY-SPY260918P00758000-2026-09-14T15:02:00Z"
    writer.write(_record(second))
    writer.close()

    assert [m.event_id for m in _rows(url, AlfaPrintMeta)] == [second]


def test_failing_commit_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(self: Session) -> None:
        msg = "database is locked"
        raise RuntimeError(msg)

    url = _url(tmp_path)
    writer = _writer(url)
    monkeypatch.setattr(Session, "commit", _boom)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        writer.write(_record())
    monkeypatch.undo()
    writer.close()

    assert "RuntimeError: database is locked" in caplog.text
    assert _rows(url, AlfaPrintMeta) == []


def test_failing_run_id_source_never_raises(tmp_path: Path) -> None:
    def _closed_store() -> str | None:
        msg = "store is closed"
        raise RuntimeError(msg)

    writer = AlfaTelemetryWriter(database_url=_url(tmp_path), run_id_source=_closed_store)
    writer.write(_record())
    writer.close()


def test_malformed_record_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    writer = _writer(_url(tmp_path))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        writer.write(object())  # type: ignore[arg-type]
    writer.close()
    assert "could not read record" in caplog.text


# ---------------------------------------------------------------------------
# End to end through a real Pipeline
# ---------------------------------------------------------------------------


def _raw_prints() -> list[Any]:
    events = [
        (f"uw-SPY-{_CHAIN}-2026-09-14T15:00:00Z", "758", "at_bid"),
        ("uw-SPY-SPY260918P00750000-2026-09-14T15:00:30Z", "750", "at_ask"),
    ]
    return [
        to_raw_print(
            build_print(
                event_id=eid, ts=_TS, ticker="SPY", option_type="put",
                strike=strike, dte=4, spot="760", fill_side=side,
            ),
            source_id="unusual_whales",
        )
        for eid, strike, side in events
    ]


def _adopt_or_start_run(store: SqliteBacktestStore) -> None:
    # Mirrors webapp/worker.py _ensure_run: a same-day restart adopts the run.
    if store.get_run(_RUN) is None:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
    else:
        store._active_run_id = _RUN


async def test_pipeline_replay_writes_one_meta_row_per_signal_row(tmp_path: Path) -> None:
    url = _url(tmp_path)
    store = SqliteBacktestStore(url, flush_threshold=1, replay_safe=True)
    try:
        for _restart in range(2):  # the second pass replays the same event ids
            _adopt_or_start_run(store)
            stages = default_stage_pipeline()
            writer = AlfaTelemetryWriter.for_stages(
                database_url=url, run_id_source=lambda: store.active_run_id, stages=stages,
            )
            pipeline = Pipeline(
                [SyntheticRawFlowSource("unusual_whales", _raw_prints())],
                stages, profile=load_default_profile(), store=store,
                decision_record_writer=writer,
            )
            await pipeline.run()
            assert writer.closed is True  # Pipeline.run closed it
    finally:
        store.close()

    signal_keys = {(r.run_id, r.event_id) for r in _rows(url, SignalRow)}
    meta = _rows(url, AlfaPrintMeta)
    assert {(m.run_id, m.event_id) for m in meta} == signal_keys
    assert len(meta) == len(signal_keys) == 2
    assert {m.option_chain for m in meta} == {_CHAIN, "SPY260918P00750000"}
    assert {m.fill_side for m in meta} == {"at_bid", "at_ask"}
    telemetry = _rows(url, AlfaStageTelemetry)
    stage_names = [s.name for s in default_stage_pipeline()]
    assert len(telemetry) == len(signal_keys) * len(stage_names)
    assert {t.stage_name for t in telemetry} == set(stage_names)


async def test_failing_writer_leaves_live_ingestion_intact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    url = _url(tmp_path)
    store = SqliteBacktestStore(url, flush_threshold=1, replay_safe=True)
    try:
        _adopt_or_start_run(store)
        writer = AlfaTelemetryWriter(
            database_url=f"sqlite:///{tmp_path / 'no-such-dir' / 'x.db'}",
            run_id_source=lambda: store.active_run_id,
        )
        pipeline = Pipeline(
            [SyntheticRawFlowSource("unusual_whales", _raw_prints())],
            default_stage_pipeline(), profile=load_default_profile(), store=store,
            decision_record_writer=writer,
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            results = await pipeline.run()
    finally:
        store.close()

    assert len(results) == 2
    assert {r.run_id: r.count for r in SignalRepo(url).runs()} == {_RUN: 2}
    assert caplog.text.count("alfa telemetry write failed") == 2
