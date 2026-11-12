"""Automatic-cohort HTTP guards; no real app auth, provider calls or price data."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select
from webapp.board.db import make_engine, session_factory
from webapp.theta_validation import automatic, automatic_router, storage

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
DAY = date(2026, 11, 11)
ORIGIN = "https://uoa.example.test"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic, "STATE", {})
    monkeypatch.setenv("THETA_AUTOMATIC_ENABLED", "true")
    value = make_engine(f"sqlite:///{tmp_path / 'automatic-http.db'}")
    yield value
    value.dispose()


def client_for(engine, *, now=NOW):
    app = FastAPI()
    app.include_router(automatic_router.build_router(
        lambda: engine,
        lambda request: request.headers.get("origin") == ORIGIN,
        clock=lambda: now,
    ))
    return TestClient(app)


def test_unavailable_read_routes_do_not_create_storage_or_claim_observation(engine):
    client = client_for(engine)
    assert inspect(engine).get_table_names() == []
    status = client.get("/api/theta/automatic/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "storage_unavailable"
    assert payload["activation_at"] is None
    assert payload["counts"]["observed"] == 0
    assert payload["recent"] == []
    assert payload["latest_report"] is None
    cohort = client.get("/api/theta/automatic/cohort", params={"day": DAY.isoformat()})
    assert cohort.status_code == 503
    assert cohort.json() == {"detail": "storage_unavailable"}
    assert inspect(engine).get_table_names() == []


def test_empty_status_and_cohort_reads_execute_no_ddl_or_writes(engine, monkeypatch):
    automatic.ensure(engine)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", capture)
    monkeypatch.setattr(automatic, "ensure", lambda *_: pytest.fail("GET cannot create tables"))
    client = client_for(engine)
    try:
        status = client.get("/api/theta/automatic/status", params={"day": DAY.isoformat()})
        first = client.get("/api/theta/automatic/cohort", params={"day": DAY.isoformat()})
        second = client.get("/api/theta/automatic/cohort", params={"day": DAY.isoformat()})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert status.status_code == first.status_code == second.status_code == 200
    assert statements and set(statements) <= {"SELECT", "PRAGMA"}
    state = status.json()
    assert state["activation_at"] is None and state["catchup_start"] is None
    assert state["last_tick_at"] is None and state["last_tick_status"] is None
    assert state["counts"]["observed"] == state["counts"]["admitted"] == 0
    assert state["counts"]["evaluated"] == 0
    assert state["latest_report"] is None
    assert first.content == second.content
    assert first.headers["cache-control"] == "private, no-store"
    assert first.headers["content-disposition"] == 'attachment; filename="theta-cohort-2026-11-11.json"'
    assert first.headers["content-type"].startswith("application/json")
    cohort = first.json()
    assert cohort["rows"] == [] and cohort["truncated"] is False
    assert cohort["raw_packets_included"] is False
    assert cohort["limits"] == {"rows": automatic.MAX_COHORT_ROWS, "bytes": automatic.MAX_EXPORT_BYTES}
    with session_factory(engine)() as session:
        for model in (*automatic.TABLES, *storage.TABLES):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_last_failed_tick_is_not_hidden_by_empty_status_or_reclassified_as_success(engine, monkeypatch):
    automatic.ensure(engine)
    monkeypatch.setattr(automatic, "STATE", {"status": "source_unavailable", "at": NOW.isoformat()})
    response = client_for(engine).get("/api/theta/automatic/status", params={"day": DAY.isoformat()})
    assert response.status_code == 200
    payload = response.json()
    assert payload["last_tick_status"] == "source_unavailable"
    assert payload["last_tick_at"] == NOW.isoformat()
    assert payload["activation_at"] is None
    assert payload["counts"]["observed"] == payload["counts"]["evaluated"] == 0
    assert payload["latest_report"] is None


def test_disabled_and_default_new_york_day_are_preserved(engine, monkeypatch):
    automatic.ensure(engine)
    monkeypatch.setenv("THETA_AUTOMATIC_ENABLED", "false")
    early_utc = datetime(2026, 11, 12, 1, tzinfo=UTC)
    payload = client_for(engine, now=early_utc).get("/api/theta/automatic/status").json()
    assert payload["day"] == "2026-11-11"
    assert payload["status"] == "disabled"
    assert payload["activation_at"] is None


@pytest.mark.parametrize("include_packets", [False, True])
def test_cohort_forwards_only_validated_day_and_explicit_packet_flag(engine, monkeypatch, include_packets):
    calls = []

    def cohort(value, day, *, include_packets=False):
        calls.append((value, day, include_packets))
        return {"day": day.isoformat(), "raw_packets_included": include_packets, "rows": []}

    monkeypatch.setattr(automatic, "cohort", cohort)
    response = client_for(engine).get("/api/theta/automatic/cohort", params={
        "day": DAY.isoformat(), "include_packets": str(include_packets).lower(),
    })
    assert response.status_code == 200
    assert calls == [(engine, DAY, include_packets)]
    assert response.json()["raw_packets_included"] is include_packets


@pytest.mark.parametrize("path,params", [
    ("/status", {"day": "2026-02-30"}),
    ("/cohort", {}),
    ("/cohort", {"day": "not-a-date"}),
    ("/cohort", {"day": "2026-11-11", "include_packets": "maybe"}),
])
def test_invalid_query_dates_or_flags_do_not_reach_storage(engine, monkeypatch, path, params):
    monkeypatch.setattr(automatic, "status", lambda *_: pytest.fail("invalid query reached status"))
    monkeypatch.setattr(automatic, "cohort", lambda *_a, **_k: pytest.fail("invalid query reached cohort"))
    response = client_for(engine).get("/api/theta/automatic" + path, params=params)
    assert response.status_code == 422


@pytest.mark.parametrize("origin", [None, "https://other.example.test"])
def test_rerun_requires_same_origin_before_reading_body_or_calling_service(engine, monkeypatch, origin):
    async def unreadable(*_args):
        pytest.fail("cross-origin request body must not be read")

    monkeypatch.setattr(automatic_router, "_body", unreadable)
    monkeypatch.setattr(automatic, "rerun", lambda *_: pytest.fail("cross-origin rerun"))
    headers = {"Origin": origin} if origin else {}
    response = client_for(engine).post("/api/theta/automatic/reruns", json={"day": DAY.isoformat()}, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "same_origin_required"}


@pytest.mark.parametrize("body", [
    {}, [], {"day": None}, {"day": 20261111}, {"day": ["2026-11-11"]},
    {"day": "2026-11-11", "extra": True}, {"day": "2026-02-30"}, {"day": "tomorrow"},
])
def test_invalid_rerun_bodies_or_dates_never_execute(engine, monkeypatch, body):
    monkeypatch.setattr(automatic, "rerun", lambda *_: pytest.fail("invalid rerun executed"))
    response = client_for(engine).post("/api/theta/automatic/reruns", json=body, headers={"Origin": ORIGIN})
    assert response.status_code == 422
    assert inspect(engine).get_table_names() == []


def test_rerun_body_cap_precedes_json_shape_validation_and_cannot_be_bypassed_by_header(engine, monkeypatch):
    monkeypatch.setattr(automatic, "rerun", lambda *_: pytest.fail("oversized rerun executed"))
    body = b'{"day":"2026-11-11","padding":"' + b"x"*4096 + b'"}'
    response = client_for(engine).post("/api/theta/automatic/reruns", content=body, headers={
        "Origin": ORIGIN, "Content-Type": "application/json", "Content-Length": "1",
    })
    assert response.status_code == 413
    assert response.json() == {"detail": "theta_upload_too_large"}


@pytest.mark.parametrize("body", [b"{", b'{"day":NaN}', b'\xff'])
def test_malformed_nonfinite_or_non_utf8_rerun_json_is_rejected(engine, monkeypatch, body):
    monkeypatch.setattr(automatic, "rerun", lambda *_: pytest.fail("invalid JSON executed"))
    response = client_for(engine).post("/api/theta/automatic/reruns", content=body, headers={"Origin": ORIGIN})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_json"}


def test_valid_rerun_passes_exact_date_engine_and_injected_clock(engine, monkeypatch):
    calls = []

    def rerun(value, day, now):
        calls.append((value, day, now))
        return {"report_id": "fixed-report", "day": day.isoformat(), "stored_only": True}

    monkeypatch.setattr(automatic, "rerun", rerun)
    response = client_for(engine).post("/api/theta/automatic/reruns", json={"day": DAY.isoformat()}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert calls == [(engine, DAY, NOW)]
    assert response.json() == {"report_id": "fixed-report", "day": DAY.isoformat(), "stored_only": True}


@pytest.mark.parametrize("reason,code", [
    ("cohort_row_limit_exceeded", 413), ("cohort_export_too_large", 413),
    ("storage_unavailable", 503), ("receipt_integrity_error", 422),
])
@pytest.mark.parametrize("endpoint", ["status", "cohort", "reruns"])
def test_backend_bounds_and_fixed_failures_are_preserved(engine, monkeypatch, reason, code, endpoint):
    def fail(*_args, **_kwargs):
        raise ValueError(reason)

    monkeypatch.setattr(automatic, "rerun" if endpoint == "reruns" else endpoint, fail)
    client = client_for(engine)
    if endpoint == "reruns":
        response = client.post("/api/theta/automatic/reruns", json={"day": DAY.isoformat()}, headers={"Origin": ORIGIN})
    else:
        response = client.get("/api/theta/automatic/" + endpoint, params={"day": DAY.isoformat()})
    assert response.status_code == code
    assert response.json() == {"detail": reason}


def legacy_receipt(engine, label, result, *, received_at=NOW):
    request = storage.add_request(engine, {
        "option_symbol": "TSLA261127C00410000",
        "planned_entry_at": "2026-11-11T15:00:00+00:00",
        "source_kind": "manual_research", "source_label": label,
    }, NOW)
    return storage.append_receipt(
        engine, request_id=request["request_id"],
        upload={"native_payload_sentinel": "never-select-raw-body"*1000},
        result=result, status="UNKNOWN", finalized=False, now=received_at,
    )


def legacy_status_with_sql(engine):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        value = storage.view(engine, now=NOW+timedelta(seconds=5))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return value, statements


def result_body_queries(statements):
    return [sql for sql in statements
            if "alfa_theta_validation_receipt.result_json" in sql.partition(" from ")[0]
            and "length(" not in sql.partition(" from ")[0]]


def test_legacy_status_loads_small_result_after_byte_preflight_but_never_raw_upload(engine):
    storage.ensure(engine)
    result = {"status": "UNKNOWN", "markouts": [], "reason": "synthetic_no_quote"}
    saved = legacy_receipt(engine, "small", result)
    view, statements = legacy_status_with_sql(engine)
    assert view["recent_results"] == [saved]
    assert view["jobs"][0]["attempts"] == 1
    assert view["jobs"][0]["state"] == "pending"
    assert not any("alfa_theta_validation_receipt.payload_json" in sql for sql in statements)
    queries = result_body_queries(statements)
    assert len(queries) == 1
    length_query = next(i for i, sql in enumerate(statements)
                        if "length(" in sql and "alfa_theta_validation_receipt.result_json" in sql)
    assert length_query < statements.index(queries[0])


def test_legacy_status_omits_large_results_without_fetching_result_or_upload_body(engine, monkeypatch):
    storage.ensure(engine)
    saved = legacy_receipt(engine, "oversized", {
        "status": "UNKNOWN", "markouts": [], "large_detail": "synthetic"*1000,
    })
    monkeypatch.setattr(storage, "MAX_STATUS_RESULT_BYTES", 64)
    view, statements = legacy_status_with_sql(engine)
    recent = view["recent_results"][0]
    assert {key: value for key, value in recent.items() if key != "result"} == {
        key: value for key, value in saved.items() if key != "result"
    }
    assert recent["result"] == {
        "status": "UNKNOWN", "markouts": [], "result_details_omitted": True,
        "reason": "status_result_byte_limit",
    }
    assert result_body_queries(statements) == []
    assert not any("alfa_theta_validation_receipt.payload_json" in sql for sql in statements)
    assert any("length(" in sql and "alfa_theta_validation_receipt.result_json" in sql for sql in statements)


def test_legacy_status_byte_limit_is_utf8_bytes_not_character_count(engine, monkeypatch):
    storage.ensure(engine)
    result = {"status": "UNKNOWN", "detail": "ölçüm"*30}
    body = storage.canonical(result)
    assert len(body.encode()) > len(body)
    legacy_receipt(engine, "unicode", result)
    monkeypatch.setattr(storage, "MAX_STATUS_RESULT_BYTES", len(body))
    view, statements = legacy_status_with_sql(engine)
    assert view["recent_results"][0]["result"]["result_details_omitted"] is True
    assert result_body_queries(statements) == []


def test_legacy_status_budget_is_shared_across_results_not_restarted_per_receipt(engine, monkeypatch):
    storage.ensure(engine)
    result = {"status": "UNKNOWN", "detail": "bounded-small-result"}
    older = legacy_receipt(engine, "older", result)
    newer = legacy_receipt(engine, "newer", result, received_at=NOW+timedelta(seconds=1))
    monkeypatch.setattr(storage, "MAX_STATUS_RESULT_BYTES", len(storage.canonical(result).encode())+1)
    view, statements = legacy_status_with_sql(engine)
    assert view["recent_results"][0] == newer
    assert view["recent_results"][1]["receipt_id"] == older["receipt_id"]
    assert view["recent_results"][1]["result"]["result_details_omitted"] is True
    assert len(result_body_queries(statements)) == 1
    assert not any("alfa_theta_validation_receipt.payload_json" in sql for sql in statements)
