"""End-to-end profile hot-swap test.

Verifies that the pipeline picks up profile changes mid-run via
``CalibrationResolver.reload()``, while the in-flight event at the moment
of reload completes on the OLD profile snapshot — no torn state.

Tests in this module:

  - ``test_pipeline_picks_up_profile_change_after_reload``: the headline
    scenario — 3 events on default, modify YAML on disk, reload, 3 events
    on override; assert profile_id and content_hash differ between phases.

  - ``test_in_flight_event_completes_on_old_profile``: a stage triggers
    ``resolver.reload()`` mid-event after the snapshot is captured; the
    resulting decision record carries the pre-reload snapshot.

  - ``test_malformed_yaml_reload_retains_previous_state``: write garbage
    to the profile YAML, ``reload()`` returns False, ``default()`` still
    returns the prior valid profile, pipeline continues uninterrupted.

  - ``test_identical_reload_is_silent``: a second ``reload()`` with no
    file change emits no ``calibration_resolver_reloaded`` info log.

  - ``test_rapid_consecutive_reloads_only_latest_sticks``: write A, reload,
    write B, reload, write C, reload; resolver.default() returns C.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from uoa_detector.calibration.resolver import CalibrationResolver
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.synthetic import SyntheticRawFlowSource

if TYPE_CHECKING:
    from uoa_detector.pipeline.stage import PipelineContext


@pytest.fixture
def profiles_root(tmp_path: Path) -> Path:
    """Build a self-contained profiles dir under tmp_path.

    Copies the real ``profiles/v5_default.yaml`` so the resolver loads a
    valid baseline. ``tickers/`` and ``regimes/`` are created empty so the
    glob in ``CalibrationResolver._load_dir`` doesn't trip on missing dirs.
    """
    root = tmp_path / "profiles"
    root.mkdir()
    shutil.copy("profiles/v5_default.yaml", root / "v5_default.yaml")
    (root / "tickers").mkdir()
    (root / "regimes").mkdir()
    return root


def _raw(event_id: str, timestamp_offset_min: int) -> RawPrint:
    """Build a synthetic RawPrint with a unique event_id."""
    return RawPrint(
        source_id="synthetic",
        source_event_id=event_id,
        timestamp=datetime(2025, 6, 11, 16, 0, tzinfo=UTC).replace(
            minute=timestamp_offset_min,
        ),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal("5000"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        implied_volatility=0.45,
        open_interest=1500,
    )


# ---------------------------------------------------------------------------
# Headline test: profile change after reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_picks_up_profile_change_after_reload(
    profiles_root: Path,
) -> None:
    """3 events on default → modify YAML → reload → 3 events on override.

    Assert each phase's records carry the right profile_id + content_hash.
    """
    resolver = CalibrationResolver(profiles_root)
    initial_id = resolver.default().profile_id
    initial_hash = resolver.default().content_hash()
    assert initial_id == "v5_default"

    # Phase A: 3 events on default profile.
    src_a = SyntheticRawFlowSource("synthetic", [_raw(f"a-{i}", i) for i in range(3)])
    pipeline_a = Pipeline(
        [src_a],
        list(default_stage_pipeline()),
        resolver=resolver,
        decay_watcher_enabled=False,  # deterministic; no walltime task
    )
    results_a = await pipeline_a.run()
    assert len(results_a) == 3
    for r in results_a:
        assert r.record is not None
        assert r.record.profile_id == "v5_default"
        assert r.record.profile_content_hash == initial_hash

    # --- Modify the profile YAML on disk: change profile_id and one tunable
    # leaf so the content_hash differs from the initial load. We do this
    # in-place rather than copying example_ticker_override.yaml because
    # the override declares ``inherits_from: v5_default`` — copying it over
    # the v5_default.yaml path itself would create an inheritance cycle.
    new_yaml = (profiles_root / "v5_default.yaml").read_text().replace(
        "profile_id: v5_default",
        "profile_id: hot_swap_target",
    ).replace(
        "median_window_days: 30",
        "median_window_days: 60",
    )
    (profiles_root / "v5_default.yaml").write_text(new_yaml)
    assert resolver.reload() is True
    new_id = resolver.default().profile_id
    new_hash = resolver.default().content_hash()
    assert new_id == "hot_swap_target"
    assert new_hash != initial_hash

    # Phase B: 3 events on the new profile.
    src_b = SyntheticRawFlowSource(
        "synthetic", [_raw(f"b-{i}", 10 + i) for i in range(3)],
    )
    pipeline_b = Pipeline(
        [src_b],
        list(default_stage_pipeline()),
        resolver=resolver,
        decay_watcher_enabled=False,
    )
    results_b = await pipeline_b.run()
    assert len(results_b) == 3
    for r in results_b:
        assert r.record is not None
        assert r.record.profile_id == "hot_swap_target"
        assert r.record.profile_content_hash == new_hash


# ---------------------------------------------------------------------------
# In-flight isolation: reload mid-event
# ---------------------------------------------------------------------------


class _ReloadTriggeringStage:
    """Stage that calls ``resolver.reload()`` while it runs.

    Used to verify that an event whose snapshot was taken BEFORE the stage
    triggered the reload still completes on the pre-reload profile.
    """

    name = "reload_trigger"

    def __init__(
        self,
        resolver: CalibrationResolver,
        profiles_root: Path,
    ) -> None:
        self._resolver = resolver
        self._profiles_root = profiles_root
        self._fired = False

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent:
        if not self._fired:
            yaml_path = self._profiles_root / "v5_default.yaml"
            new_yaml = yaml_path.read_text().replace(
                "profile_id: v5_default",
                "profile_id: hot_swap_target",
            ).replace(
                "median_window_days: 30",
                "median_window_days: 60",
            )
            yaml_path.write_text(new_yaml)
            self._resolver.reload()
            self._fired = True
        return event


@pytest.mark.asyncio
async def test_in_flight_event_completes_on_old_profile(
    profiles_root: Path,
) -> None:
    """An event whose process_one snapshotted the OLD profile must complete
    on that snapshot, even if a stage triggers reload() mid-event.
    """
    resolver = CalibrationResolver(profiles_root)
    initial_hash = resolver.default().content_hash()

    # Stage list: trigger reload BEFORE the standard pipeline runs.
    # The pipeline's snapshot was taken at process_one entry; downstream
    # stages and engines must use that snapshot, not what the resolver
    # returns now.
    stages = [
        _ReloadTriggeringStage(resolver, profiles_root),
        *default_stage_pipeline(),
    ]
    src = SyntheticRawFlowSource("synthetic", [_raw("e-0", 0)])
    pipeline = Pipeline(
        [src],
        stages,
        resolver=resolver,
        decay_watcher_enabled=False,
    )

    results = await pipeline.run()
    assert len(results) == 1
    assert results[0].record is not None

    # The event's record should carry the PRE-reload profile snapshot.
    assert results[0].record.profile_id == "v5_default"
    assert results[0].record.profile_content_hash == initial_hash

    # And the resolver's default has indeed been swapped underneath.
    assert resolver.default().profile_id == "hot_swap_target"
    assert resolver.default().content_hash() != initial_hash


# ---------------------------------------------------------------------------
# Edge case: malformed YAML retains previous state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_yaml_reload_retains_previous_state(
    profiles_root: Path,
) -> None:
    """Corrupting the profile YAML and calling reload() must:
      - return False (signalling the swap was skipped)
      - leave resolver.default() returning the prior valid profile
      - not raise (pipeline must continue uninterrupted)
    """
    resolver = CalibrationResolver(profiles_root)
    valid_hash = resolver.default().content_hash()
    valid_id = resolver.default().profile_id

    # Corrupt the file — this is invalid YAML.
    (profiles_root / "v5_default.yaml").write_text(
        "this: is: : invalid: yaml: : :\n[broken",
    )

    # reload() must not raise, must return False.
    swapped = resolver.reload()
    assert swapped is False

    # Resolver retains prior state.
    assert resolver.default().profile_id == valid_id
    assert resolver.default().content_hash() == valid_hash

    # Pipeline can still process events on the retained profile.
    src = SyntheticRawFlowSource("synthetic", [_raw("e-0", 0)])
    pipeline = Pipeline(
        [src],
        list(default_stage_pipeline()),
        resolver=resolver,
        decay_watcher_enabled=False,
    )
    results = await pipeline.run()
    assert len(results) == 1
    assert results[0].record is not None
    assert results[0].record.profile_content_hash == valid_hash


# ---------------------------------------------------------------------------
# Edge case: identical reload is silent
# ---------------------------------------------------------------------------


def test_identical_reload_is_silent(
    profiles_root: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second reload() with no file change emits no
    'calibration_resolver_reloaded' info-level log line.

    Captures both stdout (default structlog config writes there directly)
    and stdlib log records (captured by caplog if a previous test wired
    structlog through stdlib via ``CLI._configure_logging``). Cross-test
    interactions between the CLI integration tests and these resolver
    tests can change structlog's destination, so we check both sinks.
    """
    resolver = CalibrationResolver(profiles_root)

    # Drain stdout/caplog from the initial __init__ reload.
    capsys.readouterr()
    caplog.clear()

    # Second reload — file unchanged.
    with caplog.at_level("INFO"):
        result = resolver.reload()
    assert result is True

    captured = capsys.readouterr()
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    text = captured.out + captured.err + log_text

    assert "calibration_resolver_reloaded" not in text, (
        f"identical reload should be silent; got: {text!r}"
    )


