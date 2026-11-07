"""Bounded sealed operational diagnostics, never a live decision input."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1] / "artifacts/fusion-context-v1"
MAX_BYTES = 262144


def seal(body: dict[str, Any]) -> dict[str, Any]:
    # Any is confined to this public-summary JSON adapter, not a feature model.
    raw = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return {**body, "artifact_hash": hashlib.sha256(raw).hexdigest()}


def load(name: Literal["capability", "semantic-capability"]) -> dict[str, Any]:
    path = ROOT / (name + ".json")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("capability_summary_limit")
    raw = path.read_bytes()

    def invalid(_: str) -> None:
        raise ValueError("nonfinite_capability_summary")

    body = json.loads(raw, parse_constant=invalid)
    if not isinstance(body, dict):
        raise ValueError("capability_summary_invalid")
    checksum = body.pop("artifact_hash", None)
    verified = seal(body)
    if checksum != verified["artifact_hash"] or body.get("inference") != "NOT_PERFORMED":
        raise ValueError("capability_summary_integrity")
    return verified
