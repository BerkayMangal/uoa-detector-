"""Immutable calibration configuration can be loaded by simultaneous services."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from uoa_detector.calibration.loader import load_profile


@pytest.mark.parametrize("same_profile", [False, True])
def test_parallel_loaders_preserve_exact_profile_values_and_hashes(same_profile: bool) -> None:
    root = Path(__file__).resolve().parents[2]
    names = (
        ["v5_default.yaml", "v5_gamma_squeeze.yaml"] if not same_profile else ["v5_default.yaml"]
    )
    paths = [root / "profiles" / name for name in names]
    expected = {
        path: (load_profile(path).model_dump(mode="json"), load_profile(path).content_hash())
        for path in paths
    }

    def read(path: Path) -> None:
        profile = load_profile(path)
        assert (profile.model_dump(mode="json"), profile.content_hash()) == expected[path]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(read, paths * 16))
