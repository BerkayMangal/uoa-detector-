"""Stored-data replay does not trust saved PnL or materialize an unbounded day."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select
from webapp.board.db import make_engine, session_factory
from webapp.theta_validation import automatic, service, storage

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
ENTRY = datetime(2026, 11, 11, 15, tzinfo=UTC)
DAY = ENTRY.date()
SYMBOL = "TSLA261127C00410000"


@pytest.fixture
def engine(tmp_path):
    value = make_engine(f"sqlite:///{tmp_path / 'automatic-replay.db'}")
    automatic.ensure(value)
    yield value
    value.dispose()


def _admit(engine, minute=0):
    at = ENTRY + timedelta(minutes=minute)
    payload = {
        "version": "theta_validation_v1", "option_symbol": SYMBOL,
        "planned_entry_at": at.isoformat(), "source_kind": "opening_entry",
        "source_id": f"synthetic-source-{minute}", "source_label": "Synthetic replay",
        "entry_basis": automatic.RETROSPECTIVE, "original_option_trade": False,
        "session_close_at": "2026-11-11T21:00:00+00:00",
    }
    job = storage.add_request(engine, payload, NOW)
    source = {
        "planned_entry_at": at.isoformat(), "entry_basis": automatic.RETROSPECTIVE,
        "ticker": "TSLA", "source_day": DAY.isoformat(), "source_kind": "opening_entry",
        "instrument": "long_call", "option_symbol": SYMBOL,
    }
    body = storage.canonical({"source": source, "request": payload})
    with session_factory(engine)() as session, session.begin():
        session.add(automatic.AutomaticAdmission(
            admission_id=f"admission-{minute}", source_primary_key=f"source-{minute}",
            source_day=DAY, observed_at=NOW, disposition="admitted", reason=None,
            request_id=job["request_id"], payload_json=body, payload_sha256=storage.digest(body),
        ))
    return job, at


def _packet(label, start, bid, ask):
    return {
        "label": label, "window_start": start.isoformat(),
        "window_end": (start + timedelta(seconds=120)).isoformat(),
        "received_at": NOW.isoformat(), "http_status": 200, "requested_interval": "tick",
        "payload": {"response": [{
            "contract": {"symbol": "TSLA", "expiration": "2026-11-27", "strike": 410, "right": "CALL"},
            "data": [{"timestamp": (start + timedelta(seconds=1)).isoformat(),
                      "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 11}],
        }]},
    }


def _receipt(engine, job, at, *, unknown=False, padding=""):
    packets = [_packet("entry", at, "7.15", "7.35")]
    if unknown:
        packets[0]["payload"] = {"response": []}
    else:
        packets.extend([
            _packet("m15", at + timedelta(minutes=15, seconds=1), "8.00", "8.10"),
            _packet("m60", at + timedelta(minutes=60, seconds=1), "6.50", "6.60"),
        ])
    if padding:
        packets[0]["payload"]["provider_extra_metadata"] = padding
    upload = service.UploadSpec.model_validate({"request_hash": job["request_hash"], "packets": packets})
    return service.ingest(engine, job["request_id"], upload, NOW)


def _group(report):
    return next(group for group in report["groups"] if group["entry_basis"] == automatic.RETROSPECTIVE)


def _raw_select(statement):
    return re.match(r"\s*SELECT\s+alfa_theta_validation_receipt\.payload_json\s+FROM\b", statement, re.I) is not None


def _record_sql(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


def test_same_input_replay_hits_cache_before_raw_load_or_evaluation(engine, monkeypatch):
    job, at = _admit(engine)
    _receipt(engine, job, at)
    first = automatic.rerun(engine, DAY, NOW)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unchanged replay must use its verified persisted report")

    monkeypatch.setattr(service, "evaluate_upload", forbidden)
    monkeypatch.setattr(automatic, "_raw_upload", forbidden)
    second = automatic.rerun(engine, DAY, NOW + timedelta(hours=1))
    assert second == first
    with session_factory(engine)() as session:
        assert session.scalar(select(func.count()).select_from(automatic.AutomaticReport)) == 1
        assert session.scalar(select(func.count()).select_from(storage.ThetaReceipt)) == 1


def test_replay_recomputes_native_quotes_and_ignores_saved_profit_and_status(engine):
    job, at = _admit(engine)
    receipt = _receipt(engine, job, at)
    forged = {"status": "UNKNOWN", "markouts": [
        {"horizon_minutes": minutes, "status": "EVALUATED", "modeled_net_pnl_usd": "99999999"}
        for minutes in (15, 60)
    ]}
    with session_factory(engine)() as session, session.begin():
        row = session.get(storage.ThetaReceipt, receipt["receipt_id"])
        row.result_json = storage.canonical(forged)
        row.status = "UNKNOWN"
    report = automatic.rerun(engine, DAY, NOW)
    group = _group(report)
    assert group["counts"]["evaluated"] == 1
    assert group["counts"]["unknown"] == 0
    assert [item["modeled_net_pnl_usd_total"] for item in group["horizons"]] == ["61.70", "-88.30"]
    assert [item["positive"] for item in group["horizons"]] == [1, 0]
    assert [item["negative"] for item in group["horizons"]] == [0, 1]
    assert report["execution_proven"] is False and report["winner"] is None
    with session_factory(engine)() as session:
        row = session.get(storage.ThetaReceipt, receipt["receipt_id"])
        assert json.loads(row.result_json) == forged
        assert session.scalar(select(func.count()).select_from(storage.ThetaReceipt)) == 1


def test_missing_native_quotes_remain_unknown_not_stored_profit_or_zero(engine):
    job, at = _admit(engine)
    receipt = _receipt(engine, job, at, unknown=True)
    with session_factory(engine)() as session, session.begin():
        row = session.get(storage.ThetaReceipt, receipt["receipt_id"])
        row.status = "EVALUATED"
        row.result_json = storage.canonical({"status": "EVALUATED", "modeled_net_pnl_usd": "5000"})
    group = _group(automatic.rerun(engine, DAY, NOW))
    assert group["counts"]["unknown"] == 1
    assert group["counts"]["evaluated"] == 0
    for mark in group["horizons"]:
        assert mark["unknown"] == 1 and mark["evaluated"] == 0
        assert mark["modeled_net_pnl_usd_total"] is None
        assert mark["zero"] == mark["positive"] == mark["negative"] == 0


def test_raw_export_preflights_utf8_bytes_before_selecting_receipt_payload(engine, monkeypatch):
    job, at = _admit(engine)
    receipt = _receipt(engine, job, at, padding="ü" * 4000)
    with session_factory(engine)() as session:
        body = session.get(storage.ThetaReceipt, receipt["receipt_id"]).payload_json
    cap = 8000
    assert len(body) < cap < len(body.encode("utf-8"))
    monkeypatch.setattr(automatic, "MAX_EXPORT_BYTES", cap)
    statements, listener = _record_sql(engine)
    try:
        with pytest.raises(ValueError, match=r"^cohort_export_too_large$"):
            automatic.cohort(engine, DAY, include_packets=True)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert any("sum(length(CAST(alfa_theta_validation_receipt.payload_json AS BLOB)))" in sql for sql in statements)
    assert not any(_raw_select(sql) for sql in statements)


def test_metadata_status_and_export_do_not_select_raw_receipts_or_saved_results(engine, monkeypatch):
    job, at = _admit(engine)
    _receipt(engine, job, at, padding="x" * 20000)
    monkeypatch.setattr(automatic, "MAX_EXPORT_BYTES", 8000)
    statements, listener = _record_sql(engine)
    try:
        status = automatic.status(engine, NOW, DAY)
        exported = automatic.cohort(engine, DAY, include_packets=False)
        rows = automatic._rows(engine, DAY, packets=False)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert status["counts"]["admitted"] == 1
    assert exported["raw_packets_included"] is False
    assert len(rows) == 1 and "upload" not in rows[0]["receipts"][0]
    receipt_selects = [sql for sql in statements if re.search(r"\bFROM\s+alfa_theta_validation_receipt\b", sql, re.I)]
    assert receipt_selects
    for sql in receipt_selects:
        projection = re.split(r"\bFROM\b", sql, maxsplit=1, flags=re.I)[0]
        assert "payload_json" not in projection and "result_json" not in projection


def test_replay_streams_individual_receipts_when_raw_day_exceeds_export_budget(engine, monkeypatch):
    sizes = []
    for minute in (0, 1):
        job, at = _admit(engine, minute)
        receipt = _receipt(engine, job, at, padding="x" * 12000)
        with session_factory(engine)() as session:
            sizes.append(len(session.get(storage.ThetaReceipt, receipt["receipt_id"]).payload_json.encode()))
    cap = 16000
    assert max(sizes) < cap < sum(sizes)
    monkeypatch.setattr(automatic, "MAX_EXPORT_BYTES", cap)
    assert len(storage.canonical(automatic.cohort(engine, DAY)).encode()) < cap
    with pytest.raises(ValueError, match=r"^cohort_export_too_large$"):
        automatic.cohort(engine, DAY, include_packets=True)
    sequence = []
    evaluate = service.evaluate_upload

    def record_read(_connection, _cursor, statement, _parameters, _context, _executemany):
        if _raw_select(statement):
            sequence.append("raw")

    def record_evaluation(*args, **kwargs):
        sequence.append("evaluate")
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(service, "evaluate_upload", record_evaluation)
    event.listen(engine, "before_cursor_execute", record_read)
    try:
        report = automatic.rerun(engine, DAY, NOW)
    finally:
        event.remove(engine, "before_cursor_execute", record_read)
    assert sequence == ["raw", "evaluate", "raw", "evaluate"]
    group = _group(report)
    assert group["counts"]["evaluated"] == 2
    assert [mark["modeled_net_pnl_usd_total"] for mark in group["horizons"]] == ["123.40", "-176.60"]
