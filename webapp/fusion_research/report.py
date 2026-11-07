"""Bounded read-only stored research summary; GET never launches a search."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from webapp.fusion_research.model import digest
from webapp.fusion_research.settings import ROOT, identity, settings

DIRECTORY = ROOT / "artifacts/fusion-research-v1"


def unavailable(reason: str) -> dict[str, Any]:
    # Any is confined to the private JSON artifact/report adapter.
    return {
        "version": settings().version,
        "status": "UNAVAILABLE",
        "reason": reason,
        "classification": "EXPLORATORY_REUSE",
        "winner": None,
        "live_promotion": False,
        "validated_alpha": False,
        "inference": "NOT_READY",
        "configuration_trials": 0,
    }


def load(directory: Path = DIRECTORY) -> dict[str, Any]:
    try:
        path = directory / "report.json"
        if path.stat().st_size > settings().report_max_bytes:
            return unavailable("report_size_limit")
        body: object = json.loads(path.read_bytes())
        if not isinstance(body, dict):
            return unavailable("report_shape")
        if digest({k: v for k, v in body.items() if k != "report_hash"}) != body.get("report_hash"):
            return unavailable("report_hash")
        if any(body.get(k) != v for k, v in identity().items()):
            return unavailable("report_identity")
        if (
            body.get("classification") != "EXPLORATORY_REUSE"
            or body.get("winner") is not None
            or body.get("live_promotion") is not False
            or body.get("validated_alpha") is not False
            or body.get("inference") != "NOT_READY"
            or body.get("configuration_trials") != settings().registered_configurations
        ):
            return unavailable("report_scope")
        plan = json.loads((directory / "plan.json").read_bytes())
        if (
            not isinstance(plan, dict)
            or digest({k: v for k, v in plan.items() if k != "plan_hash"}) != body.get("plan_hash")
            or plan.get("plan_hash") != body.get("plan_hash")
        ):
            return unavailable("plan_hash")
        artifact = body.get("grid_artifact")
        if (
            not isinstance(artifact, dict)
            or artifact.get("name") != "grid.jsonl.gz"
            or artifact.get("rows") != body["configuration_trials"]
        ):
            return unavailable("grid_metadata")
        grid = directory / "grid.jsonl.gz"
        if grid.stat().st_size > settings().report_max_bytes or hashlib.sha256(
            grid.read_bytes()
        ).hexdigest() != artifact.get("sha256"):
            return unavailable("grid_hash")
        # Never decompress thousands of grid rows during an HTTP view.
        return body
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return unavailable("stored_report_unreadable")


def write(directory: Path, body: dict[str, Any]) -> None:
    """Offline only; never replace a prior result or immutable plan."""
    if (directory / "report.json").exists() or (directory / "grid.jsonl.gz").exists():
        raise ValueError("refusing to overwrite research evidence")
    grid = body.pop("grid")
    body.pop("report_hash")
    raw = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        for row in grid
    ).encode()
    encoded = gzip.compress(raw, mtime=0)
    with (directory / "grid.jsonl.gz").open("xb") as f:
        f.write(encoded)
    body["grid_artifact"] = {
        "name": "grid.jsonl.gz",
        "rows": len(grid),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
    }
    body["report_hash"] = digest(body)
    serialized = json.dumps(body, sort_keys=True, default=str, allow_nan=False, indent=2).encode()
    if len(serialized) > settings().report_max_bytes:
        raise ValueError("report_size_limit")
    with (directory / "report.json").open("xb") as f:
        f.write(serialized)
