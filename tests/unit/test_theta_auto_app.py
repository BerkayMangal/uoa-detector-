"""Exercise continuous-device capabilities through the real application gate."""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from webapp import main, seo
from webapp.board.db import make_engine, session_factory
from webapp.theta_validation import device_router, devices, pairing, service, storage

from tests.unit._webapp_auth import TEST_PASSWORD, TEST_USER, set_web_auth

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
ORIGIN = {"Origin": "http://testserver"}
HEARTBEAT = {
    "connector_version": "1.0.0",
    "terminal_state": "reachable",
    "last_error": None,
    "outbox_count": 0,
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'theta-auto-app.db'}")
    storage.ensure(engine)
    pairing.ensure(engine)
    devices.ensure(engine)
    owner = set_web_auth(monkeypatch)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    yield TestClient(main.app), engine, owner
    engine.dispose()


def _job(engine, minute=0):
    return service.create_request(engine, service.RequestSpec(
        option_symbol="TSLA261127C00410000",
        planned_entry_at=datetime(2026, 11, 11, 15, tzinfo=UTC) + timedelta(minutes=minute),
    ), NOW)


def _enroll(client, owner):
    issued = client.post("/api/theta/devices/pairings", json={}, headers={**owner, **ORIGIN})
    assert issued.status_code == 200
    grant = issued.json()["pairing_token"]
    downloaded = client.post("/api/theta/device/redeem", json={"pairing_token": grant}, headers=ORIGIN)
    assert downloaded.status_code == 200
    literals = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(downloaded.text).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"EMBEDDED_TOKEN", "EMBEDDED_UOA"}
    }
    assert literals["EMBEDDED_UOA"] == seo.ORIGIN
    assert re.fullmatch(r"[a-f0-9]{64}", literals["EMBEDDED_TOKEN"])
    assert literals["EMBEDDED_TOKEN"] != grant
    assert TEST_PASSWORD not in downloaded.text and TEST_USER not in downloaded.text
    return literals["EMBEDDED_TOKEN"], grant, downloaded


def _auth(token):
    return {"Authorization": "Bearer " + token, **ORIGIN}


def _upload(job):
    return {"request_hash": job["request_hash"], "attempted_at": NOW.isoformat(),
            "status": "request_failed", "packets": []}


def _receipt_count(engine):
    with session_factory(engine)() as session:
        return session.scalar(select(func.count()).select_from(storage.ThetaReceipt))


