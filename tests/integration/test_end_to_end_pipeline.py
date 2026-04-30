"""End-to-end pipeline integration test (Phase 2.3.4 wiring).

Phase 2.3.4 changes:
  - ``Pipeline`` now consumes one or more ``RawFlowSource``s through
    ``SourceFusion``. The default scenario uses a single
    ``SyntheticRawFlowSource`` so every event runs through the
    single-source fast path with ``confidence_tier='single'``.
  - Phase 1-authored ``OptionsPrint`` scenario steps are converted into
    ``RawPrint``s via ``to_raw_print`` at test setup time.

The labels and scores produced are byte-identical to the Phase 1 baseline.
Fusion adds a ``SourceAgreement`` to every event without changing the
underlying scoring or labeling pipeline.
"""

from __future__ import annotations

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.labels import SignalLabel
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.scenarios import (
    ScenarioOverrideStage,
    default_scenario,
    overrides_lookup,
)
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print


def _build_pipeline() -> tuple[Pipeline, list]:
    """Build a Pipeline from the default scenario through SourceFusion.

    Returns ``(pipeline, steps)`` so tests can assert on both. Single source
    means every event will have ``confidence_tier='single'`` in its agreement.
    """
    profile = load_default_profile()
    steps = default_scenario()
    raw_prints = [to_raw_print(s.print_, source_id="synthetic") for s in steps]
    src = SyntheticRawFlowSource("synthetic", raw_prints)
    stages = [ScenarioOverrideStage(overrides_lookup(iter(steps))), *default_stage_pipeline()]
    pipeline = Pipeline([src], stages, profile=profile)
    return pipeline, steps


@pytest.mark.asyncio
async def test_default_scenario_produces_all_expected_labels() -> None:
    pipeline, steps = _build_pipeline()
    results = await pipeline.run()

    # Sanity: one result per scripted print
    assert len(results) == len(steps)
    assert len(pipeline.store) == len(steps)

    # Each result's label matches the scenario's expectation.
    actual_labels = [r.decision.label.value for r in results]
    expected_labels = [s.expected_label for s in steps]
    assert actual_labels == expected_labels


@pytest.mark.asyncio
async def test_every_event_has_single_confidence_tier() -> None:
    """Phase 2.3.4: with one RawFlowSource the fusion path emits tier='single'.

    This is the integration-level proof that the new pipeline wires the
    single-source fast path correctly. If fusion ever ran windowing for a
    single-source scenario, this would catch it.
    """
    pipeline, _ = _build_pipeline()
    results = await pipeline.run()

    for r in results:
        agreement = r.event.print_.source_agreement
        assert agreement.confidence_tier == "single"
        assert agreement.sources_seen == ("synthetic",)
        assert agreement.timestamp_skew_ms == 0
        assert agreement.classification_disagreement is False
        assert agreement.premium_disagreement.is_zero()


@pytest.mark.asyncio
async def test_default_scenario_covers_the_eight_required_labels() -> None:
    """Task spec: the default scenario must exercise these eight labels."""
    pipeline, _ = _build_pipeline()
    await pipeline.run()
    seen = {row.label for row in pipeline.store.all()}

    required = {
        SignalLabel.IGNORE_NOISE,
        SignalLabel.CONVEXITY_WATCH,
        SignalLabel.CONVEXITY_CLUSTER,
        SignalLabel.STANDARD_UOA,
        SignalLabel.SWEEP_UOA,
        SignalLabel.PENALIZED_BELOW_THRESHOLD,
        SignalLabel.LEAP_POSITIONING,
        SignalLabel.HIGH_CONVICTION_SEQUENCE,
    }
    assert required.issubset(seen)


@pytest.mark.asyncio
async def test_score_monotonicity_implied_by_scenario() -> None:
    """The HCS step has the strongest sub-scores; its post-penalty score
    should exceed every non-LEAP, non-PENALIZED row.
    """
    profile = load_default_profile()
    pipeline, _ = _build_pipeline()
    await pipeline.run()

    rows = pipeline.store.all()
    by_label = {r.label: r for r in rows}

    hcs = by_label[SignalLabel.HIGH_CONVICTION_SEQUENCE]
    ignore = by_label[SignalLabel.IGNORE_NOISE]
    watch = by_label[SignalLabel.CONVEXITY_WATCH]
    cluster = by_label[SignalLabel.CONVEXITY_CLUSTER]
    standard = by_label[SignalLabel.STANDARD_UOA]
    sweep = by_label[SignalLabel.SWEEP_UOA]
    penalized = by_label[SignalLabel.PENALIZED_BELOW_THRESHOLD]

    assert hcs.combined_score_post_penalty is not None
    for other in (ignore, watch, cluster, standard, sweep):
        assert other.combined_score_post_penalty is not None
        assert hcs.combined_score_post_penalty > other.combined_score_post_penalty

    # Penalized must be strictly below the spec's 0.30 threshold
    assert penalized.combined_score_post_penalty is not None
    assert penalized.combined_score_post_penalty < profile.label_thresholds.penalized_below


@pytest.mark.asyncio
async def test_backtest_store_to_dataframe_round_trips() -> None:
    pipeline, steps = _build_pipeline()
    await pipeline.run()

    df = pipeline.store.to_dataframe()
    assert len(df) == len(steps)
    # Schema sanity — Module 29 required columns
    for col in (
        "timestamp", "ticker", "option_type", "strike", "expiry", "dte",
        "premium", "option_price", "moneyness",
        "uoa_score", "convexity_score", "event_score", "gamma_score",
        "price_confirmation_score", "sector_confirmation_score",
        "time_of_day_weight", "cluster_density_score", "relative_premium_score",
        "dte_multiplier_applied",
        "combined_score_pre_penalty", "combined_score_post_penalty",
        "penalties_applied", "contradiction_penalty_applied",
        "sweep_classification", "is_iso", "gamma_flag",
        "sector_confirmation", "dark_pool_confirmation",
        "label", "max_r",
    ):
        assert col in df.columns, f"missing column {col}"


@pytest.mark.asyncio
async def test_dte_multiplier_recorded_per_event() -> None:
    """Every stored row gets a DTE multiplier; the LEAP row gets 0.70."""
    profile = load_default_profile()
    pipeline, _ = _build_pipeline()
    await pipeline.run()

    for row in pipeline.store.all():
        assert row.dte_multiplier_applied is not None
        assert row.dte_multiplier_applied > 0

    leap_row = next(r for r in pipeline.store.all() if r.label == SignalLabel.LEAP_POSITIONING)
    assert leap_row.dte_multiplier_applied == profile.dte.bucket_60_plus  # 0.70 for DTE>60
