"""Bound packet metadata explains UNKNOWN without changing the original decision."""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import event, update
from webapp import news_diagnostics as diagnostic
from webapp.news_research import storage as physical
from webapp.semantic_news import sources
from webapp.shadow_capture import bindings
from webapp.shadow_capture import storage as shadow

from tests.unit.test_news_adapter import NOW, scan, snapshot
from tests.unit.test_semantic_news_runtime import engine as engine
from tests.unit.test_semantic_news_runtime import seed


def empty_seed(engine, body=None, *, old_seconds=30):
    data = snapshot()
    data["news_observations"]["NVDA"].update(
        state="checked_none", checked_at=(NOW - timedelta(seconds=old_seconds)).isoformat()
    )
    scan(engine, data=data)
    physical.append(
        engine,
        "NVDA",
        body or {"data": []},
        NOW - timedelta(seconds=old_seconds),
        NOW - timedelta(seconds=old_seconds - 1),
        clock=lambda: NOW - timedelta(seconds=old_seconds - 2),
        application_checked_at=NOW - timedelta(seconds=old_seconds),
    )
    assert (
        bindings.discover(
            engine, NOW + timedelta(seconds=2), clock=lambda: NOW + timedelta(seconds=2)
        )["admitted"]
        == 1
    )
    return shadow.list_events(engine, "candidate", NOW + timedelta(seconds=3), day=NOW.date())[0]


def test_verified_empty_is_metadata_only_no_writes_or_policy_calls(engine, monkeypatch):
    parent = empty_seed(engine)
    before = shadow.get(engine, parent.key, NOW + timedelta(seconds=5))
    monkeypatch.setattr(sources, "source", lambda *_: pytest.fail("policy source path"))
    monkeypatch.setattr(sources, "empty_source", lambda *_: pytest.fail("empty source policy path"))
    writes = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
            else None
        ),
    )
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    row = report.rows[0]
    assert report.status == "AVAILABLE" and report.inference == "NOT_PERFORMED"
    assert row.source_verified and row.packet_verified
    assert row.status == "VERIFIED_METADATA" and row.original_news is False
    assert row.item_count == 0 and row.requested_limit == 20
    assert row.scan_finish.flag is False and row.candidate_publication.flag is False
    assert row.diagnostic_codes == ("checked_empty_metadata_consistent",)
    assert shadow.get(engine, parent.key, NOW + timedelta(seconds=10)) == before
    assert not writes


def test_full_page_of_stale_headlines_is_explicitly_unverified_empty(engine):
    body = {
        "data": [
            {
                "id": str(i),
                "headline": "Old issuer story " + str(i),
                "tickers": ["NVDA"],
                "source": "Wire",
                "created_at": (NOW - timedelta(days=3)).isoformat(),
            }
            for i in range(20)
        ]
    }
    parent = empty_seed(engine, body)
    assert sources.empty_source(engine, parent, NOW + timedelta(seconds=3)) is None
    row = diagnostic.build(engine, NOW + timedelta(seconds=10)).rows[0]
    assert row.original_news is False and row.scan_finish.flag is False
    assert row.item_count == 20 and row.status == "UNKNOWN_COVERAGE"
    assert row.diagnostic_codes == ("page_at_or_over_requested_limit",)


@pytest.mark.parametrize("flag", diagnostic._FLAGS)
def test_pagination_exports_only_presence_and_boolean_never_url_or_secret(engine, flag):
    secret = "https://provider.invalid/private?api_key=never-export-this"
    parent = empty_seed(engine, {"data": [], flag: secret})
    assert sources.empty_source(engine, parent, NOW + timedelta(seconds=3)) is None
    row = diagnostic.build(engine, NOW + timedelta(seconds=10)).rows[0]
    marker = next(x for x in row.pagination if x.name == flag)
    assert marker.present and marker.active
    assert "pagination_or_limited_marker" in row.diagnostic_codes
    output = json.dumps(asdict(row), default=str)
    assert secret not in output and "provider.invalid" not in output and "api_key" not in output


