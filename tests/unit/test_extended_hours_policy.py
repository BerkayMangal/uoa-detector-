"""Tests for ``TimeOfDayWeights.extended_hours_policy`` — three policies covering
the spec-silent gap for outside-session prints.

Default policy is ``"flag"``: apply the configured weight AND tag the event.
``"weight"`` is silent (Phase 1 behavior). ``"reject"`` drops the print —
no scoring, no labeling — and records a structured ``RejectedEvent``.
"""

from __future__ import annotations

import shutil
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import build_print
from uoa_detector.calibration import CalibrationProfile, load_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.pipeline.stages.m39_time_of_day import TimeOfDayStage
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

# Pre-market: 08:00 NY = 12:00 UTC during EDT (June). Outside every window.
EXT_HOURS_TS_UTC = datetime(2025, 6, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def profiles_root(tmp_path: Path) -> Path:
    """Working copy of the profiles dir under ``tmp_path``."""
    src = Path("profiles").resolve()
    dst = tmp_path / "profiles"
    shutil.copytree(src, dst)
    return dst


def _profile_with_policy(profiles_root: Path, policy: str) -> CalibrationProfile:
    """Build a child profile that overrides only ``extended_hours_policy``."""
    override = profiles_root / "tickers" / "PCY.yaml"
    override.write_text(
        f"profile_id: policy_{policy}\n"
        "description: test\n"
        "inherits_from: v5_default\n"
        "time_of_day:\n"
        f"  extended_hours_policy: \"{policy}\"\n",
    )
    return load_profile(override, profiles_dir=profiles_root)


@pytest.mark.asyncio
async def test_extended_hours_policy_weight_silent(profiles_root: Path) -> None:
    """'weight' policy: applies outside_session_weight, no flag, no rejection."""
    profile = _profile_with_policy(profiles_root, "weight")
    ctx = PipelineContext(profile=profile)
    event = EnrichedEvent(print=build_print(ts=EXT_HOURS_TS_UTC, ticker="X"))

    await TimeOfDayStage().enrich(event, ctx)

    assert event.time_of_day_weight == profile.time_of_day.outside_session_weight
    assert event.time_window_label == "outside_session"
    assert "extended_hours" not in event.flags
    assert event.rejection is None


@pytest.mark.asyncio
async def test_extended_hours_policy_flag_tags_event(profiles_root: Path) -> None:
    """'flag' policy (default): weight applied AND extended_hours flag set."""
    profile = _profile_with_policy(profiles_root, "flag")
    ctx = PipelineContext(profile=profile)
    event = EnrichedEvent(print=build_print(ts=EXT_HOURS_TS_UTC, ticker="X"))

    await TimeOfDayStage().enrich(event, ctx)

    assert event.time_of_day_weight == profile.time_of_day.outside_session_weight
    assert event.time_window_label == "outside_session"
    assert event.flags.get("extended_hours") is True
    assert event.rejection is None


@pytest.mark.asyncio
async def test_extended_hours_policy_flag_does_not_tag_in_session(
    profiles_root: Path,
) -> None:
    """'flag' policy must NOT tag in-session prints — flag is for the gap only."""
    profile = _profile_with_policy(profiles_root, "flag")
    ctx = PipelineContext(profile=profile)
    # 11:30 NY = 15:30 UTC (EDT) — prime session
    in_session_ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    event = EnrichedEvent(print=build_print(ts=in_session_ts, ticker="X"))

    await TimeOfDayStage().enrich(event, ctx)

    assert event.time_window_label == "prime_session"
    assert "extended_hours" not in event.flags


@pytest.mark.asyncio
async def test_extended_hours_policy_reject_sets_rejection(profiles_root: Path) -> None:
    """'reject' policy: rejection set, no time_of_day_weight, no flag."""
    profile = _profile_with_policy(profiles_root, "reject")
    ctx = PipelineContext(profile=profile)
    event = EnrichedEvent(print=build_print(ts=EXT_HOURS_TS_UTC, ticker="X"))

    await TimeOfDayStage().enrich(event, ctx)

    assert event.rejection is not None
    assert event.rejection.rejected_by_stage == "m39_time_of_day"
    assert "extended_hours" in event.rejection.reason
    # Crucial: do NOT set time_of_day_weight when rejecting.
    assert event.time_of_day_weight is None
    assert event.time_window_label is None
    assert "extended_hours" not in event.flags


@pytest.mark.asyncio
async def test_orchestrator_short_circuits_on_rejection(profiles_root: Path) -> None:
    """End-to-end: pipeline detects rejection and skips scoring / labeling."""
    profile = _profile_with_policy(profiles_root, "reject")
    pr = build_print(ts=EXT_HOURS_TS_UTC, ticker="X")
    src = SyntheticRawFlowSource("synthetic", [to_raw_print(pr)])

    pipeline = Pipeline([src], list(default_stage_pipeline()), profile=profile)
    results = await pipeline.run()

    assert len(results) == 1
    result = results[0]
    # Phase 2.3.2a: rejected events get the dedicated REJECTED label.
    assert result.decision.label == SignalLabel.REJECTED
    assert "Rejected by m39_time_of_day" in result.decision.reason
    # Zero-R PositionSize for rejected events.
    assert result.size.max_r == 0.0
    # Scoring did not run.
    assert result.event.combined_score_pre_penalty is None
    assert result.event.combined_score_post_penalty is None
    # Rejection record preserved on the event.
    assert result.event.rejection is not None
    # Rejected events do NOT enter the cluster buffer.
    assert pipeline.context.recent_events == deque(maxlen=1024)


@pytest.mark.asyncio
async def test_default_v5_uses_flag_policy_end_to_end() -> None:
    """v5_default scenario produces no rejections — extended-hours TS is flagged."""
    from uoa_detector.calibration import load_default_profile

    profile = load_default_profile()
    pr = build_print(ts=EXT_HOURS_TS_UTC, ticker="X")
    src = SyntheticRawFlowSource("synthetic", [to_raw_print(pr)])

    pipeline = Pipeline([src], list(default_stage_pipeline()), profile=profile)
    results = await pipeline.run()

    assert len(results) == 1
    ev = results[0].event
    assert ev.rejection is None
    # Default policy is "flag", so the event should be tagged.
    assert ev.flags.get("extended_hours") is True
    assert ev.time_of_day_weight == profile.time_of_day.outside_session_weight
