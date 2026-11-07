"""Integrity-checked, read-only historical diagnostic; JSON is the untyped adapter."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

from ruamel.yaml import YAML

from webapp.history_capture.capture import ROOT, digest

DIRECTORY = ROOT / "artifacts/vendor-history-v1"
DEPENDENCIES = (
    "webapp/history_capture/capture.py",
    "src/uoa_detector/live_alpha/calendar.py",
    "src/uoa_detector/live_alpha/settings.py",
    "profiles/live_alpha_v1.yaml",
)
_COMPATIBILITY_PATH = ROOT / "artifacts/runtime-profile-cache-v1/manifest.json"
_COMPATIBILITY_SHA256 = "035bf203d7d8052445ce3658be87c329c25453db6eb0e3667f9840ebce64cc82"
_COMPATIBILITY_FIELDS = frozenset(
    (
        "version",
        "reason",
        "dependency",
        "original_dependency_sha256",
        "runtime_dependency_sha256",
        "profile_path",
        "profile_sha256",
        "report_path",
        "report_hash",
        "audit_path",
        "audit_hash",
    )
)


def file_hash(name: str) -> str:
    return hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def profile_hash(name: str) -> str:
    return digest(YAML(typ="safe").load((ROOT / name).read_text()))


def _compatible_dependencies(
    recorded: object,
    current: dict[str, str],
    report_hash: str,
    audit_hash: str,
) -> bool:
    """Allow one pinned cache-only runtime pair, never a new historical result."""
    if (
        not isinstance(recorded, dict)
        or recorded.keys() != set(DEPENDENCIES)
        or current.keys() != set(DEPENDENCIES)
    ):
        return False
    if recorded == current:
        return True
    dependency = "src/uoa_detector/live_alpha/settings.py"
    if any(recorded[name] != actual for name, actual in current.items() if name != dependency):
        return False
    try:
        raw = _COMPATIBILITY_PATH.read_bytes()
        if len(raw) > 4096 or hashlib.sha256(raw).hexdigest() != _COMPATIBILITY_SHA256:
            return False
        decoded = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(decoded, dict) or decoded.keys() != _COMPATIBILITY_FIELDS:
        return False
    manifest: dict[str, str] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return False
        manifest[key] = value
    profile = "profiles/live_alpha_v1.yaml"
    return (
        manifest["version"] == "runtime_profile_cache_v1"
        and manifest["reason"] == "exact-byte immutable profile parse cache only"
        and manifest["dependency"] == dependency
        and manifest["profile_path"] == profile
        and manifest["report_path"] == "artifacts/vendor-history-v1/report.json"
        and manifest["audit_path"] == "artifacts/vendor-history-v1/audit.json"
        and recorded[dependency] == manifest["original_dependency_sha256"]
        and current[dependency] == manifest["runtime_dependency_sha256"]
        and current[profile] == manifest["profile_sha256"]
        and report_hash == manifest["report_hash"]
        and audit_hash == manifest["audit_hash"]
    )


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    body: dict[str, Any] = json.loads((DIRECTORY / "report.json").read_text())
    checksum = body.pop("report_hash")
    audit: dict[str, Any] = json.loads((DIRECTORY / "audit.json").read_text())
    audit_checksum = audit.pop("audit_hash")
    if (
        digest(body) != checksum
        or digest(audit) != audit_checksum
        or audit["original_report_hash"] != checksum
        or audit["status"] != "RECONCILED"
        or audit["script_sha256"] != file_hash("scripts/audit_vendor_history.py")
        or body["script_hash"] != file_hash("scripts/diagnose_vendor_history.py")
        or body["profile_hash"] != profile_hash("profiles/history_diagnostic_v1.yaml")
        or body["timing_controls"]["profile_hash"]
        != profile_hash("profiles/history_timing_control_v1.yaml")
        or not _compatible_dependencies(
            body["dependency_hashes"],
            {name: file_hash(name) for name in DEPENDENCIES},
            checksum,
            audit_checksum,
        )
        or body["status"] != "EXPLORATORY_FIXED_DIAGNOSTICS"
        or body["classification"] != "EXPLORATORY_VENDOR_EVENT_TIME_ASSUMPTION"
        or body["winner"] is not None
        or body["validated_alpha"]
        or body["full_438_calibration_executed"]
        or len(body["rows"]) != body["executed_fixed_replays"]
    ):
        raise ValueError("vendor history artifact/profile/implementation mismatch")
    return body | {"report_hash": checksum, "audit": audit | {"audit_hash": audit_checksum}}
