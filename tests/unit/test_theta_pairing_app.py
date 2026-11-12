"""A downloaded connector has no owner credential or access outside its roster."""
from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from webapp import main, seo
from webapp.board.db import make_engine
from webapp.theta_validation import pairing, service, storage

from tests.unit._webapp_auth import set_web_auth

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


@pytest.fixture
def app(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'theta-pairing-app.db'}")
    storage.ensure(engine)
    pairing.ensure(engine)
    owner = set_web_auth(monkeypatch)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    yield TestClient(main.app), engine, owner
    engine.dispose()


def add_job(engine, minute=0):
    return service.create_request(engine, service.RequestSpec(
        option_symbol="TSLA261127C00410000",
        planned_entry_at=datetime(2026, 11, 11, 15, tzinfo=UTC) + timedelta(minutes=minute),
    ), NOW)


def test_download_handoff_requires_no_site_login_but_issuance_does(app):
    client, engine, owner = app
    first = add_job(engine)
    for path in ("/theta-bagla", "/assets/theta_pairing.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
        assert "www-authenticate" not in response.headers
    page = client.get("/theta-bagla")
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.post("/api/theta/pairings", json={}).status_code == 401
    assert client.post("/api/theta/pairings", json={}, headers=owner).status_code == 403
    issued = client.post("/api/theta/pairings", json={}, headers={**owner, "Origin": "http://testserver"})
    assert issued.status_code == 200
    token = issued.json()["pairing_token"]
    assert issued.json()["request_count"] == 1
    assert client.post("/api/theta/pairing/redeem", json={"pairing_token": token}).status_code == 403
    downloaded = client.post("/api/theta/pairing/redeem", json={"pairing_token": token},
                             headers={"Origin": "http://testserver"})
    assert downloaded.status_code == 200
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert re.fullmatch(r'attachment; filename="uoa-theta-hazir-[a-f0-9]{12}\.py"', downloaded.headers["content-disposition"])
    literals = {node.targets[0].id: ast.literal_eval(node.value)
                for node in ast.parse(downloaded.text).body
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in {"EMBEDDED_TOKEN", "EMBEDDED_UOA"}}
    assert literals["EMBEDDED_UOA"] == seo.ORIGIN
    assert literals["EMBEDDED_TOKEN"] != token
    assert pairing.authenticate(engine, literals["EMBEDDED_TOKEN"], NOW) == (first["request_id"],)
    assert client.post("/api/theta/pairing/redeem", json={"pairing_token": token},
                       headers={"Origin": "http://testserver"}).status_code == 409


def test_bearer_can_only_read_and_upload_frozen_requests(app):
    client, engine, owner = app
    first = add_job(engine)
    grant = pairing.issue(engine, NOW)
    connector = pairing.redeem(engine, grant.token, NOW)
    later = add_job(engine, 1)
    auth = {"Authorization": "Bearer " + connector.token, "Origin": "http://testserver"}
    jobs = client.get("/api/theta/jobs?limit=4", headers=auth)
    assert jobs.status_code == 200
    assert [j["request_id"] for j in jobs.json()["jobs"]] == [first["request_id"]]
    for path in ("/api/theta/status", "/api/theta/sources", "/api/theta/connector", "/opsiyon-dogrulama", "/"):
        assert client.get(path, headers=auth).status_code == 401
    for path in ("/api/theta/requests", "/api/theta/pairings", "/api/theta/requests/" + first["request_id"] + "/retry",
                 "/api/theta/results/" + later["request_id"], "/api/theta/results/" + first["request_id"] + "/extra"):
        assert client.post(path, headers=auth, json={}).status_code == 401
    upload = {"request_hash": first["request_hash"], "status": "terminal_unreachable", "packets": []}
    assert client.post("/api/theta/results/" + first["request_id"], headers={"Authorization": auth["Authorization"]}, json=upload).status_code == 403
    accepted = client.post("/api/theta/results/" + first["request_id"], headers=auth, json=upload)
    assert accepted.status_code == 200
    assert accepted.json()["result"]["status"] == "UNKNOWN"
    assert client.get("/api/theta/status", headers=owner).status_code == 200
    assert client.get("/api/theta/jobs", headers={"Authorization": "Bearer " + grant.token}).status_code == 401


def test_scope_filter_applies_before_limit(app):
    _, engine, _owner = app
    for minute in range(5):
        add_job(engine, minute)
    scoped = add_job(engine, 5)
    assert [row["request_id"] for row in service.pending_jobs(engine, NOW, 4, (scoped["request_id"],))["jobs"]] == [scoped["request_id"]]
    assert service.pending_jobs(engine, NOW, 4, ())["jobs"] == []


def test_expired_unknown_and_unavailable_auth_fail_closed(app, monkeypatch):
    client, engine, _ = app
    add_job(engine)
    grant = pairing.issue(engine, NOW)
    connector = pairing.redeem(engine, grant.token, NOW)
    monkeypatch.setattr(main, "_now", lambda: connector.expires_at)
    auth = {"Authorization": "Bearer " + connector.token}
    assert client.get("/api/theta/jobs", headers=auth).status_code == 401
    assert client.get("/api/theta/jobs", headers={"Authorization": "Bearer bad"}).status_code == 401
    def unavailable(*args):
        raise RuntimeError("private backend details")
    monkeypatch.setattr(pairing, "authenticate", unavailable)
    response = client.get("/api/theta/jobs", headers=auth)
    assert response.status_code == 503
    assert "private backend details" not in response.text


def test_issuance_without_mature_jobs_does_not_return_unusable_download(app):
    client, _, owner = app
    response = client.post("/api/theta/pairings", json={}, headers={**owner, "Origin": "http://testserver"})
    assert response.status_code == 409
    assert "pairing_token" not in response.json()
