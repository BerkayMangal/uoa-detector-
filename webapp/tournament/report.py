from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from webapp.alpha_lab.db import digest
from webapp.tournament.settings import settings

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "artifacts/tournament-v1/report.json"


def code_hash() -> str:
    own = sorted(Path(__file__).parent.glob("*.py"))
    dependencies = [
        ROOT / "webapp/calibration/model.py",
        ROOT / "webapp/calibration/settings.py",
        ROOT / "webapp/alpha_lab/flow.py",
    ]
    return hashlib.sha256(b"".join(p.read_bytes() for p in own + dependencies)).hexdigest()


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    # Immutable research artifact JSON adapter.
    body: dict[str, Any] = json.loads(REPORT_PATH.read_text())
    checksum = body.pop("report_hash")
    if (
        body.get("profile_hash") != settings().profile_hash
        or body.get("code_hash") != code_hash()
        or digest(body) != checksum
    ):
        raise ValueError("tournament artifact/profile/implementation mismatch")
    return body | {"report_hash": checksum}
