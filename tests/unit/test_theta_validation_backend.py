"""Connector packet integrity, immutable receipts, and server-priced economics."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from webapp.board.db import make_engine, session_factory
from webapp.theta_validation import service, storage
from webapp.theta_validation.router import build_router

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
ENTRY = datetime(2026, 11, 11, 15, tzinfo=UTC)
SYMBOL = "TSLA261127C00410000"


@pytest.fixture
def engine(tmp_path):
    value = make_engine(f"sqlite:///{tmp_path / 'theta.db'}")
    storage.ensure(value)
    yield value
    value.dispose()


def create(engine, at=ENTRY):
    return service.create_request(
        engine,
        service.RequestSpec(
            option_symbol=SYMBOL,
            planned_entry_at=at,
        ),
        NOW,
    )


def packet(label, start, *, bid="7.15", ask="7.35"):
    return {
        "label": label,
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(seconds=120)).isoformat(),
        "received_at": NOW.isoformat(),
        "http_status": 200,
        "requested_interval": "tick",
        "payload": {
            "response": [
                {
                    "contract": {
                        "symbol": "TSLA",
                        "expiration": "2026-11-27",
                        "strike": 410,
                        "right": "CALL",
                    },
                    "data": [
                        {
                            "timestamp": (start + timedelta(seconds=1)).isoformat(),
                            "bid": bid,
                            "ask": ask,
                            "bid_size": 10,
                            "ask_size": 11,
                        }
                    ],
                }
            ]
        },
    }


def upload(job):
    return {
        "request_hash": job["request_hash"],
        "packets": [
            packet("entry", ENTRY),
            packet("m15", ENTRY + timedelta(seconds=1, minutes=15), bid="8.00", ask="8.10"),
            packet("m60", ENTRY + timedelta(seconds=1, minutes=60), bid="6.50", ask="6.60"),
        ],
    }


def ingest(engine, job, data):
    return service.ingest(engine, job["request_id"], service.UploadSpec.model_validate(data), NOW)


def test_new_store_says_awaiting_connection_and_get_does_not_create_tables(tmp_path):
    empty = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert storage.view(empty, now=NOW)["status"] == "storage_unavailable"
    assert not storage.available(empty)
    storage.ensure(empty)
    assert storage.view(empty, now=NOW)["status"] == "awaiting_connection"
    empty.dispose()


def test_request_is_deterministic_and_only_mature_jobs_are_sent(engine):
    job = create(engine)
    assert create(engine) == job
    assert service.pending_jobs(engine, ENTRY + timedelta(minutes=63), 4)["jobs"] == []
    assert (
        service.pending_jobs(engine, ENTRY + timedelta(minutes=64), 4)["jobs"][0]["request_id"]
        == job["request_id"]
    )


def test_server_recomputes_actual_option_sides_costs_and_keeps_raw_receipt(engine):
    job = create(engine)
    data = upload(job)
    result = ingest(engine, job, data)
    assert result["finalized"] is True
    assert result["result"]["status"] == "EVALUATED"
    assert [x["modeled_net_pnl_usd"] for x in result["result"]["markouts"]] == ["61.70", "-88.30"]
    assert result["result"]["executable_fill_proven"] is False
    assert result["result"]["source_identity_independently_verified"] is False
    assert service.pending_jobs(engine, NOW, 4)["jobs"] == []
    with session_factory(engine)() as session:
        row = session.scalar(select(storage.ThetaReceipt))
        assert row is not None
        assert storage.digest(row.payload_json) == row.upload_sha256
        assert "CALL" in row.payload_json


def test_same_upload_is_idempotent_and_changed_finalized_result_is_refused(engine):
    job = create(engine)
    data = upload(job)
    result = ingest(engine, job, data)
    assert ingest(engine, job, data) == result
    changed = deepcopy(data)
    changed["packets"][2]["payload"]["response"][0]["data"][0]["bid"] = "6.51"
    with pytest.raises(ValueError, match="completed_result_is_immutable"):
        ingest(engine, job, changed)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("hash", "request_hash_mismatch"),
        ("window", "packet_query_window_mismatch"),
        ("future", "packet_receipt_time_invalid"),
        ("contract", "packet_contract_or_request_invalid"),
        ("missing_exit", "both_exit_packets_required"),
        ("duplicate_label", "unique_entry_packet_required"),
    ],
)
def test_invalid_upload_is_rejected_before_receipt(engine, mutation, reason):
    job = create(engine)
    data = upload(job)
    if mutation == "hash":
        data["request_hash"] = "0" * 64
    if mutation == "window":
        data["packets"][0]["window_start"] = (ENTRY - timedelta(seconds=1)).isoformat()
    if mutation == "future":
        data["packets"][0]["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    if mutation == "contract":
        data["packets"][0]["payload"]["response"][0]["contract"]["symbol"] = "AAPL"
    if mutation == "missing_exit":
        data["packets"].pop()
    if mutation == "duplicate_label":
        data["packets"][1]["label"] = "entry"
    with pytest.raises(ValueError, match=reason):
        ingest(engine, job, data)
    assert storage.view(engine, now=NOW)["recent_results"] == []


def test_missing_quotes_remain_unknown_and_not_zero(engine):
    job = create(engine)
    empty = packet("entry", ENTRY)
    empty["payload"] = {"response": []}
    result = ingest(engine, job, {"request_hash": job["request_hash"], "packets": [empty]})
    assert result["result"]["status"] == "UNKNOWN"
    assert all(x["modeled_net_pnl_usd"] is None for x in result["result"]["markouts"])
    assert result["finalized"] is True


def test_provider_472_keeps_status_and_raw_body_without_fabricated_quotes(engine):
    job = create(engine)
    empty = packet("entry", ENTRY)
    empty["http_status"] = 472
    empty["payload"] = "No data found for this request"
    result = ingest(engine, job, {"request_hash": job["request_hash"], "packets": [empty]})
    assert result["result"]["status"] == "UNKNOWN"
    assert result["result"]["provider_packets"][0]["http_status"] == 472
    assert result["result"]["entry_quote"] is None


def test_three_distinct_failures_stop_queue_and_network_retry_does_not_consume_attempt(engine):
    job = create(engine)
    data = {"request_hash": job["request_hash"], "status": "terminal_unreachable", "packets": []}
    first = ingest(engine, job, data)
    assert ingest(engine, job, data)["attempt"] == first["attempt"] == 1
    for seconds in (1, 2):
        data["attempted_at"] = (NOW - timedelta(seconds=seconds)).isoformat()
        result = ingest(engine, job, data)
    assert result["attempt"] == 3
    assert service.pending_jobs(engine, NOW, 4)["jobs"] == []
    data["attempted_at"] = (NOW - timedelta(seconds=3)).isoformat()
    with pytest.raises(ValueError, match="attempt_limit_reached"):
        ingest(engine, job, data)


def test_manual_request_cannot_claim_a_saved_source(engine):
    with pytest.raises(ValueError, match="manual_source_cannot_claim_saved_identity"):
        service.create_request(
            engine,
            service.RequestSpec(
                option_symbol=SYMBOL, planned_entry_at=ENTRY, source_id="not-a-real-source"
            ),
            NOW,
        )


def test_http_enforces_origin_json_shape_and_does_not_accept_client_pnl(engine):
    app = FastAPI()
    app.include_router(
        build_router(
            lambda: engine,
            lambda request: request.headers.get("origin") == "http://testserver",
            lambda: NOW,
        )
    )
    with TestClient(app) as client:
        body = {"option_symbol": SYMBOL, "planned_entry_at": ENTRY.isoformat()}
        assert client.post("/api/theta/requests", json=body).status_code == 403
        response = client.post(
            "/api/theta/requests", json=body, headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 200
        job = response.json()
        data = upload(job)
        data["pnl_usd"] = 100000
        assert (
            client.post(
                "/api/theta/results/" + job["request_id"],
                json=data,
                headers={"Origin": "http://testserver"},
            ).status_code
            == 422
        )
        assert client.get("/api/theta/jobs?limit=5").status_code == 422
        assert client.get("/api/theta/status").json()["status"] == "awaiting_connection"


def test_http_payload_size_and_nonfinite_values_are_rejected(engine):
    app = FastAPI()
    app.include_router(build_router(lambda: engine, lambda request: True, lambda: NOW))
    with TestClient(app) as client:
        assert client.post("/api/theta/requests", content=b" " * 4097).status_code == 413
        assert (
            client.post("/api/theta/requests", content=b'{"option_symbol":NaN}').status_code == 422
        )


def test_near_close_omits_out_of_session_exits_and_keeps_unknown(engine):
    at = datetime(2026, 11, 11, 20, 50, tzinfo=UTC)
    job = create(engine, at=at)
    result = ingest(
        engine, job, {"request_hash": job["request_hash"], "packets": [packet("entry", at)]}
    )
    assert result["result"]["status"] == "PARTIAL"
    assert all(
        x["reason"] == "exit_target_outside_regular_session" for x in result["result"]["markouts"]
    )
    assert job["session_close_at"] == "2026-11-11T21:00:00+00:00"


def test_owner_can_explicitly_retry_exhausted_job_without_rewriting_receipts(engine):
    job = create(engine)
    for seconds in (1, 2, 3):
        ingest(
            engine,
            job,
            {
                "request_hash": job["request_hash"],
                "status": "terminal_unreachable",
                "attempted_at": (NOW - timedelta(seconds=seconds)).isoformat(),
            },
        )
    retry = storage.retry_request(engine, job["request_id"], NOW)
    assert retry["request_id"] != job["request_id"]
    assert retry["retry_of"] == job["request_id"]
    assert storage.retry_request(engine, job["request_id"], NOW) == retry
    assert [row["request_id"] for row in storage.pending(engine, NOW, 4)] == [retry["request_id"]]
    assert len(storage.view(engine, now=NOW)["recent_results"]) == 3


def test_saved_uoa_decision_supplies_its_own_contract_and_reference_time(engine):
    from webapp.board.cards import AlfaDecisionCard, ensure_card_tables

    ensure_card_tables(engine)
    with session_factory(engine)() as session, session.begin():
        session.add(
            AlfaDecisionCard(
                id="saved-card",
                created_at=ENTRY - timedelta(seconds=60),
                decision="log",
                ticker="TSLA",
                direction="yukarı",
                run_id="saved-run",
                dominant_option_symbol=SYMBOL,
                card_json="{}",
                board_version="test",
                board_profile_hash="a" * 64,
            )
        )
    source = service.sources(engine, NOW)["sources"][0]
    assert source["source_id"] == "saved-card"
    assert source["planned_entry_at"] == ENTRY.isoformat()
    job = service.create_request(
        engine, service.RequestSpec(source_kind="decision_card", source_id="saved-card"), NOW
    )
    assert job["option_symbol"] == SYMBOL
    assert job["source_kind"] == "decision_card"
    with pytest.raises(ValueError, match="source_contract_mismatch"):
        service.create_request(
            engine,
            service.RequestSpec(
                source_kind="decision_card",
                source_id="saved-card",
                option_symbol="AAPL261127C00200000",
            ),
            NOW,
        )


def test_pending_queue_does_not_lose_old_jobs_when_recent_view_is_bounded(engine):
    expected = None
    for minutes in range(40):
        job = create(engine, at=ENTRY + timedelta(minutes=minutes))
        if minutes == 0:
            expected = job["request_id"]
    assert len(storage.view(engine, now=NOW)["jobs"]) == 32
    assert expected in [job["request_id"] for job in storage.pending(engine, NOW, 40)]


def test_http_upload_requires_explicit_transport_status(engine):
    app = FastAPI()
    app.include_router(build_router(lambda: engine, lambda request: True, lambda: NOW))
    job = create(engine)
    data = upload(job)
    del data["packets"][0]["http_status"]
    with TestClient(app) as client:
        response = client.post("/api/theta/results/" + job["request_id"], json=data)
    assert response.status_code == 422
    assert storage.view(engine, now=NOW)["recent_results"] == []
