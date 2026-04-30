"""YAML profile loader with deep-merge inheritance.

Why deep-merge: a child profile overriding only ``relative_premium.median_window_days``
should leave every other field of ``relative_premium`` intact. Shallow merge
would force users to copy entire nested blocks to change one number.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog
from ruamel.yaml import YAML

from uoa_detector.calibration.profile import CalibrationProfile
from uoa_detector.errors import ConfigurationError

_logger = structlog.get_logger(__name__)
_yaml = YAML(typ="safe")

# Fields the spec marks as commonly tuned — warn if a non-default profile
# loads without overriding them. Extend over time.
_COMMONLY_TUNED: tuple[str, ...] = (
    "relative_premium.median_window_days",
    "cluster.window_minutes",
)


def _read_yaml(path: Path) -> Mapping[str, Any]:
    """Read a YAML file as a dict. Raise ``ConfigurationError`` on read failure."""
    try:
        with path.open() as f:
            data = _yaml.load(f)
    except OSError as e:
        msg = f"failed to read profile file {path}: {e}"
        raise ConfigurationError(msg) from e
    if not isinstance(data, dict):
        msg = f"profile file {path} must be a YAML mapping at root, got {type(data).__name__}"
        raise ConfigurationError(msg)
    return data


def _deep_merge(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``child`` into ``parent``.

    Leaf-level overrides: if both sides have a dict at the same key, recurse;
    otherwise the child value wins. Lists do NOT merge — child replaces parent
    wholesale. This matches the deep-merge behavior described in the prompt.
    """
    out: dict[str, Any] = dict(parent)
    for k, v in child.items():
        if k in out and isinstance(out[k], Mapping) and isinstance(v, Mapping):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_chain(
    leaf: Mapping[str, Any],
    profiles_dir: Path,
    seen: set[str] | None = None,
) -> dict[str, Any]:
    """Walk the ``inherits_from`` chain, deep-merging from root → leaf.

    Cycle detection raises ``ConfigurationError``.
    """
    seen = seen or set()
    pid = leaf.get("profile_id")
    if not isinstance(pid, str):
        msg = "profile is missing required 'profile_id' string field"
        raise ConfigurationError(msg)
    if pid in seen:
        msg = f"profile inheritance cycle detected at {pid!r}"
        raise ConfigurationError(msg)
    seen = seen | {pid}

    parent_id = leaf.get("inherits_from")
    if parent_id is None:
        return dict(leaf)

    if not isinstance(parent_id, str):
        msg = f"profile {pid!r}: inherits_from must be a string or null"
        raise ConfigurationError(msg)

    parent_path = profiles_dir / f"{parent_id}.yaml"
    if not parent_path.exists():
        msg = f"profile {pid!r} inherits from {parent_id!r}, but {parent_path} not found"
        raise ConfigurationError(msg)

    parent_raw = _read_yaml(parent_path)
    parent_resolved = _resolve_chain(parent_raw, profiles_dir, seen)

    # Child wins on collisions; child's profile_id and description always
    # replace parent's, but inherits_from is dropped from the merged result
    # to prevent confusing re-inheritance.
    merged = _deep_merge(parent_resolved, leaf)
    merged["inherits_from"] = parent_id
    return merged


def _profile_hash(profile: CalibrationProfile) -> str:
    """SHA-256 of a canonical JSON serialization of the profile.

    Used in ``SignalDecisionRecord`` so analysts can verify which profile
    produced which signals across long backtests.
    """
    payload = profile.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _warn_if_commonly_tuned_missing(
    profile: CalibrationProfile,
    raw_leaf: Mapping[str, Any],
) -> None:
    """Log a warning if a non-default profile didn't override commonly-tuned fields."""
    if profile.profile_id == "v5_default":
        return  # the default IS the source of truth — no warnings
    for dotted in _COMMONLY_TUNED:
        parts = dotted.split(".")
        cursor: Any = raw_leaf
        present = True
        for p in parts:
            if isinstance(cursor, Mapping) and p in cursor:
                cursor = cursor[p]
            else:
                present = False
                break
        if not present:
            _logger.warning(
                "profile_missing_commonly_tuned_field",
                profile_id=profile.profile_id,
                field=dotted,
            )


def load_profile(path: Path | str, *, profiles_dir: Path | str | None = None) -> CalibrationProfile:
    """Load a profile from ``path``, applying deep-merge inheritance.

    :param path: Path to the leaf YAML file.
    :param profiles_dir: Directory to search for parent profiles named in
        ``inherits_from``. Defaults to the leaf's containing directory.
    """
    leaf_path = Path(path).expanduser().resolve()
    base_dir = Path(profiles_dir).expanduser().resolve() if profiles_dir else leaf_path.parent

    raw_leaf = _read_yaml(leaf_path)
    merged = _resolve_chain(raw_leaf, base_dir)

    try:
        profile = CalibrationProfile.model_validate(merged)
    except Exception as e:
        msg = f"profile validation failed for {leaf_path}: {e}"
        raise ConfigurationError(msg) from e

    _warn_if_commonly_tuned_missing(profile, raw_leaf)
    return profile


def load_default_profile(*, profiles_dir: Path | str = "profiles") -> CalibrationProfile:
    """Convenience: load ``profiles/v5_default.yaml``."""
    base = Path(profiles_dir).expanduser().resolve()
    return load_profile(base / "v5_default.yaml", profiles_dir=base)


def profile_hash(profile: CalibrationProfile) -> str:
    """Public helper — exported for the decision-record writer."""
    return _profile_hash(profile)
