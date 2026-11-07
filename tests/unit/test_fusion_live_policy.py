"""Fixed forward arm decisions require timely, issuer-bound observed evidence."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from webapp.board.db import make_engine
from webapp.fusion_live import adapters, archive, config, policy, scoring, service, storage
from webapp.fusion_live.model import FundamentalEvidence, NewsEvidence
from webapp.live_alpha import store as live
from webapp.news_research import storage as physical
from webapp.semantic_news.view import Row as SemanticRow
from webapp.shadow_capture import bindings
from webapp.shadow_capture import storage as shadow

from tests.unit.test_news_adapter import NOW, news, scan

AT = datetime(2026, 11, 9, 15, tzinfo=UTC)
CUTOFF = AT + timedelta(seconds=55)
CANDIDATE = "candidate-one"
TICKER = "NVDA"


def semantic_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_key": CANDIDATE,
        "ticker": TICKER,
        "status": "completed",
        "direction": "positive",
        "supports": True,
        "available_at": AT - timedelta(seconds=1),
        "eligible_before_entry": True,
        "checked_empty": False,
        "input_count": 1,
        "source_hash": "a" * 64,
        "model": "grok-test",
        "prompt_hash": "b" * 64,
        "schema_hash": "c" * 64,
        "reason": "Source-grounded interpretation, not a return forecast.",
        "items": [
            {"id": "headline-one", "ticker": TICKER, "relation": "direct", "certainty": "confirmed"}
        ],
    }
    row.update(changes)
    return row


def fundamental_row(revenue: object = 3.0, margin: object = 8.0, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_key": CANDIDATE,
        "ticker": TICKER,
        "status": "recorded",
        "observations": [
            {
                "endpoint": "financials",
                "status": "recorded",
                "available_at": AT - timedelta(seconds=2),
                "body_sha256": "d" * 64,
                "facts": {
                    "ticker": TICKER,
                    "endpoint": "financials",
                    "available_at": AT - timedelta(seconds=2),
                    "metrics": [
                        {
                            "name": "revenue_yoy_pct",
                            "value": revenue,
                            "unit": "percent",
                            "source_paths": [
                                "income_statements.current",
                                "income_statements.prior",
                            ],
                        },
                        {
                            "name": "operating_margin_pct",
                            "value": margin,
                            "unit": "percent",
                            "source_paths": ["income_statements.current"],
                        },
                    ],
                },
            }
        ],
    }
    row.update(changes)
    return row


def semantic_evidence(row: object) -> NewsEvidence:
    return adapters.semantic(row, candidate_key=CANDIDATE, ticker=TICKER, now=AT)


def fundamental_evidence(row: object) -> FundamentalEvidence:
    return adapters.fundamental(row, candidate_key=CANDIDATE, ticker=TICKER, now=AT)


@pytest.mark.parametrize(
    ("case", "positive", "veto"),
    [
        ("positive", "PAPER_ACCEPT", "PAPER_ACCEPT"),
        ("negative", "PAPER_SKIP", "PAPER_SKIP"),
        ("mixed", "PAPER_SKIP", "PAPER_SKIP"),
        ("neutral", "PAPER_SKIP", "PAPER_ACCEPT"),
        ("empty", "PAPER_SKIP", "PAPER_ACCEPT"),
        ("rumor", "PAPER_UNKNOWN", "PAPER_UNKNOWN"),
        ("expected", "PAPER_UNKNOWN", "PAPER_UNKNOWN"),
        ("denied", "PAPER_UNKNOWN", "PAPER_UNKNOWN"),
        ("indirect", "PAPER_UNKNOWN", "PAPER_UNKNOWN"),
        ("unknown", "PAPER_UNKNOWN", "PAPER_UNKNOWN"),
    ],
)
def test_registered_news_truth_table_uses_meaning_and_uncertainty(
    case: str,
    positive: str,
    veto: str,
) -> None:
    row = semantic_row()
    if case in ("negative", "mixed", "neutral", "unknown"):
        row.update(direction=case, supports=False if case != "unknown" else None)
    elif case == "empty":
        row.update(checked_empty=True, input_count=0, items=[], direction="unknown", supports=None)
    elif case in ("rumor", "expected", "denied"):
        row["items"][0]["certainty"] = case
    elif case == "indirect":
        row["items"][0]["relation"] = "indirect"
    evidence = semantic_evidence(row)
    quality = fundamental_evidence(fundamental_row())
    assert policy.evaluate("base", AT, CUTOFF, evidence, quality).state == "PAPER_ACCEPT"
    assert policy.evaluate("semantic_positive", AT, CUTOFF, evidence, quality).state == positive
    assert policy.evaluate("semantic_veto", AT, CUTOFF, evidence, quality).state == veto
    assert policy.evaluate("semantic_fundamental", AT, CUTOFF, evidence, quality).state == positive


def test_missing_pending_work_is_not_frozen_as_an_early_unknown() -> None:
    pending = policy.evaluate(
        "semantic_fundamental", AT, CUTOFF, NewsEvidence(), FundamentalEvidence()
    )
    assert pending.state == "PENDING" and pending.used_at == ()
    ready_news = semantic_evidence(semantic_row())
    ready_fund = fundamental_evidence(fundamental_row())
    assert (
        policy.evaluate("semantic_fundamental", AT, CUTOFF, ready_news, FundamentalEvidence()).state
        == "PENDING"
    )
    assert (
        policy.evaluate("semantic_fundamental", AT, CUTOFF, ready_news, ready_fund).state
        == "PAPER_ACCEPT"
    )


def test_actual_checked_empty_public_row_uses_original_packet_provenance_without_model() -> None:
    row = SemanticRow(
        candidate_key=CANDIDATE,
        ticker=TICKER,
        status="skipped",
        direction="neutral",
        supports=False,
        available_at=AT - timedelta(seconds=1),
        eligible_before_entry=True,
        source_packet_key="d" * 64,
        source_payload_hash="e" * 64,
        checked_empty=True,
    )
    assert row.source_hash is None and row.model is None and row.input_count == 0
    evidence = semantic_evidence(asdict(row))
    assert (
        evidence.status == "known" and evidence.positive is False and evidence.veto_accept is True
    )
    assert evidence.source_hash == row.source_payload_hash
    fundamental = fundamental_evidence(fundamental_row())
    assert (
        policy.evaluate("semantic_veto", AT, CUTOFF, evidence, fundamental).state == "PAPER_ACCEPT"
    )
    assert (
        policy.evaluate("semantic_positive", AT, CUTOFF, evidence, fundamental).state
        == "PAPER_SKIP"
    )


@pytest.mark.parametrize(
    "damage", ["payload_hash", "packet_key", "nonempty", "not_checked_empty", "failed_status"]
)
def test_empty_packet_fallback_cannot_replace_unverified_model_or_source_provenance(
    damage: str,
) -> None:
    row = asdict(
        SemanticRow(
            candidate_key=CANDIDATE,
            ticker=TICKER,
            status="skipped",
            direction="neutral",
            available_at=AT - timedelta(seconds=1),
            eligible_before_entry=True,
            source_packet_key="d" * 64,
            source_payload_hash="e" * 64,
            checked_empty=True,
        )
    )
    if damage == "payload_hash":
        row["source_payload_hash"] = "invalid"
    elif damage == "packet_key":
        row["source_packet_key"] = None
    elif damage == "nonempty":
        row["input_count"] = 1
    elif damage == "failed_status":
        row["status"] = "failed"
    else:
        row["checked_empty"] = False
    evidence = semantic_evidence(row)
    assert evidence.status == "unknown" and evidence.positive is evidence.veto_accept is None


@pytest.mark.parametrize(
    "arm", ["base", "semantic_positive", "semantic_veto", "semantic_fundamental"]
)
def test_cutoff_and_later_decisions_cannot_be_scored_as_entered(arm: str) -> None:
    n, f = semantic_evidence(semantic_row()), fundamental_evidence(fundamental_row())
    assert (
        policy.evaluate(arm, CUTOFF - timedelta(microseconds=1), CUTOFF, n, f).state
        == "PAPER_ACCEPT"
    )
    for now in (CUTOFF, CUTOFF + timedelta(seconds=1)):
        result = policy.evaluate(arm, now, CUTOFF, n, f)
        assert result.state == "PAPER_UNKNOWN" and result.used_at == ()


@pytest.mark.parametrize("status", ["unknown", "failed", "pending"])
def test_status_cannot_be_overridden_by_an_inconsistent_positive_scalar(status: str) -> None:
    n = NewsEvidence(status=status, positive=True, veto_accept=True, available_at=AT)
    f = FundamentalEvidence(status=status, quality=True, available_at=AT)
    for arm in ("semantic_positive", "semantic_veto", "semantic_fundamental"):
        assert policy.evaluate(arm, AT, CUTOFF, n, f).state != "PAPER_ACCEPT"


def test_known_failed_conjunct_is_skip_even_when_other_component_is_unknown() -> None:
    known_bad_news = semantic_evidence(semantic_row(direction="negative", supports=False))
    known_bad_fund = fundamental_evidence(fundamental_row(revenue=-1, margin=None))
    assert (
        policy.evaluate(
            "semantic_fundamental", AT, CUTOFF, known_bad_news, FundamentalEvidence()
        ).state
        == "PAPER_SKIP"
    )
    result = policy.evaluate("semantic_fundamental", AT, CUTOFF, NewsEvidence(), known_bad_fund)
    assert result.state == "PAPER_SKIP" and result.used_at == (AT - timedelta(seconds=2),)


def test_future_or_late_evidence_never_supplies_an_accept_or_a_known_skip() -> None:
    n = replace(semantic_evidence(semantic_row()), available_at=AT + timedelta(seconds=1))
    f = replace(
        fundamental_evidence(fundamental_row(-1)), available_at=CUTOFF + timedelta(seconds=1)
    )
    assert policy.evaluate("semantic_positive", AT, CUTOFF, n, f).state == "PAPER_UNKNOWN"
    assert policy.evaluate("semantic_fundamental", AT, CUTOFF, n, f).state == "PAPER_UNKNOWN"


@pytest.mark.parametrize(
    "damage", ["candidate", "ticker", "nested_ticker", "source_hash", "late", "future"]
)
def test_semantic_adapter_rejects_wrong_source_or_candidate_binding(damage: str) -> None:
    row = semantic_row()
    if damage == "candidate":
        row["candidate_key"] = "other-candidate"
    elif damage == "ticker":
        row["ticker"] = "AAPL"
    elif damage == "nested_ticker":
        row["items"][0]["ticker"] = "AAPL"
    elif damage == "source_hash":
        row["source_hash"] = "not-a-source-hash"
    elif damage == "late":
        row["eligible_before_entry"] = False
    else:
        row["available_at"] = AT + timedelta(seconds=1)
    result = semantic_evidence(row)
    assert result.status == "unknown" and result.positive is result.veto_accept is None


@pytest.mark.parametrize(
    ("revenue", "margin", "quality"),
    [
        (0.0, 1.0, True),
        (1.0, 0.0, False),
        (-1.0, None, False),
        (None, 0.0, False),
        (None, 1.0, None),
        (1.0, None, None),
        (None, None, None),
        (False, 1.0, None),
        ("1", 1.0, None),
        (float("nan"), 1.0, None),
        (float("inf"), 1.0, None),
    ],
)
def test_financial_zero_boundaries_known_failures_and_unknown_values_are_distinct(
    revenue: object,
    margin: object,
    quality: bool | None,
) -> None:
    evidence = fundamental_evidence(fundamental_row(revenue, margin))
    assert evidence.quality is quality
    assert evidence.status == ("known" if quality is not None else "unknown")


@pytest.mark.parametrize(
    "damage",
    ["candidate", "ticker", "failed", "nested_ticker", "endpoint", "clock", "hash", "unit", "path"],
)
def test_financial_adapter_rejects_unverified_observation_and_metric_provenance(
    damage: str,
) -> None:
    row = fundamental_row()
    source = row["observations"][0]
    if damage == "candidate":
        row["candidate_key"] = "other"
    elif damage == "ticker":
        row["ticker"] = "AAPL"
    elif damage == "failed":
        source["status"] = "failed"
    elif damage == "nested_ticker":
        source["facts"]["ticker"] = "AAPL"
    elif damage == "endpoint":
        source["facts"]["endpoint"] = "earnings"
    elif damage == "clock":
        source["facts"]["available_at"] = AT + timedelta(seconds=1)
    elif damage == "hash":
        source["body_sha256"] = "invalid"
    elif damage == "unit":
        source["facts"]["metrics"][0]["unit"] = "USD"
    else:
        source["facts"]["metrics"][0]["source_paths"] = []
    evidence = fundamental_evidence(row)
    assert evidence.status == "unknown" and evidence.quality is None


@pytest.fixture
def source_engine(tmp_path: Any) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'fusion-source.db'}")
    live.ensure_live_tables(engine)
    physical.ensure(engine)
    shadow.ensure(engine)
    shadow.activate(engine, lambda: NOW - timedelta(hours=1))
    scan(engine)
    news(engine)
    bindings.discover(engine, NOW + timedelta(seconds=2), clock=lambda: NOW + timedelta(seconds=2))
    yield engine
    engine.dispose()


def test_source_archive_preserves_known_false_and_unknown_without_zero_filling(
    source_engine: Engine,
) -> None:
    at = NOW + timedelta(seconds=10)
    candidate = shadow.list_events(source_engine, "candidate", at)[0]
    raw = archive.snapshot(source_engine, candidate, at)
    evidence_at = NOW + timedelta(seconds=5)
    n = NewsEvidence(
        status="known",
        positive=False,
        veto_accept=False,
        available_at=evidence_at,
        source_hash="a" * 64,
    )
    f = FundamentalEvidence(
        status="known",
        quality=False,
        available_at=evidence_at,
        revenue_yoy_pct=-1.0,
        operating_margin_pct=None,
        source_hashes=("b" * 64,),
    )
    feature = archive.features(candidate, raw, NOW + timedelta(seconds=3), at, n, f)
    values = {m.name: m for m in feature.news}
    assert values["positive"].value is False and values["positive"].known_at == evidence_at
    financials = {m.name: m for m in feature.fundamental}
    assert financials["revenue_yoy_pct"].value == -1.0
    assert (
        financials["operating_margin_pct"].value is None
        and financials["operating_margin_pct"].missing
    )
    market = {m.name: m for m in feature.market}
    assert (
        market["dark_pool_confirmation"].value is None and market["level2_orderbook"].value is None
    )
    assert feature.usable(at, (("news", "positive"),))[0] is True
    assert feature.usable(at, (("fundamental", "operating_margin_pct"),))[0] is False


def test_source_archive_rejects_changed_original_scan_hash(source_engine: Engine) -> None:
    at = NOW + timedelta(seconds=10)
    candidate = shadow.list_events(source_engine, "candidate", at)[0]
    forged = replace(candidate, payload={**candidate.payload, "snapshot_hash": "0" * 64})
    with pytest.raises(ValueError, match="fusion_source_changed"):
        archive.snapshot(source_engine, forged, at)


def test_feature_archive_does_not_backdate_a_future_semantic_or_financial_receipt(
    source_engine: Engine,
) -> None:
    at = NOW + timedelta(seconds=10)
    candidate = shadow.list_events(source_engine, "candidate", at)[0]
    raw = archive.snapshot(source_engine, candidate, at)
    n = NewsEvidence(status="known", positive=True, available_at=at + timedelta(seconds=1))
    f = FundamentalEvidence(
        status="known",
        quality=True,
        available_at=at + timedelta(seconds=1),
        revenue_yoy_pct=5.0,
        operating_margin_pct=10.0,
    )
    feature = archive.features(candidate, raw, at, at, n, f)
    assert all(item.value is None for item in feature.news + feature.fundamental)
    assert all(item.missing for item in feature.news + feature.fundamental)


def test_real_base_source_finalizes_once_with_evidence_before_five_second_cutoff(
    source_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "version": "fusion_live_v1",
        "profile_hash": "a" * 64,
        "implementation_hash": "b" * 64,
    }
    monkeypatch.setattr(config, "identity", lambda: dict(identity))
    storage.ensure(source_engine)
    active = storage.activate(source_engine, lambda: NOW - timedelta(seconds=10))
    monkeypatch.setattr(service, "_views", lambda *_args: ({}, {}))
    moment = NOW + timedelta(seconds=3)
    result = service.tick(source_engine, moment, clock=lambda: moment)
    assert result["decisions"] == 1 and result["pending"] == 3
    candidate = shadow.list_events(source_engine, "candidate", moment)[0]
    assert candidate.confirmed_at is not None
    cutoff = candidate.confirmed_at + timedelta(seconds=55)
    decision = scoring._decision(
        source_engine, candidate, active, scoring.Identity(**identity), cutoff, "base", moment
    )
    assert decision.state == "PAPER_ACCEPT" and decision.verified and decision.timely
    assert decision.at is not None and decision.available_at is not None
    assert decision.used_at and all(
        when <= decision.at <= decision.available_at <= cutoff for when in decision.used_at
    )
    later = moment + timedelta(seconds=1)
    service.tick(source_engine, later, clock=lambda: later)
    assert len(storage.list_events(source_engine, "decision", later)) == 1
