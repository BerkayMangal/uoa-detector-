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


def file_hash(name: str) -> str:
    return hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def profile_hash(name: str) -> str:
    return digest(YAML(typ="safe").load((ROOT / name).read_text()))


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
        or body["dependency_hashes"] != {name: file_hash(name) for name in DEPENDENCIES}
        or body["status"] != "EXPLORATORY_FIXED_DIAGNOSTICS"
        or body["classification"] != "EXPLORATORY_VENDOR_EVENT_TIME_ASSUMPTION"
        or body["winner"] is not None
        or body["validated_alpha"]
        or body["full_438_calibration_executed"]
        or len(body["rows"]) != body["executed_fixed_replays"]
    ):
        raise ValueError("vendor history artifact/profile/implementation mismatch")
    return body | {"report_hash": checksum, "audit": audit | {"audit_hash": audit_checksum}}