def test_public_setup_is_private_to_caches_and_issuance_stays_owner_only(app):
    client, _engine, owner = app
    for path in ("/theta-otomatik", "/assets/theta_pairing.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
        assert "www-authenticate" not in response.headers
    page = client.get("/theta-otomatik")
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.post("/api/theta/devices/pairings", json={}).status_code == 401
    assert client.post("/api/theta/devices/pairings", json={}, headers=owner).status_code == 403
    token, grant, downloaded = _enroll(client, owner)
    assert re.fullmatch(r'attachment; filename="uoa-theta-otomatik-[a-f0-9]{12}\.py"',
                        downloaded.headers["content-disposition"])
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-robots-tag"] == "noindex, nofollow, nosnippet"
    assert client.post("/api/theta/device/redeem", json={"pairing_token": grant}).status_code == 403
    assert client.post("/api/theta/device/redeem", json={"pairing_token": grant},
                       headers=ORIGIN).status_code == 409
    assert client.get("/api/theta/device/jobs", headers=_auth(grant)).status_code == 401
    assert client.get("/api/theta/device/jobs", headers=_auth(token)).status_code == 200


def test_empty_queue_enrollment_receives_future_jobs_beyond_legacy_expiry(app, monkeypatch):
    client, engine, owner = app
    token, _grant, _downloaded = _enroll(client, owner)
    auth = _auth(token)
    assert client.get("/api/theta/device/jobs", headers=auth).json()["jobs"] == []
    later = _job(engine)
    jobs = client.get("/api/theta/device/jobs?limit=1", headers=auth)
    assert jobs.status_code == 200
    assert [row["request_id"] for row in jobs.json()["jobs"]] == [later["request_id"]]
    monkeypatch.setattr(main, "_now", lambda: NOW + timedelta(days=8))
    assert client.get("/api/theta/device/jobs", headers=auth).status_code == 200
    heartbeat = client.post("/api/theta/device/heartbeat", json=HEARTBEAT, headers=auth)
    assert heartbeat.status_code == 200


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/api/theta/status"),
    ("GET", "/api/theta/sources"),
    ("GET", "/api/theta/connector"),
    ("GET", "/api/theta/devices/status"),
    ("GET", "/api/theta/automatic/status"),
    ("GET", "/api/theta/automatic/cohort?day=2026-11-11&include_packets=true"),
    ("GET", "/opsiyon-dogrulama"),
    ("GET", "/"),
    ("POST", "/api/theta/requests"),
    ("POST", "/api/theta/pairings"),
    ("POST", "/api/theta/devices/pairings"),
    ("POST", "/api/theta/automatic/reruns"),
    ("POST", "/api/theta/requests/theta_" + "a" * 32 + "/retry"),
    ("POST", "/api/theta/devices/thetadev_" + "a" * 32 + "/revoke"),
])
def test_device_cannot_enter_owner_routes(app, method, path):
    client, _engine, owner = app
    token, _grant, _downloaded = _enroll(client, owner)
    response = client.request(method, path, headers=_auth(token), json={}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/theta-otomatik"),
    ("GET", "/theta-otomatik/extra"),
    ("GET", "/api/theta/device/redeem"),
    ("POST", "/api/theta/device/redeem/extra"),
    ("POST", "/api/theta/device/jobs"),
    ("GET", "/api/theta/device/jobs/"),
    ("GET", "/api/theta/device/jobsevil"),
    ("GET", "/api/theta/device/heartbeat"),
    ("POST", "/api/theta/device/heartbeat/extra"),
    ("GET", "/api/theta/device/results/theta_" + "a" * 32),
    ("POST", "/api/theta/device/results/theta_" + "a" * 31),
    ("POST", "/api/theta/device/results/theta_" + "A" * 32),
    ("POST", "/api/theta/device/results/theta_" + "a" * 32 + "/extra"),
    ("POST", "/api/theta/device/results/theta_" + "a" * 32 + "%2Fextra"),
])
def test_capabilities_require_exact_method_and_path(app, method, path):
    client, _engine, owner = app
    token, _grant, _downloaded = _enroll(client, owner)
    response = client.request(method, path, headers=_auth(token), json={}, follow_redirects=False)
    assert response.status_code == 401


def test_legacy_and_continuous_credentials_cannot_cross_namespaces(app):
    client, engine, owner = app
    first = _job(engine)
    legacy_grant = pairing.issue(engine, NOW)
    legacy = pairing.redeem(engine, legacy_grant.token, NOW)
    token, _grant, _downloaded = _enroll(client, owner)
    later = _job(engine, 1)
    legacy_jobs = client.get("/api/theta/jobs", headers=_auth(legacy.token))
    assert legacy_jobs.status_code == 200
    assert [row["request_id"] for row in legacy_jobs.json()["jobs"]] == [first["request_id"]]
    device_jobs = client.get("/api/theta/device/jobs", headers=_auth(token))
    assert {row["request_id"] for row in device_jobs.json()["jobs"]} == {
        first["request_id"], later["request_id"],
    }
    for method, path in (
        ("GET", "/api/theta/device/jobs"),
        ("POST", "/api/theta/device/heartbeat"),
        ("POST", "/api/theta/device/results/" + first["request_id"]),
    ):
        assert client.request(method, path, headers=_auth(legacy.token), json={}).status_code == 401
    assert client.get("/api/theta/jobs", headers=_auth(token)).status_code == 401
    assert client.post("/api/theta/results/" + first["request_id"],
                       headers=_auth(token), json=_upload(first)).status_code == 401
    assert client.get("/api/theta/devices/status", headers=owner).status_code == 200