def test_source_age_can_change_between_original_scan_and_candidate_publication(engine):
    parent = empty_seed(engine, old_seconds=900)
    # At original scan the response is899s old, at publication it is901s old.
    row = diagnostic.build(engine, NOW + timedelta(seconds=10)).rows[0]
    assert row.source_verified and row.packet_verified
    assert row.scan_finish.flag is False
    assert row.candidate_publication.flag is None
    assert row.candidate_publication.reason == "physical_news_stale"
    assert "source_evidence_changed_between_clocks" in row.diagnostic_codes
    assert "publication_evidence_unknown" in row.diagnostic_codes
    assert sources.empty_source(engine, parent, NOW + timedelta(seconds=3)) is None


def test_nonempty_verified_packet_exposes_count_not_headlines_or_source_values(engine):
    seed(engine)
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    row = report.rows[0]
    assert row.original_news is True and row.item_count == 1
    assert row.packet_verified and row.scan_finish.flag is True
    output = json.dumps(asdict(report), default=str)
    assert "raises guidance" not in output and "IssuerWire" not in output
    assert "headline" not in output and "raw_body" not in output


def test_original_verifier_runs_before_bound_packet_inspection(engine, monkeypatch):
    seed(engine)
    monkeypatch.setattr(bindings, "valid_candidate", lambda *_: False)
    monkeypatch.setattr(
        diagnostic, "_payload", lambda *_: pytest.fail("unverified packet inspected")
    )
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    assert report.invalid_records == 1
    assert report.rows[0].reason == "original_candidate_unverified"
    assert report.rows[0].item_count is None and report.rows[0].packet_key is None


@pytest.mark.parametrize(
    "damage", ["hash", "gzip", "oversize", "future_clock", "missing_receipt", "wrong_ticker"]
)
def test_toctou_packet_damage_after_original_verification_stays_unknown(
    engine, monkeypatch, damage
):
    parent = seed(engine)[0]
    packet_key = parent.payload["news_packet_key"]
    original = bindings.valid_candidate
    calls = []

    def verify_then_damage(*args):
        valid = original(*args)
        assert valid
        calls.append("verified")
        with engine.begin() as connection:
            if damage == "missing_receipt":
                connection.execute(
                    update(physical.Receipt)
                    .where(physical.Receipt.key == packet_key)
                    .values(packet_hash="invalid")
                )
            elif damage == "future_clock":
                connection.execute(
                    update(physical.Receipt)
                    .where(physical.Receipt.key == packet_key)
                    .values(available_at=NOW + timedelta(hours=1))
                )
            else:
                changes = {"compressed_payload": b"not-gzip"}
                if damage == "hash":
                    changes = {"compressed_payload": gzip.compress(b'{"data":[]}')}
                elif damage == "oversize":
                    changes = {
                        "compressed_payload": gzip.compress(b"x" * (diagnostic._MAX_BYTES + 1))
                    }
                elif damage == "wrong_ticker":
                    changes = {"ticker": "OTHER"}
                connection.execute(
                    update(physical.Packet)
                    .where(physical.Packet.key == packet_key)
                    .values(**changes)
                )
        return valid

    monkeypatch.setattr(bindings, "valid_candidate", verify_then_damage)
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    assert calls == ["verified"]
    assert report.invalid_records == 1 and report.rows[0].status == "UNKNOWN"
    assert report.rows[0].packet_key is None and report.rows[0].item_count is None


@pytest.mark.parametrize(
    "raw", [b'{"data":[],"data":[]}', b'{"data":[],"next":NaN}', b"[]", b'{"data":null}', b"{"]
)
def test_payload_json_is_strict_and_never_reuses_untrusted_exception_text(raw):
    with pytest.raises((ValueError, TypeError)):
        diagnostic._payload(gzip.compress(raw), hashlib.sha256(raw).hexdigest())


