"""Real opening publications, physical observer clocks and immutable admission."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from webapp.alpha_lab.db import digest
from webapp.board.db import make_engine, session_factory
from webapp.live_alpha.store import ScanRow, ensure_live_tables, write_scan
from webapp.opening import storage as opening
from webapp.opening.model import Advice, Plan, clock
from webapp.theta_validation import automatic, service, storage

AT = datetime(2026, 11, 12, 15, tzinfo=UTC)
SYMBOL = "TSLA261127C00410000"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("THETA_AUTOMATIC_ENABLED", "true")
    automatic.STATE.clear()
    value = make_engine(f"sqlite:///{tmp_path / 'automatic.db'}")
    ensure_live_tables(value)
    opening.ensure(value)
    automatic.ensure(value)
    yield value
    automatic.STATE.clear()
    value.dispose()


def publish(engine, *, at=AT, ticker="TSLA", symbol=SYMBOL, instrument="long_call",
            direction="up", legs=None, expiry=None, multiplier=100, scope="idea", scan_id="scan1"):
    if legs is None:
        legs = [{"option_symbol": symbol, "is_long": True}]
    card = {"ticker": ticker, "instrument": {"preferred": instrument},
            "options": [{"kind": instrument, "readiness": "READY", "multiplier": multiplier, "legs": legs}]}
    snapshot = {"policy_version": "frozen_live_policy", "profile_sha256": "a" * 64, "cards": [card]}
    scan = ScanRow(scan_id, at - timedelta(seconds=20), at - timedelta(seconds=5), "regular", "ok", None, snapshot)
    write_scan(engine, scan, "frozen_live_policy")
    contracts = tuple(leg["option_symbol"] for leg in legs)
    advice = Advice(ticker=ticker, action="BUY", scope=scope, instrument=instrument, direction=direction,
                    headline="Saved source", reasons=("saved",), plan=Plan(entry=1, stop=.5, target=2,
                    quantity=1, risk_usd=50, instrument=instrument, contracts=contracts),
                    source_card_hash=digest(card), source_policy="frozen_live_policy", source_profile_hash="a" * 64,
                    source_at=scan.finished_at, expires_at=expiry or at + timedelta(seconds=300))
    assert opening.publish(engine, scan, at, clock(at), (advice,))


def tick(engine, at):
    return automatic.run_once(engine, at, clock=lambda: at)


def cohort(engine):
    return automatic.cohort(engine, AT.date())["rows"]


def test_observer_commit_read_sets_prospective_clock_and_restart_preserves_job(engine):
    tick(engine, AT - timedelta(seconds=30))
    publish(engine)
    seen = AT + timedelta(seconds=25)
    tick(engine, seen)
    row = cohort(engine)[0]
    assert row["disposition"] == "admitted"
    assert row["source"]["entry_basis"] == automatic.PROSPECTIVE
    assert row["source"]["first_observed_committed_at"] == seen.isoformat()
    assert row["request"]["planned_entry_at"] == (seen + timedelta(seconds=60)).isoformat()
    assert row["request"]["source_expires_at"] == (AT + timedelta(seconds=300)).isoformat()
    automatic.STATE.clear()
    tick(engine, AT + timedelta(seconds=500))
    assert cohort(engine) == [row]
    assert automatic.status(engine, seen)["counts"]["pending_maturity"] == 1
    assert automatic.status(engine, AT + timedelta(hours=2))["counts"]["awaiting_connector"] == 1


def test_bounded_retrospective_publication_proxy_is_separate_and_old_sources_are_not_scanned(engine):
    publish(engine)
    publish(engine, at=AT - timedelta(days=8), scan_id="old")
    tick(engine, AT + timedelta(days=1))
    row = cohort(engine)[0]
    assert row["source"]["entry_basis"] == automatic.RETROSPECTIVE
    assert row["request"]["planned_entry_at"] == (AT + timedelta(seconds=60)).isoformat()
    assert row["source"]["historical_physical_availability_proven"] is False
    assert automatic.cohort(engine, (AT - timedelta(days=8)).date())["rows"] == []


@pytest.mark.parametrize("kwargs, reason", [
    ({"instrument": "stock"}, "unsupported_instrument"),
    ({"instrument": "bull_call_debit"}, "multiple_legs"),
    ({"legs": []}, "missing_contract"),
    ({"symbol": "bogus"}, "invalid_contract"),
    ({"direction": "down"}, "contract_direction_mismatch"),
    ({"ticker": "AAPL"}, "contract_direction_mismatch"),
    ({"multiplier": 10}, "nonstandard_multiplier"),
    ({"legs": [{"option_symbol": SYMBOL, "is_long": False}]}, "source_leg_not_single_long"),
    ({"scope": "position"}, "source_snapshot_mismatch"),
    ({"expiry": AT + timedelta(seconds=60)}, "expired_plan_before_target"),
])
def test_exclusions_are_counted_and_do_not_invent_contracts(engine, kwargs, reason):
    publish(engine, **kwargs)
    tick(engine, AT + timedelta(seconds=1))
    row = cohort(engine)[0]
    assert row["disposition"] == "excluded"
    assert row["reason"] == reason
    assert row["request"] is None
    view = automatic.status(engine, AT + timedelta(seconds=1))
    assert view["counts"]["observed"] == view["counts"]["excluded"] == 1
    assert view["counts"]["admitted"] == 0
    with session_factory(engine)() as session:
        assert session.scalars(select(storage.ThetaRequest)).all() == []


def test_first_stock_entry_is_never_replaced_by_later_option_buy(engine):
    publish(engine, instrument="stock")
    publish(engine, at=AT + timedelta(minutes=1), scan_id="later")
    tick(engine, AT + timedelta(minutes=2))
    rows = cohort(engine)
    assert len(rows) == 1
    assert rows[0]["reason"] == "unsupported_instrument"


def test_crash_after_admission_reconciles_reserved_immutable_job(engine, monkeypatch):
    publish(engine)
    original = storage.add_request
    monkeypatch.setattr(storage, "add_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        tick(engine, AT + timedelta(seconds=1))
    reserved = cohort(engine)[0]
    assert reserved["request"] is None
    monkeypatch.setattr(storage, "add_request", original)
    tick(engine, AT + timedelta(minutes=10))
    resumed = cohort(engine)[0]
    assert resumed["request_id"] == reserved["request_id"]
    assert resumed["source"] == reserved["source"]
    assert resumed["request"]["request_id"] == reserved["request_id"]


def test_bounded_batch_exposes_unobserved_sources_in_denominator(engine, monkeypatch):
    monkeypatch.setattr(automatic, "SOURCE_BATCH", 1)
    publish(engine, instrument="stock")
    publish(engine, ticker="AAPL", instrument="stock", scan_id="aapl")
    tick(engine, AT + timedelta(seconds=1))
    first = automatic.status(engine, AT + timedelta(seconds=1))
    assert first["counts"]["observed"] == 1
    assert first["source_context"]["unobserved"] == 1
    assert first["source_context"]["scan_complete"] is False
    tick(engine, AT + timedelta(seconds=2))
    final = automatic.status(engine, AT + timedelta(seconds=2))
    assert final["counts"]["observed"] == 2
    assert final["source_context"]["scan_complete"] is True


def test_status_waits_for_observer_and_identifies_stale_loop(engine):
    assert automatic.status(engine, AT)["status"] == "awaiting_observer"
    tick(engine, AT)
    assert automatic.status(engine, AT)["status"] == "ready"
    assert automatic.status(engine, AT + timedelta(seconds=181))["status"] == "stale"
    automatic.STATE.clear()
    assert automatic.status(engine, AT)["status"] == "awaiting_observer"


def test_native_entry_at_source_expiry_remains_unknown(engine):
    tick(engine, AT - timedelta(seconds=1))
    publish(engine, expiry=AT + timedelta(seconds=62))
    tick(engine, AT)
    job = cohort(engine)[0]["request"]
    entry = datetime.fromisoformat(job["planned_entry_at"])
    receipt_at = AT + timedelta(hours=2)
    packets = []
    for label, start in (("entry", entry), ("m15", entry + timedelta(seconds=2, minutes=15)),
                         ("m60", entry + timedelta(seconds=2, minutes=60))):
        quote_at = start + timedelta(seconds=2) if label == "entry" else start
        packets.append({"label": label, "window_start": start.isoformat(),
                        "window_end": (start + timedelta(seconds=120)).isoformat(),
                        "received_at": receipt_at.isoformat(), "http_status": 200, "requested_interval": "tick",
                        "payload": {"response": [{"contract": {"symbol": "TSLA", "expiration": "2026-11-27",
                        "strike": 410, "right": "CALL"}, "data": [{"timestamp": quote_at.isoformat(),
                        "bid": "7.15", "ask": "7.35", "bid_size": 10, "ask_size": 11}]}]}})
    result = service.ingest(engine, job["request_id"], service.UploadSpec.model_validate(
        {"request_hash": job["request_hash"], "packets": packets}), receipt_at)
    assert result["finalized"] is True
    assert result["result"]["status"] == "UNKNOWN"
    assert all(mark["modeled_net_pnl_usd"] is None for mark in result["result"]["markouts"])
    assert all(mark["reason"] == "source_plan_expired_before_entry_quote" for mark in result["result"]["markouts"])


def fail_three(engine, job, now):
    for seconds in (1, 2, 3):
        service.ingest(engine, job["request_id"], service.UploadSpec.model_validate({
            "request_hash": job["request_hash"], "status": "terminal_unreachable",
            "attempted_at": (now - timedelta(seconds=seconds)).isoformat(), "packets": []}), now)


def test_explicit_retry_follows_same_source_preserves_all_receipts_and_invalidates_report(engine):
    publish(engine)
    tick(engine, AT + timedelta(seconds=1))
    original = cohort(engine)[0]
    job = original["request"]
    now = AT + timedelta(hours=2)
    fail_three(engine, job, now)
    before = automatic.rerun(engine, AT.date(), now)
    before_cohort = automatic.cohort(engine, AT.date())
    assert automatic.status(engine, now)["recent"][0]["retry_available"] is True
    child = storage.retry_request(engine, job["request_id"], now)
    assert storage.retry_request(engine, job["request_id"], now) == child
    waiting = cohort(engine)[0]
    assert waiting["request_id"] == original["request_id"]
    assert waiting["active_request_id"] == child["request_id"]
    assert waiting["source"] == original["source"]
    assert waiting["request"]["planned_entry_at"] == job["planned_entry_at"]
    assert len(waiting["receipts"]) == 3
    assert automatic.cohort(engine, AT.date())["cohort_hash"] == before_cohort["cohort_hash"]
    assert automatic.cohort(engine, AT.date())["input_hash"] != before_cohort["input_hash"]
    after = automatic.rerun(engine, AT.date(), now)
    assert before["report_id"] != after["report_id"]
    assert after["groups"][1]["counts"]["retry_limit"] == 0
    assert after["groups"][1]["counts"]["awaiting_connector"] == 1
    assert after["groups"][1]["horizons"][0]["pending"] == 1
    target = datetime.fromisoformat(child["planned_entry_at"])
    result = service.ingest(engine, child["request_id"], service.UploadSpec.model_validate({
        "request_hash": child["request_hash"], "packets": [{"label": "entry", "window_start": target.isoformat(),
        "window_end": (target + timedelta(seconds=120)).isoformat(), "received_at": now.isoformat(),
        "http_status": 472, "requested_interval": "tick", "payload": None}]}), now)
    assert result["finalized"] is True
    final = automatic.cohort(engine, AT.date(), include_packets=True)["rows"][0]
    assert len(final["receipts"]) == 4
    assert len(final["request_lineage"]) == 2
    assert all("upload" in receipt for receipt in final["receipts"])
    report = automatic.rerun(engine, AT.date(), now)
    assert report["groups"][1]["counts"]["unknown"] == 1
    assert report["groups"][1]["horizons"][0]["modeled_net_pnl_usd_total"] is None
    assert automatic.status(engine, now)["recent"][0]["request_id"] == child["request_id"]
    with pytest.raises(ValueError, match="completed_result_is_immutable"):
        storage.retry_request(engine, child["request_id"], now)


def test_owner_retries_are_bounded_to_eight_and_parent_repeat_remains_idempotent(engine):
    publish(engine)
    tick(engine, AT + timedelta(seconds=1))
    original = cohort(engine)[0]["request"]
    job = original
    now = AT + timedelta(hours=2)
    for depth in range(9):
        fail_three(engine, job, now)
        if depth < 8:
            job = storage.retry_request(engine, job["request_id"], now)
    view = automatic.status(engine, now)["recent"][0]
    assert view["retry_depth"] == 8
    assert view["retry_available"] is False
    assert len(cohort(engine)[0]["receipts"]) == 27
    with pytest.raises(ValueError, match="automatic_retry_lineage_limit"):
        storage.retry_request(engine, job["request_id"], now)
    assert storage.retry_request(engine, original["request_id"], now)["retry_of"] == original["request_id"]


def test_unexhausted_or_changed_source_retry_fails_closed(engine):
    publish(engine)
    tick(engine, AT + timedelta(seconds=1))
    original = cohort(engine)[0]
    payload = original["request_lineage"][0]["payload"]
    storage.add_request(engine, {**payload, "retry_of": original["request_id"]}, AT + timedelta(hours=2))
    with pytest.raises(ValueError, match="invalid_retry_lineage"):
        cohort(engine)


@pytest.mark.parametrize("changed", ["planned_entry_at", "option_symbol"])
def test_exhausted_retry_cannot_change_contract_or_entry_target(engine, changed):
    publish(engine)
    tick(engine, AT + timedelta(seconds=1))
    original = cohort(engine)[0]
    fail_three(engine, original["request"], AT + timedelta(hours=2))
    payload = dict(original["request_lineage"][0]["payload"])
    payload["retry_of"] = original["request_id"]
    payload[changed] = (AT + timedelta(minutes=2)).isoformat() if changed == "planned_entry_at" else "TSLA261127C00420000"
    storage.add_request(engine, payload, AT + timedelta(hours=2))
    with pytest.raises(ValueError, match="retry_source_identity_mismatch"):
        cohort(engine)


def test_finalized_negative_measurement_cannot_be_retried(engine):
    publish(engine)
    tick(engine, AT + timedelta(seconds=1))
    job = cohort(engine)[0]["request"]
    entry = datetime.fromisoformat(job["planned_entry_at"])
    now = AT + timedelta(hours=2)
    packets = []
    for label, start in (("entry", entry), ("m15", entry + timedelta(seconds=1, minutes=15)),
                         ("m60", entry + timedelta(seconds=1, minutes=60))):
        packets.append({"label": label, "window_start": start.isoformat(),
                        "window_end": (start + timedelta(seconds=120)).isoformat(), "received_at": now.isoformat(),
                        "http_status": 200, "requested_interval": "tick", "payload": {"response": [{
                            "contract": {"symbol": "TSLA", "expiration": "2026-11-27", "strike": 410, "right": "CALL"},
                            "data": [{"timestamp": (start + timedelta(seconds=1)).isoformat(),
                                      "bid": "7.15" if label == "entry" else "6.00", "ask": "7.35",
                                      "bid_size": 10, "ask_size": 10}]}]}})
    result = service.ingest(engine, job["request_id"], service.UploadSpec.model_validate({
        "request_hash": job["request_hash"], "packets": packets}), now)
    assert result["result"]["status"] == "EVALUATED"
    assert all(float(mark["modeled_net_pnl_usd"]) < 0 for mark in result["result"]["markouts"])
    with pytest.raises(ValueError, match="completed_result_is_immutable"):
        storage.retry_request(engine, job["request_id"], now)
    assert automatic.status(engine, now)["recent"][0]["retry_available"] is False


def test_oversized_source_is_audited_without_selecting_its_raw_body(engine, monkeypatch):
    from sqlalchemy import event

    publish(engine)
    monkeypatch.setattr(automatic, "MAX_SOURCE_BYTES", 2)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    tick(engine, AT + timedelta(seconds=1))
    assert cohort(engine)[0]["reason"] == "source_payload_too_large"
    assert not any(statement.startswith("SELECT alfa_opening_decision.payload_json,") for statement in statements)
    selects = [statement.split("FROM")[0] for statement in statements if statement.startswith("SELECT")]
    assert not any("alfa_live_scan.snapshot_json," in statement for statement in selects)
