"""Tests for YAML loading and deep-merge inheritance.

The worked example required by the user:
    A child profile that overrides only ``relative_premium.median_window_days``
    must inherit every other ``relative_premium.*`` field unchanged.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from uoa_detector.calibration import (
    CalibrationProfile,
    load_default_profile,
    load_profile,
    profile_hash,
)
from uoa_detector.errors import ConfigurationError


@pytest.fixture
def profiles_root(tmp_path: Path) -> Path:
    src = Path("profiles").resolve()
    dst = tmp_path / "profiles"
    shutil.copytree(src, dst)
    return dst


def test_example_ticker_override_loads(profiles_root: Path) -> None:
    """The example child profile loads and its overrides take effect."""
    p = load_profile(
        profiles_root / "example_ticker_override.yaml",
        profiles_dir=profiles_root,
    )
    assert p.profile_id == "example_ticker_override"
    assert p.inherits_from == "v5_default"
    assert p.relative_premium.median_window_days == 60  # overridden
    assert p.cluster.window_minutes == 90  # overridden


def test_deep_merge_preserves_non_overridden_leaves_in_same_block(profiles_root: Path) -> None:
    """Worked example: child overrides only ``relative_premium.median_window_days``.

    All other ``relative_premium.*`` fields must come from the parent (v5_default).
    """
    child_path = profiles_root / "tickers" / "WORKED.yaml"
    child_path.write_text(
        "profile_id: worked_example\n"
        "description: deep-merge worked example\n"
        "inherits_from: v5_default\n"
        "relative_premium:\n"
        "  median_window_days: 60   # only this leaf changes\n"
    )

    child = load_profile(child_path, profiles_dir=profiles_root)
    parent = load_default_profile(profiles_dir=profiles_root)

    # The one overridden field
    assert child.relative_premium.median_window_days == 60
    assert parent.relative_premium.median_window_days == 30  # parent unchanged

    # Every OTHER relative_premium.* field is inherited intact
    assert child.relative_premium.high_ratio == parent.relative_premium.high_ratio
    assert child.relative_premium.mid_ratio == parent.relative_premium.mid_ratio
    assert child.relative_premium.score_high == parent.relative_premium.score_high
    assert child.relative_premium.score_mid == parent.relative_premium.score_mid
    assert child.relative_premium.score_low == parent.relative_premium.score_low

    # Every OTHER block is also inherited intact
    assert child.scoring == parent.scoring
    assert child.penalties == parent.penalties
    assert child.dte == parent.dte
    assert child.label_thresholds == parent.label_thresholds
    assert child.fusion == parent.fusion


def test_multiple_blocks_partial_override(profiles_root: Path) -> None:
    """Override leaves across multiple blocks; rest inherits."""
    child_path = profiles_root / "tickers" / "MULTI.yaml"
    child_path.write_text(
        "profile_id: multi_override\n"
        "description: multi-block override\n"
        "inherits_from: v5_default\n"
        "cluster:\n"
        "  window_minutes: 30\n"
        "label_thresholds:\n"
        "  cluster_burst: 0.95\n"
    )
    child = load_profile(child_path, profiles_dir=profiles_root)
    parent = load_default_profile(profiles_dir=profiles_root)

    assert child.cluster.window_minutes == 30
    assert child.cluster.decay_minutes == parent.cluster.decay_minutes  # inherited
    assert child.cluster.density_three_same == parent.cluster.density_three_same

    assert child.label_thresholds.cluster_burst == 0.95
    assert child.label_thresholds.cluster_min == parent.label_thresholds.cluster_min


def test_inheritance_cycle_detected(profiles_root: Path) -> None:
    """A → B → A should raise rather than infinite-loop."""
    a = profiles_root / "a.yaml"
    b = profiles_root / "b.yaml"
    a.write_text("profile_id: a\ndescription: x\ninherits_from: b\n")
    b.write_text("profile_id: b\ndescription: x\ninherits_from: a\n")
    with pytest.raises(ConfigurationError, match="cycle"):
        load_profile(a, profiles_dir=profiles_root)


def test_missing_parent_raises(profiles_root: Path) -> None:
    orphan = profiles_root / "orphan.yaml"
    orphan.write_text(
        "profile_id: orphan\ndescription: x\ninherits_from: nonexistent\n"
    )
    with pytest.raises(ConfigurationError, match="not found"):
        load_profile(orphan, profiles_dir=profiles_root)


def test_profile_hash_stable_for_same_content(profiles_root: Path) -> None:
    p1 = load_default_profile(profiles_dir=profiles_root)
    p2 = load_default_profile(profiles_dir=profiles_root)
    assert profile_hash(p1) == profile_hash(p2)


def test_profile_hash_changes_with_content(profiles_root: Path) -> None:
    parent = load_default_profile(profiles_dir=profiles_root)
    child_path = profiles_root / "tickers" / "DIFF.yaml"
    child_path.write_text(
        "profile_id: diff\ndescription: x\ninherits_from: v5_default\n"
        "cluster: { window_minutes: 31 }\n"
    )
    child = load_profile(child_path, profiles_dir=profiles_root)
    assert profile_hash(parent) != profile_hash(child)


def test_loaded_profile_is_a_calibration_profile(profiles_root: Path) -> None:
    p = load_default_profile(profiles_dir=profiles_root)
    assert isinstance(p, CalibrationProfile)


def test_warning_logged_for_commonly_tuned_missing(
    profiles_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-default profile that doesn't override a 'commonly tuned' field warns."""
    child = profiles_root / "tickers" / "NOTUNE.yaml"
    child.write_text(
        "profile_id: notune\ndescription: x\ninherits_from: v5_default\n"
    )
    load_profile(child, profiles_dir=profiles_root)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    # structlog emits the warning to stdout via its default writer.
    assert "profile_missing_commonly_tuned_field" in text
    assert "median_window_days" in text or "window_minutes" in text
