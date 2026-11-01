"""Independent reconciliation detects an internally rehashed but false ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from scripts.audit_vendor_history import audit
from scripts.diagnose_vendor_history import diagnose
from webapp.history_capture.capture import digest, read, seal

from tests.unit.test_vendor_diagnostic import archive


def fixture_source(tmp_path):
    archive(tmp_path)
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text())
    state.update(previous_runs=[], calls_this_run=0, resumed=0)
    state_path.write_text(json.dumps(state))
    for path in tmp_path.rglob("*.json.gz"):
        packet = read(path)
        packet.pop("packet_hash")
        packet["kind"] = "flow" if "flow" in path.name else "minute"
        packet["received_at"] = datetime(2026, 11, 2, tzinfo=UTC).isoformat()
        if packet["kind"] == "flow":
            for row in packet["payload"]["data"]:
                row["ticker"] = packet["ticker"]
        path.unlink()
        seal(path, packet)
    report = diagnose(tmp_path)
    report["report_hash"] = digest(report)
    original = tmp_path / "diagnostic.json"
    original.write_text(json.dumps(report, default=str))
    return original, report


def test_independent_audit_reconciles_fixed_references(tmp_path):
    original, _ = fixture_source(tmp_path)
    result = audit(tmp_path, original)
    assert result["status"] == "RECONCILED"
    assert result["verified_reference_units_all_costs"] == 12
    assert result["verified_packets"] == 63


@pytest.mark.parametrize("damage", ["pnl", "timestamp", "quantity", "source"])
def test_rehashing_does_not_hide_incorrect_arithmetic_or_source(tmp_path, damage):
    original, report = fixture_source(tmp_path)
    unit = report["rows"][0]["ledger"][0]
    if damage == "pnl":
        unit["net_usd"] += 100
    elif damage == "timestamp":
        unit["entry_at"] = unit["signal_at"]
    elif damage == "quantity":
        unit["quantity"] += 100
    else:
        path = next(tmp_path.rglob("*minute.json.gz"))
        packet = read(path)
        packet.pop("packet_hash")
        packet["payload"]["data"][0]["open"] += 1
        path.unlink()
        seal(path, packet)
    report.pop("report_hash")
    report["report_hash"] = digest(report)
    original.write_text(json.dumps(report, default=str))
    with pytest.raises(ValueError):
        audit(tmp_path, original)
