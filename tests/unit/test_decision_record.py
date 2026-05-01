"""Tests for ``observability`` — decision records, profile hashing, writers."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.observability import (
    DecisionRecordWriter,
    NDJSONWriter,
    ParquetWriter,
    PrettyWriter,
    SignalDecisionRecord,
    StageExecutionEntry,
    build_decision_record,
)

if TYPE_CHECKING:
    pass


_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def _full_event() -> EnrichedEvent:
    """An event with ALL sub-scores populated so build_decision_record runs
    through every code path in score_breakdown.
    """
    ev = EnrichedEvent(
        print=OptionsPrint(
            event_id="evt-1",
            timestamp=_TS,
            ticker="AAPL",
            option_type="call",
            strike=Decimal("200"),
            expiry=date(2025, 7, 18),
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal("4500"),
            option_price=Decimal("1.50"),
            implied_volatility=0.45,
            bid=Decimal("1.45"),
            ask=Decimal("1.55"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=True,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )
    ev.uoa_score = 0.85
    ev.convexity_score = 0.6
    ev.event_score = 0.5
    ev.gamma_score = 0.4
    ev.price_confirmation_score = 0.7
    ev.sector_confirmation_score = 0.3
    ev.time_of_day_weight = 1.0
    ev.cluster_density_score = 0.8
    ev.relative_premium_score = 1.0
    ev.dte_multiplier_applied = 1.0
    ev.combined_score_pre_penalty = 0.65
    ev.combined_score_post_penalty = 0.65
    return ev


def _decision() -> LabelDecision:
    return LabelDecision(
        label=SignalLabel.HIGH_CONVICTION_SEQUENCE,
        reason="Test decision",
    )


def _size() -> PositionSize:
    return PositionSize(
        bucket=RiskBucket.HIGH_CONVICTION_SEQUENCE,
        max_r=1.0,
        scale_in=True,
        initial_r=0.5,
    )


# ---------------------------------------------------------------------------
# CalibrationProfile.content_hash
# ---------------------------------------------------------------------------


def test_profile_content_hash_is_deterministic() -> None:
    """Two loads of the same profile produce the same hash."""
    h1 = load_default_profile().content_hash()
    h2 = load_default_profile().content_hash()
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_profile_content_hash_changes_when_field_changes() -> None:
    """Mutate a single tunable → different hash."""
    p1 = load_default_profile()
    p2 = p1.model_copy(
        update={"label_thresholds": p1.label_thresholds.model_copy(
            update={"penalized_below": 0.40},
        )},
    )
    assert p1.content_hash() != p2.content_hash()


# ---------------------------------------------------------------------------
# build_decision_record
# ---------------------------------------------------------------------------


def test_build_decision_record_carries_full_state() -> None:
    profile = load_default_profile()
    record = build_decision_record(
        event=_full_event(),
        profile=profile,
        decision=_decision(),
        size=_size(),
        stage_executions=[
            StageExecutionEntry(stage_name="m39_time_of_day", latency_ms=1.2),
            StageExecutionEntry(stage_name="m38_temporal_cluster", latency_ms=0.8),
        ],
    )

    assert record.profile_id == profile.profile_id
    assert record.profile_content_hash == profile.content_hash()
    assert record.event.print_.ticker == "AAPL"
    assert record.decision.label == SignalLabel.HIGH_CONVICTION_SEQUENCE
    assert record.size.max_r == 1.0
    assert len(record.stage_executions) == 2
    assert record.stage_executions[0].stage_name == "m39_time_of_day"

    # score_breakdown contains weighted contributions for each component.
    bd = record.score_breakdown
    assert "uoa" in bd
    assert "convexity" in bd
    assert "event" in bd
    assert "gamma" in bd
    assert "price_confirmation" in bd
    assert "sector_confirmation" in bd
    assert "time_of_day" in bd


def test_build_decision_record_is_frozen() -> None:
    """SignalDecisionRecord is immutable post-construction."""
    record = build_decision_record(
        event=_full_event(),
        profile=load_default_profile(),
        decision=_decision(),
        size=_size(),
    )
    with pytest.raises(Exception, match="frozen"):
        record.profile_id = "modified"  # type: ignore[misc]


def test_emitted_at_defaults_to_walltime() -> None:
    before = datetime.now(tz=UTC)
    record = build_decision_record(
        event=_full_event(),
        profile=load_default_profile(),
        decision=_decision(),
        size=_size(),
    )
    after = datetime.now(tz=UTC)
    assert before <= record.decision_emitted_at <= after


def test_emitted_at_can_be_pinned() -> None:
    pinned = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    record = build_decision_record(
        event=_full_event(),
        profile=load_default_profile(),
        decision=_decision(),
        size=_size(),
        emitted_at=pinned,
    )
    assert record.decision_emitted_at == pinned


# ---------------------------------------------------------------------------
# NDJSONWriter
# ---------------------------------------------------------------------------


def _record() -> SignalDecisionRecord:
    return build_decision_record(
        event=_full_event(),
        profile=load_default_profile(),
        decision=_decision(),
        size=_size(),
    )


def test_ndjson_writer_satisfies_protocol() -> None:
    import io

    w = NDJSONWriter(io.StringIO())
    assert isinstance(w, DecisionRecordWriter)


def test_ndjson_writer_emits_one_object_per_line(tmp_path: Path) -> None:
    """Three writes → three lines, each parses as a complete JSON object."""
    out_path = tmp_path / "out.jsonl"
    w = NDJSONWriter(out_path)
    for _ in range(3):
        w.write(_record())
    w.close()

    content = out_path.read_text().splitlines()
    assert len(content) == 3
    for line in content:
        obj = json.loads(line)
        assert obj["profile_id"] == load_default_profile().profile_id
        # 'print' alias used (not 'print_')
        assert "print" in obj["event"]
        assert "ticker" in obj["event"]["print"]


def test_ndjson_writer_flushes_after_each_record(tmp_path: Path) -> None:
    """A partial run leaves valid NDJSON on disk."""
    out_path = tmp_path / "partial.jsonl"
    w = NDJSONWriter(out_path)
    w.write(_record())
    # Don't call close — file should still have a complete first record.
    content = out_path.read_text()
    assert content.endswith("\n")
    obj = json.loads(content.strip())
    assert obj["profile_id"] == load_default_profile().profile_id


def test_ndjson_writer_close_is_idempotent(tmp_path: Path) -> None:
    w = NDJSONWriter(tmp_path / "x.jsonl")
    w.close()
    w.close()  # second call must not raise


def test_ndjson_writer_write_after_close_raises(tmp_path: Path) -> None:
    w = NDJSONWriter(tmp_path / "x.jsonl")
    w.close()
    with pytest.raises(RuntimeError, match="closed"):
        w.write(_record())


# ---------------------------------------------------------------------------
# PrettyWriter
# ---------------------------------------------------------------------------


def test_pretty_writer_satisfies_protocol() -> None:
    import io

    assert isinstance(PrettyWriter(io.StringIO()), DecisionRecordWriter)


def test_pretty_writer_emits_human_readable_block() -> None:
    import io

    sink = io.StringIO()
    w = PrettyWriter(sink)
    w.write(_record())
    out = sink.getvalue()

    # Header line
    assert "AAPL CALL" in out
    assert "200" in out  # strike
    # Decision section
    assert "decision:" in out
    assert "HIGH_CONVICTION_SEQUENCE" in out
    # Sub-scores section
    assert "sub-scores:" in out
    assert "uoa_score" in out
    # Source agreement
    assert "tier=" in out
    # Profile hash truncated to 12 chars
    assert "content_hash=" in out


def test_pretty_writer_includes_score_adjustments_when_present() -> None:
    import io

    from uoa_detector.domain.agreement import ScoreAdjustment

    event = _full_event()
    event.score_adjustments.append(
        ScoreAdjustment(
            target="uoa_score",
            delta=0.15,
            reason="ISO sweep",
            source_module="m34_sweep_block",
        ),
    )
    record = build_decision_record(
        event=event,
        profile=load_default_profile(),
        decision=_decision(),
        size=_size(),
    )

    sink = io.StringIO()
    PrettyWriter(sink).write(record)
    out = sink.getvalue()

    assert "adjustments:" in out
    assert "ISO sweep" in out
    assert "m34_sweep_block" in out


# ---------------------------------------------------------------------------
# ParquetWriter
# ---------------------------------------------------------------------------


def test_parquet_writer_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(ParquetWriter(tmp_path / "x.parquet"), DecisionRecordWriter)


def test_parquet_writer_writes_table_on_close(tmp_path: Path) -> None:
    """Three records → close() → readable parquet with 3 rows."""
    import pyarrow.parquet as pq

    out_path = tmp_path / "decisions.parquet"
    w = ParquetWriter(out_path)
    for _ in range(3):
        w.write(_record())
    w.close()

    assert out_path.exists()
    table = pq.read_table(out_path)
    assert table.num_rows == 3
    columns = set(table.column_names)
    # Top-level scalars present for grep-ability
    for needed in (
        "ticker", "label", "max_r",
        "combined_score_pre_penalty", "combined_score_post_penalty",
        "profile_id", "profile_content_hash",
    ):
        assert needed in columns
    # Nested objects encoded as JSON strings
    assert "event_json" in columns
    assert "score_breakdown_json" in columns


def test_parquet_writer_no_records_produces_no_file(tmp_path: Path) -> None:
    """Closing without writing anything → no file created (avoids empty-table
    parquet which some tools choke on).
    """
    out_path = tmp_path / "empty.parquet"
    w = ParquetWriter(out_path)
    w.close()
    assert not out_path.exists()


def test_parquet_writer_close_is_idempotent(tmp_path: Path) -> None:
    out_path = tmp_path / "x.parquet"
    w = ParquetWriter(out_path)
    w.write(_record())
    w.close()
    w.close()  # must not raise


def test_parquet_writer_write_after_close_raises(tmp_path: Path) -> None:
    w = ParquetWriter(tmp_path / "x.parquet")
    w.write(_record())
    w.close()
    with pytest.raises(RuntimeError, match="closed"):
        w.write(_record())


# ---------------------------------------------------------------------------
# Integration: Pipeline with writer attached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_with_ndjson_writer_emits_one_record_per_event(
    tmp_path: Path,
) -> None:
    """End-to-end: run the default scenario with NDJSONWriter attached;
    expect one line per scripted print, all parseable.
    """
    from uoa_detector.pipeline.orchestrator import Pipeline
    from uoa_detector.pipeline.stages import default_stage_pipeline
    from uoa_detector.sources.scenarios import (
        ScenarioOverrideStage,
        default_scenario,
        overrides_lookup,
    )
    from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

    out_path = tmp_path / "decisions.jsonl"
    profile = load_default_profile()
    steps = default_scenario()
    src = SyntheticRawFlowSource(
        "synthetic",
        [to_raw_print(s.print_, source_id="synthetic") for s in steps],
    )
    stages = [
        ScenarioOverrideStage(overrides_lookup(iter(steps))),
        *default_stage_pipeline(),
    ]
    pipeline = Pipeline(
        [src],
        stages,
        profile=profile,
        decision_record_writer=NDJSONWriter(out_path),
    )
    await pipeline.run()

    lines = out_path.read_text().splitlines()
    assert len(lines) == len(steps)
    for line in lines:
        obj = json.loads(line)
        assert "profile_id" in obj
        assert "decision" in obj
        assert "score_breakdown" in obj
        assert "stage_executions" in obj
        # Every event has a stage_executions trace per the pipeline default
        assert len(obj["stage_executions"]) >= 1