def test_missing_storage_and_future_non_session_are_explicit(tmp_path):
    from webapp.board.db import make_engine

    e = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        assert diagnostic.build(e, NOW).reason == "source_storage_unavailable"
        assert (
            diagnostic.build(e, NOW, NOW.date() + timedelta(days=1)).reason
            == "future_session_not_observed"
        )
        assert (
            diagnostic.build(e, NOW, NOW.date() - timedelta(days=3)).reason
            == "not_a_verified_session"
        )
    finally:
        e.dispose()


def test_future_physical_packet_is_not_accepted_as_an_original_candidate(engine):
    parent = seed(engine)[0]
    with engine.begin() as connection:
        connection.execute(
            update(physical.Receipt)
            .where(physical.Receipt.key == parent.payload["news_packet_key"])
            .values(available_at=NOW + timedelta(hours=1))
        )
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    assert report.rows[0].reason == "original_candidate_unverified" and report.invalid_records == 1


def test_unknown_original_source_cannot_call_a_bad_receipt_verified_metadata(engine):
    data = snapshot()
    data["news_observations"]["NVDA"].update(state="checked_none")
    scan(engine, data=data)
    key = physical.append(
        engine,
        "NVDA",
        {"data": []},
        NOW - timedelta(seconds=30),
        NOW - timedelta(seconds=20),
        clock=lambda: NOW - timedelta(seconds=10),
        application_checked_at=NOW - timedelta(seconds=30),
    )
    with engine.begin() as c:
        c.execute(
            update(physical.Receipt)
            .where(physical.Receipt.key == key)
            .values(packet_hash="bad-receipt")
        )
    assert (
        bindings.discover(
            engine, NOW + timedelta(seconds=2), clock=lambda: NOW + timedelta(seconds=2)
        )["admitted"]
        == 1
    )
    parent = shadow.list_events(engine, "candidate", NOW + timedelta(seconds=3), day=NOW.date())[0]
    assert parent.payload["news"] is None
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    assert report.rows[0].source_verified
    assert report.rows[0].reason == "bound_physical_receipt_hash_mismatch"
    assert report.rows[0].packet_verified is False and report.rows[0].item_count is None


def test_deep_json_failure_inside_real_original_verifier_is_safe(engine):
    from sqlalchemy.orm import Session

    parent = seed(engine)[0]
    key = parent.payload["news_packet_key"]
    raw = b'{"data":' + b"[" * 2000 + b"]" * 2000 + b"}"
    checksum = hashlib.sha256(raw).hexdigest()
    with Session(engine) as session:
        p = session.get(physical.Packet, key)
        receipt_hash = physical.signature(
            p.key,
            p.ticker,
            p.requested_at,
            p.received_at,
            checksum,
            p.succeeded,
            p.application_checked_at,
        )
    with engine.begin() as c:
        c.execute(
            update(physical.Packet)
            .where(physical.Packet.key == key)
            .values(payload_hash=checksum, compressed_payload=gzip.compress(raw))
        )
        c.execute(
            update(physical.Receipt)
            .where(physical.Receipt.key == key)
            .values(packet_hash=receipt_hash)
        )
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    assert report.rows[0].reason in (
        "source_verification_unavailable",
        "original_candidate_unverified",
    )
    assert report.invalid_records == 1 and report.rows[0].item_count is None


@pytest.mark.parametrize("error_type", [RecursionError, ValueError])
def test_original_verifier_errors_are_fixed_safe_codes(engine, monkeypatch, error_type):
    seed(engine)
    private = "https://private.invalid/?token=never-export-this"

    def fail(*_args):
        raise error_type(private)

    monkeypatch.setattr(bindings, "valid_candidate", fail)
    report = diagnostic.build(engine, NOW + timedelta(seconds=10))
    assert report.rows[0].reason == "source_verification_unavailable"
    assert private not in json.dumps(asdict(report), default=str)
