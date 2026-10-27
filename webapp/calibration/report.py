"""Read-only frozen artifact, validated before use in prospective monitoring."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from webapp.alpha_lab.db import digest
from webapp.calibration.search import code_hash
from webapp.calibration.settings import settings

REPORT_PATH = Path(__file__).resolve().parents[2] / "artifacts/calibration-v1/report.json"


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    # Any confined to this immutable report's JSON adapter.
    body: dict[str, Any] = json.loads(REPORT_PATH.read_text())
    checksum = body.pop("report_sha256")
    if body.get("profile_sha256") != settings().profile_hash or digest(body) != checksum or body.get("code_sha256") != code_hash():
        raise ValueError("frozen calibration report checksum/profile mismatch")
    return body | {"report_sha256": checksum}
