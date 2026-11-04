"""Read-only integrity boundary for the frozen JSON research artifact."""
from __future__ import annotations

import gzip
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from webapp.history_capture.capture import digest
from webapp.history_capture.report import load as original_report
from webapp.horizon_research.model import ROOT, code_hash, profile_hash, settings

DIRECTORY = ROOT / "artifacts/horizon-research-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path, *, ledger: bool = False, require_audit: bool = True) -> dict[str, Any]:
    """JSON dictionaries are an explicitly untyped artifact adapter, never signals."""
    body: dict[str, Any] = json.loads(path.read_text())
    checksum = body.pop("report_hash")
    if digest(body) != checksum:
        raise ValueError("horizon report checksum mismatch")
    cfg = settings()
    original = original_report()
    if (
        body["profile_hash"] != profile_hash()
        or body["code_hash"] != code_hash()
        or body["original_report_hash"] != original["report_hash"]
        or body["source_packet_hashes"] != [list(p) for p in sorted(original["source_packet_hashes"].items())]
        or body["cli_sha256"] != sha256(ROOT / "scripts/horizon_research.py")
        or len(body["rows"]) != cfg.registered_cells
        or body.get("winner") is not None
        or body.get("validated_alpha", False)
    ):
        raise ValueError("horizon implementation/profile/source mismatch")
    if body["ledger_file"] != "units.json.gz":
        raise ValueError("unexpected horizon ledger path")
    units = path.parent / body["ledger_file"]
    if sha256(units) != body["ledger_sha256"]:
        raise ValueError("horizon ledger checksum mismatch")
    if ledger:
        data = json.loads(gzip.decompress(units.read_bytes()))
        if digest(data) != body["ledger_content_hash"]:
            raise ValueError("horizon ledger content mismatch")
        body["units"] = data
    if require_audit:
        audit: dict[str, Any] = json.loads((path.parent / "audit.json").read_text())
        audit_checksum = audit.pop("audit_hash")
        if (
            digest(audit) != audit_checksum
            or audit["report_hash"] != checksum
            or audit["source_hash"] != body["source_hash"]
            or audit["status"] != "RECONCILED"
            or audit["audit_script_sha256"] != sha256(ROOT / "scripts/audit_horizon_research.py")
        ):
            raise ValueError("horizon independent audit mismatch")
        body["audit"] = audit | {"audit_hash": audit_checksum}
    if not ledger:
        # The web view retains every cell's numbers and coverage. Repeated long
        # slot identifiers belong to the standalone evidence, not each GET.
        for row in body["rows"]:
            for field in ("unit_keys", "false_signal_keys", "unknown_signal_keys"):
                row.pop(field, None)
    return body | {"report_hash": checksum}


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return read(DIRECTORY / "report.json")
