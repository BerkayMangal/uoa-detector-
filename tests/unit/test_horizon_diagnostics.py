"""Post-hoc evidence remains bound to its audited source and immutable worksheet."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from webapp import horizon_diagnostics, main
from webapp.history_capture.capture import digest

from tests.unit._webapp_auth import set_web_auth


def test_actual_concentration_worksheet_preserves_fragility():
    horizon_diagnostics.load.cache_clear()
    body = horizon_diagnostics.load()
    assert body["winner"] is None and not body["validated_alpha"]
    assert len(body["rows"]) == 40
    row = next(r for r in body["rows"] if r["horizon"] == "c5" and r["cost_bps_per_side"] == 5)
    assert row["reference_units"] == 66
    assert row["net_usd"] == pytest.approx(978.1063975)
    assert row["without_best_net_3_usd"] == pytest.approx(14.721115)
    assert row["without_best_excess_3_usd"] < 0
    assert row["worst_observed_gross_mae_pct"] < row["gross_mae_mean_pct"] < 0
    horizon_diagnostics.load.cache_clear()


@pytest.mark.parametrize("field,value", [("net_usd", 999999), ("winner", "c5"), ("report_hash", "fake")])
def test_changed_posthoc_evidence_refused(tmp_path, monkeypatch, field, value):
    body = json.loads(horizon_diagnostics.PATH.read_text())
    if field == "net_usd":
        body["rows"][0][field] = value
    else:
        body.pop("description_hash")
        body[field] = value
        body["description_hash"] = digest(body)
    path = tmp_path / "description.json"
    path.write_text(json.dumps(body))
    monkeypatch.setattr(horizon_diagnostics, "PATH", path)
    horizon_diagnostics.load.cache_clear()
    with pytest.raises(ValueError, match="mismatch"):
        horizon_diagnostics.load()
    horizon_diagnostics.load.cache_clear()


def test_diagnostics_api_private_and_fail_closed(monkeypatch):
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/api/arastirma/ufuk/dayaniklilik").status_code == 401
    response = client.get("/api/arastirma/ufuk/dayaniklilik", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]

    def broken():
        raise ValueError("mismatch")

    monkeypatch.setattr(horizon_diagnostics, "load", broken)
    assert client.get("/api/arastirma/ufuk/dayaniklilik", headers=headers).status_code == 503


def test_cached_diagnostics_cannot_bypass_a_failed_horizon_report(monkeypatch):
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/api/arastirma/ufuk/dayaniklilik", headers=headers).status_code == 200

    def broken():
        raise ValueError("horizon report unavailable")

    monkeypatch.setattr(main.horizon_report, "load", broken)
    assert client.get("/api/arastirma/ufuk/dayaniklilik", headers=headers).status_code == 503
