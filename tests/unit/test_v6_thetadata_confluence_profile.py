"""Pin tests for ``profiles/v6_thetadata_confluence.yaml`` (Phase 5.0.11).

v6 inherits ``v5_gamma_squeeze`` and overrides exactly one leaf:
``scoring.modules.m21.short_gamma_threshold = 0`` (Phase 3.6 D4: a sign-based
net-short test for the GEX self-derived from the ThetaData chain). The track is
burned for validation; these tests pin the profile so an edit to either YAML
cannot silently change what the v6 verdict ran against.

Identity fields (``profile_id``, ``description``, ``inherits_from``) are
excluded from the leaf comparison. ``content_hash()`` is not compared: it
necessarily differs because the identity fields differ.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from uoa_detector.calibration.loader import load_profile
from uoa_detector.calibration.profile import CalibrationProfile

_PROFILES_DIR = Path("profiles")
_IDENTITY_FIELDS = {"profile_id", "description", "inherits_from"}
_OVERRIDE_LEAF = "scoring.modules.m21.short_gamma_threshold"


@pytest.fixture(scope="module")
def v6_profile() -> CalibrationProfile:
    return load_profile(_PROFILES_DIR / "v6_thetadata_confluence.yaml")


@pytest.fixture(scope="module")
def gamma_squeeze_profile() -> CalibrationProfile:
    return load_profile(_PROFILES_DIR / "v5_gamma_squeeze.yaml")


def _leaves(node: object, prefix: str = "") -> dict[str, object]:
    """Flatten a model dump to ``{"a.b.c": value}``; lists stay whole leaves."""
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, value in node.items():
            out.update(_leaves(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: node}


def _non_identity_leaves(profile: CalibrationProfile) -> dict[str, object]:
    return _leaves(profile.model_dump(exclude=_IDENTITY_FIELDS))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_profile_id(v6_profile: CalibrationProfile) -> None:
    assert v6_profile.profile_id == "v6_thetadata_confluence"


def test_inherits_from_gamma_squeeze(v6_profile: CalibrationProfile) -> None:
    assert v6_profile.inherits_from == "v5_gamma_squeeze"


# ---------------------------------------------------------------------------
# The single override
# ---------------------------------------------------------------------------


def test_m21_short_gamma_threshold_is_sign_based_zero(v6_profile: CalibrationProfile) -> None:
    assert v6_profile.scoring.modules.m21.short_gamma_threshold == Decimal(0)


def test_override_differs_from_parent(gamma_squeeze_profile: CalibrationProfile) -> None:
    """Keeps the inheritance check below from passing vacuously."""
    assert gamma_squeeze_profile.scoring.modules.m21.short_gamma_threshold != Decimal(0)


# ---------------------------------------------------------------------------
# Inheritance: every other leaf equals v5_gamma_squeeze
# ---------------------------------------------------------------------------


def test_same_leaf_set_as_gamma_squeeze(
    v6_profile: CalibrationProfile, gamma_squeeze_profile: CalibrationProfile,
) -> None:
    assert _non_identity_leaves(v6_profile).keys() == _non_identity_leaves(
        gamma_squeeze_profile,
    ).keys()


def test_only_the_m21_leaf_differs_from_gamma_squeeze(
    v6_profile: CalibrationProfile, gamma_squeeze_profile: CalibrationProfile,
) -> None:
    child = _non_identity_leaves(v6_profile)
    parent = _non_identity_leaves(gamma_squeeze_profile)
    differing = {key for key in child.keys() | parent.keys() if child.get(key) != parent.get(key)}
    assert differing == {_OVERRIDE_LEAF}


def test_every_other_leaf_equals_gamma_squeeze(
    v6_profile: CalibrationProfile, gamma_squeeze_profile: CalibrationProfile,
) -> None:
    child = _non_identity_leaves(v6_profile)
    parent = _non_identity_leaves(gamma_squeeze_profile)
    del child[_OVERRIDE_LEAF]
    del parent[_OVERRIDE_LEAF]
    assert child == parent