def test_owner_revocation_immediately_blocks_jobs_heartbeat_and_upload(app):
    client, engine, owner = app
    token, _grant, _downloaded = _enroll(client, owner)
    job = _job(engine)
    auth = _auth(token)
    device_id = devices.authenticate(engine, token, NOW)
    assert device_id is not None
    path = "/api/theta/device/results/" + job["request_id"]
    bare_auth = {"Authorization": auth["Authorization"]}
    assert client.post("/api/theta/device/heartbeat", json=HEARTBEAT, headers=bare_auth).status_code == 403
    assert client.post(path, json=_upload(job), headers=bare_auth).status_code == 403
    assert _receipt_count(engine) == 0
    assert client.post("/api/theta/device/heartbeat", json=HEARTBEAT, headers=auth).status_code == 200
    uploaded = client.post(path, json=_upload(job), headers=auth)
    assert uploaded.status_code == 200
    assert uploaded.json()["result"]["status"] == "UNKNOWN"
    assert _receipt_count(engine) == 1
    revoke = "/api/theta/devices/" + device_id + "/revoke"
    assert client.post(revoke, json={}, headers=auth).status_code == 401
    assert client.post(revoke, json={}, headers=owner).status_code == 403
    assert client.get("/api/theta/device/jobs", headers=auth).status_code == 200
    assert client.post(revoke, json={}, headers={**owner, **ORIGIN}).status_code == 200
    assert client.get("/api/theta/device/jobs", headers=auth).status_code == 401
    assert client.post("/api/theta/device/heartbeat", json=HEARTBEAT, headers=auth).status_code == 401
    assert client.post(path, json=_upload(job), headers=auth).status_code == 401
    assert _receipt_count(engine) == 1
    assert client.get("/api/theta/devices/status", headers=owner).status_code == 200


@pytest.mark.parametrize("operation", ["heartbeat", "upload"])
def test_revocation_while_request_body_is_read_cannot_write(app, monkeypatch, operation):
    client, engine, owner = app
    token, _grant, _downloaded = _enroll(client, owner)
    job = _job(engine)
    device_id = devices.authenticate(engine, token, NOW)
    assert device_id is not None
    original_body = device_router._body

    async def revoke_after_body(request, maximum):
        body = await original_body(request, maximum)
        await asyncio.to_thread(devices.revoke, engine, device_id, NOW)
        return body

    monkeypatch.setattr(device_router, "_body", revoke_after_body)
    path = "/api/theta/device/heartbeat" if operation == "heartbeat" else (
        "/api/theta/device/results/" + job["request_id"]
    )
    body = HEARTBEAT if operation == "heartbeat" else _upload(job)
    response = client.post(path, json=body, headers=_auth(token))
    assert response.status_code == 401
    assert _receipt_count(engine) == 0
    with session_factory(engine)() as session:
        record = session.get(devices.ThetaDevice, device_id)
        assert record is not None and record.revoked_at is not None
        assert record.last_heartbeat_at is None and record.last_upload_at is None


def test_device_auth_is_offloaded_and_backend_failures_are_sanitized(app, monkeypatch):
    client, _engine, owner = app
    token, _grant, _downloaded = _enroll(client, owner)
    original = devices.authenticate
    calls = []

    def checked_authenticate(*args):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(devices, "authenticate", checked_authenticate)
    assert client.get("/api/theta/device/jobs", headers=_auth(token)).status_code == 200
    assert calls

    def unavailable(*args):
        raise RuntimeError("private-database-password-and-host")

    monkeypatch.setattr(devices, "authenticate", unavailable)
    response = client.get("/api/theta/device/jobs", headers=_auth(token))
    assert response.status_code == 503
    assert "private-database-password-and-host" not in response.text
    assert token not in response.text
    assert response.headers["cache-control"] == "private, no-store"
