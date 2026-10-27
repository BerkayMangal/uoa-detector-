"""Read a small integrity-checked artifact, never run research in an HTTP request."""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from webapp.alpha_lab.db import digest
from webapp.robustness.settings import settings
from webapp.tournament import report as tournament_report

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "artifacts/stress-v1/report.json"
_logger = logging.getLogger(__name__)


def code_hash() -> str:
    own = sorted(Path(__file__).parent.glob("*.py"))
    # Tournament loader independently checks its entire archived implementation.
    dependencies = [ROOT / "webapp/alpha_lab/db.py"]
    return hashlib.sha256(b"".join(p.read_bytes() for p in own + dependencies)).hexdigest()


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(REPORT_PATH.read_text())
    checksum = body.pop("report_hash")
    base = tournament_report.load()
    if (
        body.get("profile_hash") != settings().profile_hash
        or body.get("code_hash") != code_hash()
        or body.get("base_report_hash") != base["report_hash"]
        or digest(body) != checksum
        or body.get("source_hashes")
        != {"data/study_f/bars.csv": base["source_hashes"]["data/study_f/bars.csv"]}
    ):
        raise ValueError("stress artifact/profile/base/dependency mismatch")
    return body | {"report_hash": checksum}


def view() -> dict[str, Any] | None:
    try:
        return load()
    except Exception:
        _logger.exception("stress supplement unavailable; existing strategy report preserved")
        return None
