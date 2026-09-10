"""Unit tests for the Phase 3.8 screener self-diagnostic.

Covers:
  - branch -> ok/no_data/error classification (incl. preset_skip / missing)
  - per-module tally across records (only enrichment M-stages counted)
  - the flow-line renderer (counts, n/a fallback, dropped-key sample)
  - the enrichment-line renderer content
"""

from __future__ import annotations

import pytest

from tests.conftest import build_enriched, build_print
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.observability.decision_record import (
    SignalDecisionRecord,
    StageExecutionEntry,
    build_decision_record,
)
from uoa_detector.observability.diagnostics import (
    ModuleHealth,
    classify_branch,
    format_enrichment_line,
    format_flow_line,
    module_health,
)


def _record_with_branches(
    branches: dict[str, str | None],
    *,
    event_id: str = "e",
) -> SignalDecisionRecord:
    """A decision record whose stage_executions carry the given branches.

    ``branches`` maps ``stage_name`` -> branch value (or None for a stage
    that exposed no telemetry).
    """
    profile = load_default_profile()
    event = build_enriched(print_=build_print(event_id=event_id))
    executions = [
        StageExecutionEntry(
            stage_name=name,
            latency_ms=1.0,
            metadata=None if branch is None else {"branch": branch},
        )
        for name, branch in branches.items()
    ]
    return build_decision_record(
        event=event,
        profile=profile,
        decision=LabelDecision(label=SignalLabel.STANDARD_UOA, reason="t"),
        size=PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        stage_executions=executions,
    )


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("full_short_and_proximate", "ok"),
        ("strong", "ok"),
        ("no_data", "no_data"),
        ("no_sector", "no_data"),
        ("empty_peer_flow", "no_data"),
        ("data_missing_neutral", "no_data"),
        ("no_iv_history", "no_data"),
        ("timeout", "error"),
        ("iv_provider_timeout", "error"),
        ("provider_error", "error"),
        ("preset_skip", None),
        (None, None),
    ],
)
def test_classify_branch(branch: str | None, expected: str | None) -> None:
    assert classify_branch(branch) == expected


def test_module_health_tallies_per_enrichment_stage() -> None:
    records = [
        _record_with_branches(
            {
                "m21_dealer_gamma": "no_data",
                "m22_event_calendar": "no_catalyst",  # ok (endpoint worked)
                "m25_sector_peer": "timeout",
                "m39_time_of_day": "computed",  # not an enrichment M-stage
            },
            event_id="e1",
        ),
        _record_with_branches(
            {
                "m21_dealer_gamma": "full_short_and_proximate",
                "m25_sector_peer": "no_sector",
                "m26_dark_pool": "preset_skip",  # skipped, not counted
            },
            event_id="e2",
        ),
    ]
    health = module_health(records)

    assert health["m21_dealer_gamma"] == ModuleHealth(ok=1, no_data=1, error=0)
    assert health["m22_event_calendar"] == ModuleHealth(ok=1, no_data=0, error=0)
    assert health["m25_sector_peer"] == ModuleHealth(ok=0, no_data=1, error=1)
    # m26 saw only a preset_skip → nothing counted.
    assert health["m26_dark_pool"] == ModuleHealth(ok=0, no_data=0, error=0)
    # A non-enrichment stage never appears in the tally.
    assert "m39_time_of_day" not in health


def test_format_enrichment_line_content() -> None:
    records = [
        _record_with_branches(
            {
                "m21_dealer_gamma": "no_data",
                "m25_sector_peer": "timeout",
            },
        ),
    ]
    line = format_enrichment_line(records)
    assert line.startswith("enrichment: ")
    assert "M21 ok=0/nodata=1/err=0" in line
    assert "M25 ok=0/nodata=0/err=1" in line
    # All seven modules are present in the line.
    for short in ("M21", "M22", "M23", "M24", "M25", "M26", "M27"):
        assert short in line


def test_format_flow_line_with_counts_and_dropped_sample() -> None:
    line = format_flow_line(
        fetched=10,
        mapped=8,
        dropped=2,
        dropped_sample=[("id", "ticker"), ("executed_at", "premium")],
    )
    assert "flow rows fetched=10, mapped=8, dropped=2" in line
    assert "dropped row keys sample:" in line
    assert "{id,ticker}" in line
    assert "{executed_at,premium}" in line


def test_format_flow_line_no_drops_omits_sample() -> None:
    line = format_flow_line(fetched=5, mapped=5, dropped=0)
    assert line == "flow rows fetched=5, mapped=5, dropped=0"


def test_format_flow_line_na_when_unavailable() -> None:
    line = format_flow_line(fetched=None, mapped=None, dropped=None)
    assert line == "flow rows fetched=n/a, mapped=n/a, dropped=n/a"
