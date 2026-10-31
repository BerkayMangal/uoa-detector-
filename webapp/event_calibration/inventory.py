"""Count real local evidence without inspecting reserved forward prices."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from webapp.event_calibration.model import ROOT, identity


def inventory(root: Path = ROOT) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for relative in ("data/study_e/flow_daily.csv", "data/study_f/bars.csv"):
        path = root / relative
        if not path.exists():
            sources.append({"source": relative, "status": "missing"})
            continue
        body = path.read_bytes()
        rows = list(csv.DictReader(body.decode().splitlines()))
        dates = sorted({r.get("day", r.get("date", "")) for r in rows})
        sources.append(
            {
                "source": relative,
                "sha256": hashlib.sha256(body).hexdigest(),
                "rows": len(rows),
                "tickers": len({r["ticker"] for r in rows}),
                "sessions": len(dates),
                "start": dates[0] if dates else None,
                "end": dates[-1] if dates else None,
                "fields": list(rows[0]) if rows else [],
                "event_calibration_usable": False,
                "reason": "Daily aggregate; lacks first-known event times and executable intraday paths.",
            }
        )
    capability = root / "artifacts/options-alpha-v1/capability_matrix.json"
    old = json.loads(capability.read_text()) if capability.exists() else {}
    snapshots = tuple((root / "artifacts/options-alpha-v1/replay").glob("*/chain*.json"))
    return {
        "status": "EVENT_DATA_MISSING",
        "ready_for_calibration": False,
        "sources": sources,
        "option_snapshot_files": len(snapshots),
        "snapshot_days": sorted({p.parent.name for p in snapshots}),
        "old_vendor_probe_at": old.get("generated_at"),
        "old_probe_is_current_entitlement": False,
        "credential_present": bool(os.getenv("UNUSUAL_WHALES_API_KEY")),
        "reserved_forward_prices_read": False,
        "new_market_replays_executed": 0,
        "best_calibration": None,
        "reasons": [
            "No canonical observed event dataset is present in the registered local sources.",
            "Old capability measurements establish earlier reachability, not present access or complete history.",
            "No minute-level flow, as-of auxiliary feature, quote exit or fitted probability was fabricated.",
        ],
        **identity(),
    }
