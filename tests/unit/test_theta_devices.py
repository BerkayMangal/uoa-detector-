"""Continuous enrollment, revocation, bounded health and native receipt proof."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, select
from webapp.board.db import make_engine, session_factory
from webapp.theta_validation import devices, pairing, service, storage

from tests.unit.test_theta_validation_backend import ENTRY, NOW, create, upload


@pytest.fixture
def engine(tmp_path):
    result = make_engine(f"sqlite:///{tmp_path / 'devices.db'}")
    storage.ensure(result)
    pairing.ensure(result)
    devices.ensure(result)
    yield result
    result.dispose()


def enroll(engine):
    grant = devices.issue(engine, NOW)
    return devices.redeem(engine, grant.token, NOW)


def healthy(**changes):
    return devices.HeartbeatSpec.model_validate({
        "connector_version": "5.70", "terminal_state": "reachable",
        "last_error": None, "outbox_count": 0, **changes,
    })


def test_enrollment_works_without_jobs_and_persists_only_token_hashes(engine):
    grant = devices.issue(engine, NOW)
    assert grant.expires_at == NOW + timedelta(hours=24)
    assert devices.authenticate(engine, grant.token, NOW) is None
    device = devices.redeem(engine, grant.token, NOW)
    assert device.token != grant.token
    assert devices.authenticate(engine, device.token, NOW) == device.device_id
    assert devices.authenticate(engine, device.token, NOW + timedelta(days=366)) == device.device_id
    assert devices.jobs(engine, device.device_id, NOW)["jobs"] == []
    with session_factory(engine)() as session:
        saved_grant = session.scalar(select(devices.ThetaDeviceGrant))
        saved = session.scalar(select(devices.ThetaDevice))
        assert saved_grant.token_sha256 == hashlib.sha256(grant.token.encode()).hexdigest()
        assert saved.token_sha256 == hashlib.sha256(device.token.encode()).hexdigest()
        values = [getattr(row, column.name) for row in [saved_grant, saved] for column in row.__table__.columns]
        assert grant.token not in values and device.token not in values
    status = devices.status(engine, NOW)
    assert status["status"] == "awaiting_connection"
    assert status["devices"][0]["last_heartbeat_at"] is None
    assert status["devices"][0]["last_validated_data_at"] is None


def test_grant_strict_expiry_one_use_and_future_clock(engine):
    expired = devices.issue(engine, NOW)
    with pytest.raises(ValueError, match="expired"):
        devices.redeem(engine, expired.token, expired.expires_at)
    with pytest.raises(ValueError, match="expired"):
        devices.redeem(engine, expired.token, NOW-timedelta(seconds=1))
    grant = devices.issue(engine, NOW)
    device = devices.redeem(engine, grant.token, NOW)
    with pytest.raises(ValueError, match="consumed"):
        devices.redeem(engine, grant.token, NOW)
    assert devices.authenticate(engine, device.token, NOW-timedelta(seconds=1)) is None
    with pytest.raises(ValueError, match="aware_device_clock"):
        devices.issue(engine, NOW.replace(tzinfo=None))


def test_concurrent_redeem_creates_exactly_one_device(engine):
    grant = devices.issue(engine, NOW)
    barrier = Barrier(6)

    def once(_):
        barrier.wait()
        try:
            return devices.redeem(engine, grant.token, NOW).device_id
        except ValueError as error:
            assert str(error) == "device_pairing_token_consumed"
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        issued = list(pool.map(once, range(6)))
    assert len([d for d in issued if d]) == 1
    with session_factory(engine)() as session:
        assert len(list(session.scalars(select(devices.ThetaDevice)))) == 1


def test_future_jobs_are_in_scope_but_legacy_tokens_are_isolated(engine):
    device = enroll(engine)
    legacy_grant = pairing.issue(engine, NOW)
    legacy = pairing.redeem(engine, legacy_grant.token, NOW)
    requests = [create(engine, ENTRY+timedelta(minutes=i)) for i in range(6)]
    assert len(devices.jobs(engine, device.device_id, NOW)["jobs"]) == 4
    assert devices.jobs(engine, device.device_id, NOW, 1)["jobs"][0]["request_id"] == min(r["request_id"] for r in requests)
    for limit in [0, 5]:
        with pytest.raises(ValueError, match="invalid_device_job_limit"):
            devices.jobs(engine, device.device_id, NOW, limit)
    assert pairing.authenticate(engine, legacy.token, NOW) == ()
    assert pairing.authenticate(engine, device.token, NOW) is None
    assert devices.authenticate(engine, legacy.token, NOW) is None


def test_revocation_is_durable_idempotent_and_rechecked_by_each_operation(engine):
    device = enroll(engine)
    job = create(engine)
    revoked = devices.revoke(engine, device.device_id, NOW)
    assert devices.revoke(engine, device.device_id, NOW+timedelta(minutes=1)) == revoked
    assert devices.authenticate(engine, device.token, NOW) is None
    for operation in [
        lambda: devices.jobs(engine, device.device_id, NOW),
        lambda: devices.heartbeat(engine, device.device_id, healthy(), NOW),
        lambda: devices.ingest(engine, device.device_id, job["request_id"], service.UploadSpec.model_validate(upload(job)), NOW),
    ]:
        with pytest.raises(ValueError, match="device_unauthorized"):
            operation()
    assert devices.status(engine, NOW)["devices"][0]["state"] == "revoked"
    with session_factory(engine)() as session:
        assert list(session.scalars(select(storage.ThetaReceipt))) == []


def test_heartbeat_is_bounded_health_not_quote_or_provider_entitlement(engine):
    device = enroll(engine)
    devices.heartbeat(engine, device.device_id, healthy(), NOW)
    state = devices.status(engine, NOW)
    assert state["status"] == "idle"
    item = state["devices"][0]
    assert item["bridge_online"] is True
    assert item["terminal_state"] == "reachable"
    assert item["terminal_health_source"] == "device_self_report"
    assert item["last_validated_data_at"] is None
    assert item["last_receipt_at"] is None
    assert state["source_identity_independently_verified"] is False
    assert devices.status(engine, NOW+timedelta(seconds=180))["devices"][0]["bridge_online"] is True
    assert devices.status(engine, NOW+timedelta(seconds=181))["status"] == "stale"
    create(engine)
    assert devices.status(engine, NOW)["status"] == "online"
    devices.heartbeat(engine, device.device_id, healthy(terminal_state="unreachable", last_error="terminal_unreachable"), NOW)
    assert devices.status(engine, NOW)["devices"][0]["terminal_state"] == "unreachable"


@pytest.mark.parametrize("changes", [
    {"outbox_count": -1}, {"outbox_count": 257}, {"outbox_count": True},
    {"last_error": "private secret provider stack"}, {"terminal_state": "licensed"},
    {"connector_version": "bad version\n"}, {"actual_data_received": True},
])
def test_heartbeat_rejects_raw_errors_arbitrary_fields_and_false_proof(changes):
    with pytest.raises(ValidationError):
        healthy(**changes)


def test_local_transport_auth_failures_never_consume_job_attempts(engine):
    device = enroll(engine)
    job = create(engine)
    for status in ["terminal_unreachable", "authentication_failed"]:
        spec = service.UploadSpec(request_hash=job["request_hash"], status=status)
        with pytest.raises(ValueError, match="report_terminal_health"):
            devices.ingest(engine, device.device_id, job["request_id"], spec, NOW)
    with session_factory(engine)() as session:
        assert list(session.scalars(select(storage.ThetaReceipt))) == []
    assert len(devices.jobs(engine, device.device_id, NOW)["jobs"]) == 1


def test_native_server_validation_sets_quote_proof_and_replay_does_not_refresh_it(engine):
    device = enroll(engine)
    job = create(engine)
    spec = service.UploadSpec.model_validate(upload(job))
    receipt = devices.ingest(engine, device.device_id, job["request_id"], spec, NOW)
    assert receipt["result"]["status"] == "EVALUATED"
    assert devices.ingest(engine, device.device_id, job["request_id"], spec, NOW+timedelta(minutes=2)) == receipt
    item = devices.status(engine, NOW+timedelta(minutes=2))["devices"][0]
    assert item["last_validated_data_at"] == NOW.isoformat()
    assert item["last_receipt_at"] == NOW.isoformat()
    assert item["last_upload_at"] == (NOW+timedelta(minutes=2)).isoformat()
    assert item["last_heartbeat_at"] is None
    assert item["state"] == "awaiting_connection"


def test_no_quotes_is_a_receipt_but_not_actual_validated_quote(engine):
    device = enroll(engine)
    job = create(engine)
    body = upload(job)
    body["packets"] = body["packets"][:1]
    body["packets"][0]["http_status"] = 472
    body["packets"][0]["payload"] = {}
    receipt = devices.ingest(engine, device.device_id, job["request_id"], service.UploadSpec.model_validate(body), NOW)
    assert receipt["finalized"] is True
    item = devices.status(engine, NOW)["devices"][0]
    assert item["last_receipt_at"] == NOW.isoformat()
    assert item["last_validated_data_at"] is None


@pytest.mark.parametrize("token", ["", "0"*63, "0"*65, "A"*64, "0"*64, "0"*64+"\n"])
def test_malformed_and_unknown_tokens_fail_closed(engine, token):
    assert devices.authenticate(engine, token, NOW) is None
    with pytest.raises(ValueError, match="invalid_device_pairing_token"):
        devices.redeem(engine, token, NOW)


def test_authorized_paths_are_exact_method_and_fullmatch_only():
    path = "/api/theta/device/results/theta_" + "a"*32
    for method, allowed in [("GET", "/api/theta/device/jobs"), ("POST", "/api/theta/device/heartbeat"), ("POST", path)]:
        assert devices.authorized_path(method, allowed)
        assert not devices.authorized_path("PUT", allowed)
        for suffix in ["/", "/extra", "\n", "?limit=4", "%2fextra"]:
            assert not devices.authorized_path(method, allowed+suffix)
    for path in ["/api/theta/jobs", "/api/theta/status", "/api/theta/devices/status", "/api/theta/device/redeem", "/api/theta/devices/pairings", "/api/theta/device/results/theta_"+"A"*32]:
        assert not devices.authorized_path("POST", path)
        assert not devices.authorized_path("GET", path)


def test_status_read_never_creates_tables_and_ensure_is_additive(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert devices.status(engine, NOW)["status"] == "storage_unavailable"
    assert inspect(engine).get_table_names() == []
    devices.ensure(engine)
    assert set(inspect(engine).get_table_names()) == {"alfa_theta_device", "alfa_theta_device_grant"}
    devices.ensure(engine)
    assert devices.status(engine, NOW)["status"] == "awaiting_enrollment"
    engine.dispose()
