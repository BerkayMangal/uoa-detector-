"""Frozen real results must remain verifiable, private and read-only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine
from webapp.history_capture import report

from tests.unit._webapp_auth import set_web_auth


def test_real_fixed_results_and_cost_attribution_are_preserved():
    report.load.cache_clear()
    body = report.load()
    assert body["report_hash"] == "b3b411c1092f00df8f39eadb03aca4050cb4425ff10befd88b2b2dfb080d17af"
    assert (body["sessions"], body["evaluation_sessions"]) == (61, 41)
    assert body["winner"] is None and not body["validated_alpha"]
    assert not body["full_438_calibration_executed"]
    assert body["audit"]["verified_packets"] == 614
    assert body["audit"]["verified_reference_units_all_costs"] == 1024
    assert body["audit"]["resume_http_calls"] == 0
    for original, checked in zip(body["rows"], body["audit"]["rows"], strict=True):
        assert (original["arm"], original["bps_per_side"]) == (
            checked["arm"],
            checked["bps_per_side"],
        )
        assert original["net_unit_usd"] == pytest.approx(checked["net_unit_usd"])
        assert checked["gross_fixed_unit_usd"] - checked["modeled_price_cost_usd"] == pytest.approx(
            checked["net_unit_usd"]
        )
    assert all(r["net_unit_usd"] < 0 for r in body["rows"])


@pytest.mark.parametrize("filename", ["report.json", "audit.json"])
def test_modified_artifacts_are_rejected(tmp_path, monkeypatch, filename):
    for name in ("report.json", "audit.json"):
        (tmp_path / name).write_bytes((report.DIRECTORY / name).read_bytes())
    body = json.loads((tmp_path / filename).read_text())
    body["status"] = "FAKE_SUCCESS"
    (tmp_path / filename).write_text(json.dumps(body))
    report.load.cache_clear()
    monkeypatch.setattr(report, "DIRECTORY", tmp_path)
    with pytest.raises(ValueError, match="mismatch"):
        report.load()
    report.load.cache_clear()


def test_changed_calculation_code_is_not_presented_as_verified(monkeypatch):
    original = report.file_hash
    report.load.cache_clear()
    monkeypatch.setattr(
        report,
        "file_hash",
        lambda name: "changed" if name.endswith("diagnose_vendor_history.py") else original(name),
    )
    with pytest.raises(ValueError, match="mismatch"):
        report.load()
    report.load.cache_clear()


def test_research_page_shows_actual_negative_results_without_writes(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'read-only.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: datetime(2026, 11, 2, 8, tzinfo=UTC))
    mutations = []

    def executed(_c, _cur, sql, _params, _ctx, _many):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")):
            mutations.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    assert client.get("/arastirma").status_code == 401
    response = client.get("/arastirma", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    for text in ("Gerçek akış geçmişi", "-406.57", "-398.70", "-333.02", "438", "61 / 41"):
        assert text in response.text
    assert mutations == []

    def unavailable():
        raise ValueError("checksum")

    monkeypatch.setattr(report, "load", unavailable)
    response = client.get("/arastirma", headers=headers)
    assert response.status_code == 200
    assert "Tarihsel akış raporu doğrulanamadı" in response.text
    assert "-406.57" not in response.text
