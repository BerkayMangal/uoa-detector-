"""Evidence integrity and private, read-only summary delivery."""
from __future__ import annotations

import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from webapp import main
from webapp.history_capture.capture import digest
from webapp.horizon_research import report

from tests.unit._webapp_auth import set_web_auth


def artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "code_hash", lambda: "code")
    monkeypatch.setattr(report, "profile_hash", lambda: "profile")
    monkeypatch.setattr(report, "original_report", lambda: {"report_hash": "original", "source_packet_hashes": {"dated-minute": "packet"}})
    monkeypatch.setattr(report, "settings", lambda: SimpleNamespace(registered_cells=2))
    units = [{"key": "reference", "net_usd": None}]
    packed = gzip.compress(json.dumps(units).encode(), mtime=0)
    (tmp_path / "units.json.gz").write_bytes(packed)
    body = {"profile_hash": "profile", "code_hash": "code", "original_report_hash": "original",
            "source_packet_hashes": [["dated-minute", "packet"]],
            "cli_sha256": report.sha256(report.ROOT / "scripts/horizon_research.py"),
            "rows": [{}, {}], "winner": None, "validated_alpha": False,
            "ledger_file": "units.json.gz", "ledger_sha256": hashlib.sha256(packed).hexdigest(),
            "ledger_content_hash": digest(units)}
    body["report_hash"] = digest(body)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(body))
    return path, body


def test_read_checks_summary_and_compressed_ledger(tmp_path, monkeypatch):
    path, body = artifact(tmp_path, monkeypatch)
    checked = report.read(path, ledger=True, require_audit=False)
    assert checked["report_hash"] == body["report_hash"]
    assert checked["units"][0]["net_usd"] is None
    (tmp_path / "units.json.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="ledger checksum"):
        report.read(path, require_audit=False)


@pytest.mark.parametrize("field,value", [("code_hash", "changed"), ("profile_hash", "changed"),
                                          ("original_report_hash", "changed"), ("winner", "m5"),
                                          ("source_packet_hashes", [["later-daily", "packet"]]),
                                          ("validated_alpha", True), ("ledger_file", "../outside.gz")])
def test_rehashed_but_incompatible_artifact_refused(tmp_path, monkeypatch, field, value):
    path, body = artifact(tmp_path, monkeypatch)
    body.pop("report_hash")
    body[field] = value
    body["report_hash"] = digest(body)
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        report.read(path, require_audit=False)


def test_missing_independent_audit_is_not_verified(tmp_path, monkeypatch):
    path, _ = artifact(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError):
        report.read(path)


def test_horizon_summary_is_private_and_read_only(monkeypatch):
    headers = set_web_auth(monkeypatch)
    monkeypatch.setattr(report, "load", lambda: {"winner": None, "rows": [{"strict_net_usd": None}]})
    client = TestClient(main.app)
    assert client.get("/api/arastirma/ufuk").status_code == 401
    response = client.get("/api/arastirma/ufuk", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    assert response.json()["rows"][0]["strict_net_usd"] is None

    def broken():
        raise ValueError("evidence mismatch")

    monkeypatch.setattr(report, "load", broken)
    assert client.get("/api/arastirma/ufuk", headers=headers).status_code == 503


def test_published_frozen_results_reconcile_and_do_not_select_a_winner():
    body = report.read(report.DIRECTORY / "report.json")
    assert body["report_hash"] == "ed405e06de5069070f746a9675b015ae9f6dd2620ee0add3a65b1c8380e8182e"
    assert body["winner"] is None and body["validated_alpha"] is False
    assert body["assessed_cells"] == 1440
    assert (body["complete_cells"], body["partial_cells"], body["blocked_cells"]) == (368, 1072, 0)
    assert body["deduplicated_outcome_units"] == 30720
    assert body["audit"]["audited_original_controls"] == 12
    assert body["audit"]["source_replay"] == "ALL_MARKS_GATES_AND_EXCURSIONS"
    assert all(r["strict_net_usd"] < 0 for r in body["rows"] if r["status"] == "COMPLETE")
    assert all(r["strict_net_usd"] is None for r in body["rows"] if r["status"] != "COMPLETE")
    assert all("unit_keys" not in r for r in body["rows"])
    primary = body["primary_family"]
    assert primary["administrative_roster"] == 66 and len(primary["complete_common_days"]) == 36
    assert len(primary["right_censored_days_excluded"]) == 5
    assert len(primary["contrasts"]) == 10
    assert next(r for r in primary["contrasts"] if r["horizon"] == "m60")["total_net_usd"] == pytest.approx(-326.653896)


def test_invalid_original_dependency_blocks_cached_horizon_views(monkeypatch):
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/api/arastirma/ufuk", headers=headers).status_code == 200
    assert client.get("/api/arastirma/ufuk/dayaniklilik", headers=headers).status_code == 200

    def broken():
        raise ValueError("original source unavailable")

    monkeypatch.setattr(main.vendor_history_report, "load", broken)
    assert client.get("/api/arastirma/ufuk", headers=headers).status_code == 503
    assert client.get("/api/arastirma/ufuk/dayaniklilik", headers=headers).status_code == 503
