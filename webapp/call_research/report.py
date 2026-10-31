"""Integrity-checked compact artifact; JSON is the only untyped adapter."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

from webapp.call_research.settings import ROOT, profile_hash, settings

DIRECTORY = ROOT / "artifacts/call-research-v1"
RESEARCH_FILES = (
    "webapp/call_research/settings.py",
    "webapp/call_research/data.py",
    "webapp/call_research/backtest.py",
    "webapp/call_research/offline.py",
    "webapp/call_research/report.py",
    "webapp/calibration/model.py",
    "webapp/calibration/settings.py",
    "webapp/alpha_lab/research.py",
    "src/uoa_detector/live_alpha/calendar.py",
    "profiles/live_alpha_v1.yaml",
)


def code_hash() -> str:
    return hashlib.sha256(
        b"".join(name.encode() + (ROOT / name).read_bytes() for name in RESEARCH_FILES)
    ).hexdigest()


def checksum(body: object) -> str:
    return hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    body: dict[str, Any] = json.loads((DIRECTORY / "report.json").read_text())
    digest = body.pop("report_hash")
    if (
        checksum(body) != digest
        or body["profile_hash"] != profile_hash()
        or body["code_hash"] != code_hash()
        or body["source_hash"] != settings().source_sha256
    ):
        raise ValueError("call research artifact/profile/implementation mismatch")
    return body | {"report_hash": digest}
