"""The new product routes inherit authentication and never fetch on GET."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from webapp import main
from webapp.board.db import make_engine
from webapp.theta_validation import storage

from tests.unit._webapp_auth import set_web_auth


def test_theta_product_is_private_and_same_origin_requests_are_required(tmp_path, monkeypatch):
    headers = set_web_auth(monkeypatch)
    engine = make_engine(f"sqlite:///{tmp_path / 'theta.db'}")
    storage.ensure(engine)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    client = TestClient(main.app)
    for path in ("/opsiyon-dogrulama", "/api/theta/status", "/api/theta/jobs",
                 "/api/theta/sources", "/api/theta/connector"):
        assert client.get(path).status_code == 401
    page = client.get("/opsiyon-dogrulama", headers=headers)
    assert page.status_code == 200
    assert "ThetaData bağlantısı bekleniyor" in page.text
    assert client.get("/api/theta/status", headers=headers).json()["status"] == "awaiting_connection"
    body = {"option_symbol": "TSLA261127C00410000",
            "planned_entry_at": "2026-11-11T15:24:20+00:00"}
    assert client.post("/api/theta/requests", json=body, headers=headers).status_code == 403
    created = client.post("/api/theta/requests", json=body,
                          headers={**headers, "Origin": "http://testserver"})
    assert created.status_code == 200
    assert created.json()["option_symbol"] == body["option_symbol"]
    assert len(storage.pending(engine, datetime(2026, 11, 12, tzinfo=UTC), 4)) == 1
    engine.dispose()
