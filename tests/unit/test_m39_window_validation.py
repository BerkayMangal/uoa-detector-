"""Validation tests for ``TimeOfDayWeights`` window calibration.

Each rejection case here corresponds to a specific failure mode that misuse
of the calibration layer might introduce — gaps, overlaps, malformed windows,
duplicate labels, bad timezone, out-of-range outside weight.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from uoa_detector.calibration import (
    TimeOfDayWeights,
    TimeWindow,
    load_profile,
)
from uoa_detector.errors import ConfigurationError


def _w(start: str, end: str, weight: float = 0.5, label: str = "lbl") -> TimeWindow:
    return TimeWindow.model_validate(
        {"start": start, "end": end, "weight": weight, "label": label},
    )


# ---------------------------------------------------------------------------
# Direct model-level validation (no YAML)
# ---------------------------------------------------------------------------

def test_well_formed_windows_load() -> None:
    tod = TimeOfDayWeights.model_validate(
        {
            "timezone": "America/New_York",
            "windows": [
                {"start": "09:30", "end": "10:00", "weight": 0.3, "label": "a"},
                {"start": "10:00", "end": "11:00", "weight": 1.0, "label": "b"},
            ],
            "outside_session_weight": 0.0,
        },
    )
    assert tod.timezone == "America/New_York"
    assert len(tod.windows) == 2


def test_overlap_rejected() -> None:
    with pytest.raises(Exception, match="overlap"):
        TimeOfDayWeights.model_validate(
            {
                "timezone": "America/New_York",
                "windows": [
                    {"start": "09:30", "end": "10:30", "weight": 0.3, "label": "a"},
                    {"start": "10:00", "end": "11:00", "weight": 1.0, "label": "b"},
                ],
                "outside_session_weight": 0.0,
            },
        )


def test_gap_rejected() -> None:
    with pytest.raises(Exception, match="gap"):
        TimeOfDayWeights.model_validate(
            {
                "timezone": "America/New_York",
                "windows": [
                    {"start": "09:30", "end": "10:00", "weight": 0.3, "label": "a"},
                    {"start": "10:30", "end": "11:00", "weight": 1.0, "label": "b"},
                ],
                "outside_session_weight": 0.0,
            },
        )


def test_window_with_start_at_or_after_end_rejected() -> None:
    """A single window with start >= end is malformed."""
    with pytest.raises(Exception, match="start"):
        _w("10:00", "10:00")  # zero-width
    with pytest.raises(Exception, match="start"):
        _w("11:00", "10:00")  # inverted (would also be 'overnight')


def test_duplicate_window_labels_rejected() -> None:
    with pytest.raises(Exception, match="duplicate"):
        TimeOfDayWeights.model_validate(
            {
                "timezone": "America/New_York",
                "windows": [
                    {"start": "09:30", "end": "10:00", "weight": 0.3, "label": "x"},
                    {"start": "10:00", "end": "11:00", "weight": 1.0, "label": "x"},
                ],
                "outside_session_weight": 0.0,
            },
        )


def test_window_weight_out_of_range_rejected() -> None:
    with pytest.raises(Exception, match=r"(less than|greater)"):
        _w("09:30", "10:00", weight=1.5)
    with pytest.raises(Exception, match=r"(less than|greater)"):
        _w("09:30", "10:00", weight=-0.1)


def test_outside_session_weight_out_of_range_rejected() -> None:
    base = {
        "timezone": "America/New_York",
        "windows": [
            {"start": "09:30", "end": "16:00", "weight": 1.0, "label": "session"},
        ],
    }
    with pytest.raises(Exception, match=r"(less than|greater)"):
        TimeOfDayWeights.model_validate({**base, "outside_session_weight": 1.5})
    with pytest.raises(Exception, match=r"(less than|greater)"):
        TimeOfDayWeights.model_validate({**base, "outside_session_weight": -0.1})


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(Exception, match="unknown timezone"):
        TimeOfDayWeights.model_validate(
            {
                "timezone": "Mars/Olympus_Mons",
                "windows": [
                    {"start": "09:30", "end": "16:00", "weight": 1.0, "label": "session"},
                ],
                "outside_session_weight": 0.0,
            },
        )


def test_empty_windows_rejected() -> None:
    """Calibration without windows is meaningless — the lookup would always
    fall through to ``outside_session_weight``."""
    with pytest.raises(Exception, match=r"(min_length|at least)"):
        TimeOfDayWeights.model_validate(
            {
                "timezone": "America/New_York",
                "windows": [],
                "outside_session_weight": 0.0,
            },
        )


# ---------------------------------------------------------------------------
# YAML round-trip — same rejections fire when loading from a child profile
# ---------------------------------------------------------------------------

@pytest.fixture
def profiles_root(tmp_path: Path) -> Path:
    src = Path("profiles").resolve()
    dst = tmp_path / "profiles"
    shutil.copytree(src, dst)
    return dst


def test_overlap_in_yaml_rejected(profiles_root: Path) -> None:
    bad = profiles_root / "tickers" / "BAD.yaml"
    bad.write_text(
        "profile_id: bad\n"
        "description: x\n"
        "inherits_from: v5_default\n"
        "time_of_day:\n"
        "  timezone: 'America/New_York'\n"
        "  windows:\n"
        "    - { start: '09:30', end: '11:00', weight: 0.5, label: a }\n"
        "    - { start: '10:30', end: '12:00', weight: 0.5, label: b }\n"
        "  outside_session_weight: 0.0\n"
    )
    with pytest.raises(ConfigurationError):
        load_profile(bad, profiles_dir=profiles_root)


def test_gap_in_yaml_rejected(profiles_root: Path) -> None:
    bad = profiles_root / "tickers" / "BADGAP.yaml"
    bad.write_text(
        "profile_id: badgap\n"
        "description: x\n"
        "inherits_from: v5_default\n"
        "time_of_day:\n"
        "  timezone: 'America/New_York'\n"
        "  windows:\n"
        "    - { start: '09:30', end: '10:00', weight: 0.3, label: a }\n"
        "    - { start: '10:30', end: '11:00', weight: 1.0, label: b }\n"
        "  outside_session_weight: 0.0\n"
    )
    with pytest.raises(ConfigurationError):
        load_profile(bad, profiles_dir=profiles_root)


def test_alternative_session_loads_cleanly(profiles_root: Path) -> None:
    """A half-day session with different boundaries is a first-class case."""
    half = profiles_root / "regimes" / "half_day.yaml"
    half.write_text(
        "profile_id: half_day\n"
        "description: 'NYSE early-close: open auction → midday → 13:00 close'\n"
        "inherits_from: v5_default\n"
        "time_of_day:\n"
        "  timezone: 'America/New_York'\n"
        "  windows:\n"
        "    - { start: '09:30', end: '10:00', weight: 0.30, label: open_auction }\n"
        "    - { start: '10:00', end: '12:00', weight: 1.00, label: prime_session }\n"
        "    - { start: '12:00', end: '13:00', weight: 0.30, label: moc_loc }\n"
        "  outside_session_weight: 0.0\n"
    )
    p = load_profile(half, profiles_dir=profiles_root)
    assert len(p.time_of_day.windows) == 3
    from datetime import time as _t
    assert p.time_of_day.lookup(_t(11, 0)) == (1.00, "prime_session")
    assert p.time_of_day.lookup(_t(13, 30)) == (0.0, "outside_session")
