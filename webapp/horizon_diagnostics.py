"""Integrity-checked post-hoc JSON worksheet; no calculation on a web GET."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

from webapp.history_capture.capture import digest
from webapp.horizon_research.model import ROOT, settings
from webapp.horizon_research.report import load as horizon_report

PATH = ROOT / "artifacts/horizon-research-v1/description.json"


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    # JSON is the intentionally untyped artifact boundary; this is not a signal.
    body: dict[str, Any] = json.loads(PATH.read_text())
    checksum = body.pop("description_hash")
    original = horizon_report()
    if (
        digest(body) != checksum
        or body["classification"] != "POSTHOC_DESCRIPTIVE_CONCENTRATION_NOT_NEW_VALIDATION"
        or body["report_hash"] != original["report_hash"]
        or body["audit_hash"] != original["audit"]["audit_hash"]
        or body["script_sha256"] != hashlib.sha256((ROOT / "scripts/describe_horizon_panel.py").read_bytes()).hexdigest()
        or body["winner"] is not None
        or body["validated_alpha"]
        or len(body["rows"]) != len(original["primary_family"]["contrasts"]) * len(settings().costs_bps_per_side)
    ):
        raise ValueError("horizon post-hoc worksheet mismatch")
    return body | {"description_hash": checksum}