def test_changed_reload_does_log(
    profiles_root: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Companion to the silent-reload test: a reload where content changed
    DOES log. Without this, the previous test could be vacuously true.

    Same dual-sink approach as ``test_identical_reload_is_silent`` so the
    test is robust to structlog reconfiguration by other test files.
    """
    resolver = CalibrationResolver(profiles_root)
    capsys.readouterr()
    caplog.clear()

    yaml_path = profiles_root / "v5_default.yaml"
    new_yaml = yaml_path.read_text().replace(
        "median_window_days: 30",
        "median_window_days: 60",
    )
    yaml_path.write_text(new_yaml)
    with caplog.at_level("INFO"):
        resolver.reload()

    captured = capsys.readouterr()
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    text = captured.out + captured.err + log_text

    assert "calibration_resolver_reloaded" in text, (
        f"changed reload should log; got: {text!r}"
    )


# ---------------------------------------------------------------------------
# Edge case: rapid consecutive reloads
# ---------------------------------------------------------------------------


def test_rapid_consecutive_reloads_only_latest_sticks(
    profiles_root: Path,
) -> None:
    """Three different YAML contents written and reloaded back-to-back.
    Only the last reload's content survives in the resolver state.
    """
    resolver = CalibrationResolver(profiles_root)

    # Build three distinct profile contents (all valid). Each is the
    # default v5 with a different profile_id.
    base = (profiles_root / "v5_default.yaml").read_text()

    def with_id(new_id: str) -> str:
        return base.replace("profile_id: v5_default", f"profile_id: {new_id}")

    # Reload A.
    (profiles_root / "v5_default.yaml").write_text(with_id("variant_a"))
    assert resolver.reload() is True
    # Reload B.
    (profiles_root / "v5_default.yaml").write_text(with_id("variant_b"))
    assert resolver.reload() is True
    # Reload C.
    (profiles_root / "v5_default.yaml").write_text(with_id("variant_c"))
    assert resolver.reload() is True

    # Only C is observable.
    assert resolver.default().profile_id == "variant_c"
