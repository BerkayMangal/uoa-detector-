"""Prospective fixed-arm accounting; all prices/model decisions here are fixtures."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, update
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables
from webapp.fusion_live import config, scoring, storage, view
from webapp.live_alpha import store
from webapp.news_research import storage as news_storage
from webapp.shadow_capture import bindings, collector
from webapp.shadow_capture import storage as source_storage
from webapp.shadow_capture.model import digest

from tests.unit.test_news_adapter import NOW, scan, snapshot
from tests.unit.test_shadow_collector import Client, Clock
from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("SHADOW_CAPTURE_ENABLED", "true")
    identity = {
        "version": "fusion_live_v1",
        "profile_hash": "a" * 64,
        "implementation_hash": "b" * 64,
    }
    monkeypatch.setattr(config, "identity", lambda: dict(identity))
    engine = make_engine(f"sqlite:///{tmp_path / 'fusion-forward.db'}")
    store.ensure_live_tables(engine)
    news_storage.ensure(engine)
    source_storage.ensure(engine)
    ensure_quota_tables(engine)
    storage.ensure(engine)
    source_storage.activate(engine, lambda: NOW - timedelta(hours=1))
    active = storage.activate(engine, lambda: NOW - timedelta(minutes=30))
    yield SimpleNamespace(engine=engine, active=active)
    collector._HALTS.clear()
    collector._COOLDOWNS.clear()
    engine.dispose()


def admit(fixture, count=4):
    engine = fixture.engine
    data = snapshot()
    template = copy.deepcopy(data)
    tickers = ("NVDA", "AMD", "MSFT", "AAPL")[:count]
    data["flow_observations"] = {}
    data["news_observations"] = {}
    for ticker in tickers:
        data["flow_observations"][ticker] = copy.deepcopy(template["flow_observations"]["NVDA"])
        data["news_observations"][ticker] = copy.deepcopy(template["news_observations"]["NVDA"])
        data["news_research_context"]["prices"][ticker] = copy.deepcopy(
            template["news_research_context"]["prices"]["NVDA"]
        )
        news_storage.append(
            engine,
            ticker,
            {
                "data": [
                    {
                        "headline": "Synthetic company wins contract",
                        "source": "FixtureWire",
                        "tickers": [ticker],
                        "created_at": (NOW - timedelta(hours=1)).isoformat(),
                    }
                ]
            },
            NOW - timedelta(seconds=30),
            NOW - timedelta(seconds=20),
            clock=lambda: NOW - timedelta(seconds=10),
            application_checked_at=NOW - timedelta(seconds=30),
        )
    scan(engine, data=data)
    clock = Clock(NOW + timedelta(seconds=2))
    assert bindings.discover(engine, clock(), clock=clock)["admitted"] == count
    return source_storage.list_events(engine, "candidate", clock(), day=NOW.date())


def publish(fixture, sources, states=None, omit=(), changes=None, confirmed_second=10):
    states = states or {}
    for source in sources:
        for arm in config.settings().arms:
            if (source.key, arm) in omit:
                continue
            cutoff = source.confirmed_at + timedelta(seconds=55)
            payload = {
                "identity": config.identity(),
                "activation_key": fixture.active.key,
                "candidate_checksum": source.checksum,
                "ticker": source.payload["ticker"],
                "cutoff": cutoff.isoformat(),
                "state": states.get(arm, "PAPER_ACCEPT"),
                "reasons": ["Synthetic fixed-arm decision"],
                "used_at": [source.confirmed_at.isoformat()],
                "news": {"status": "fixture"},
                "fundamental": {"status": "fixture"},
            }
            if changes:
                payload.update(changes)
            assert (
                storage.put(
                    fixture.engine,
                    key=digest((config.settings().version, fixture.active.key, source.key, arm)),
                    kind="decision",
                    at=NOW + timedelta(seconds=10),
                    payload=payload,
                    day=NOW.date(),
                    candidate_key=source.key,
                    arm=arm,
                    clock=lambda: NOW + timedelta(seconds=confirmed_second),
                )
                is not None
            )


def capture(fixture, sources, include_m60=True):
    clock = Clock()
    client = Client(clock)
    tickers = tuple(source.payload["ticker"] for source in sources)
    cases = [(64, 100.0, 500.0), (966, 103.0, 510.0)]
    if include_m60:
        cases.append((3666, 98.0, 495.0))
    for second, price, spy in cases:
        clock.at = NOW + timedelta(seconds=second)
        client.prices = {ticker: price for ticker in tickers} | {"SPY": spy}
        asyncio.run(collector.tick(fixture.engine, client, clock(), clock=clock))
    return client


def arm(day, horizon, name):
    return next(
        value
        for value in next(h for h in day.horizons if h.horizon == horizon).arms
        if value.arm == name
    )


def serialized(day):
    return json.loads(json.dumps(asdict(day), default=lambda value: value.isoformat()))


def save_day(fixture, body, at=None, key=None, owner=None):
    day = date.fromisoformat(body["day"])
    at = at or scoring.session_close(day) + timedelta(seconds=2)
    return storage.put(
        fixture.engine,
        key=key
        or digest((config.settings().version, fixture.active.key, day.isoformat(), "day_result")),
        kind="day_result",
        at=at,
        day=day,
        candidate_key=owner or fixture.active.key,
        payload={
            "identity": config.identity(),
            "activation_key": fixture.active.key,
            "result": body,
        },
        clock=lambda: at,
    )


def test_absent_storage_get_does_not_create_schema_or_activate(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_ENGINE_ENABLED", "true")
    engine = make_engine(f"sqlite:///{tmp_path / 'absent.db'}")
    writes = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _c, _cu, sql, *_a: (
            writes.append(sql)
            if sql.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE"))
            else None
        ),
    )
    report = view.build(engine, NOW)
    assert report.status == "STORAGE_UNAVAILABLE" and report.candidate_count is None
    assert report.complete_common_sessions == 0 and not writes
    engine.dispose()


def test_active_empty_session_is_not_a_zero_return_eligible_day(setup):
    day = scoring.build_day(setup.engine, NOW.date(), NOW)
    assert day.status == "NO_CANDIDATES" and day.candidates == 0
    assert not any(h.common_complete for h in day.horizons)
    assert all(a.net_usd is None for h in day.horizons for a in h.arms)
    body = serialized(
        scoring.build_day(setup.engine, NOW.date(), scoring.session_close(NOW.date()))
    )
    save_day(setup, body)
    report = view.build(setup.engine, scoring.session_close(NOW.date()) + timedelta(seconds=3))
    assert report.complete_common_sessions == 0 and report.stored_days == 1


def test_all_four_original_candidates_remain_when_a_decision_claim_is_missing(setup):
    sources = admit(setup)
    publish(setup, sources, omit=((sources[0].key, "semantic_positive"),))
    capture(setup, sources)
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=4000))
    assert day.candidates == 4 and len(day.rows) == 4
    missing = next(row for row in day.rows if row.key == sources[0].key)
    assert (
        next(d for d in missing.decisions if d.arm == "semantic_positive").state == "PAPER_UNKNOWN"
    )
    assert arm(day, "m15", "semantic_positive").net_usd is None
    assert arm(day, "m15", "base").net_usd == pytest.approx(278.256)
    assert not any(h.common_complete for h in day.horizons)


def test_known_skip_cash_zero_waits_for_the_entire_reference_and_arm_panel(setup):
    sources = admit(setup)
    publish(setup, sources, {"semantic_positive": "PAPER_SKIP"})
    waiting = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=20))
    assert all(
        next(d for d in row.decisions if d.arm == "semantic_positive").m15_net_usd is None
        for row in waiting.rows
    )
    capture(setup, sources)
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=4000))
    assert arm(day, "m15", "semantic_positive").net_usd == 0
    assert arm(day, "m60", "semantic_positive").net_usd == 0
    assert arm(day, "m15", "base").net_usd == pytest.approx(278.256)
    assert arm(day, "m60", "base").net_usd == pytest.approx(-201.504)
    assert all(
        next(d for d in row.decisions if d.arm == "semantic_positive").m15_net_usd == 0
        for row in day.rows
    )


def test_one_missing_decision_blocks_cash_zero_for_other_skips_in_the_same_arm(setup):
    sources = admit(setup)
    publish(
        setup,
        sources,
        {"semantic_positive": "PAPER_SKIP"},
        omit=((sources[0].key, "semantic_positive"),),
    )
    capture(setup, sources)
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=4000))
    assert arm(day, "m15", "semantic_positive").unknown == 1
    assert arm(day, "m15", "semantic_positive").net_usd is None
    assert all(
        next(d for d in row.decisions if d.arm == "semantic_positive").m15_net_usd is None
        for row in day.rows
    )


@pytest.mark.parametrize(
    "damage", ["future_used", "naive_used", "wrong_checksum", "wrong_cutoff", "wrong_identity"]
)
def test_decision_clocks_and_source_bindings_are_revalidated_before_scoring(setup, damage):
    sources = admit(setup, 1)
    changes = {
        "future_used": {"used_at": [(NOW + timedelta(seconds=11)).isoformat()]},
        "naive_used": {"used_at": ["2026-11-04T15:00:02"]},
        "wrong_checksum": {"candidate_checksum": "f" * 64},
        "wrong_cutoff": {"cutoff": (NOW + timedelta(seconds=58)).isoformat()},
        "wrong_identity": {"identity": {"version": "different"}},
    }[damage]
    publish(setup, sources, changes=changes)
    capture(setup, sources)
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=4000))
    assert day.candidates == 1 and all(d.state == "PAPER_UNKNOWN" for d in day.rows[0].decisions)
    assert all(a.net_usd is None for h in day.horizons for a in h.arms)


def test_late_postcommit_receipt_never_scores_a_skip_as_cash_or_an_accept_as_entry(setup):
    sources = admit(setup, 1)
    publish(setup, sources, {"semantic_positive": "PAPER_SKIP"}, confirmed_second=58)
    capture(setup, sources)
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=4000))
    assert all(
        d.recorded_state in ("PAPER_ACCEPT", "PAPER_SKIP")
        and d.state == "PAPER_UNKNOWN"
        and not d.timely
        for d in day.rows[0].decisions
    )
    assert all(a.net_usd is None for h in day.horizons for a in h.arms)


def test_source_started_before_new_activation_is_ignored_despite_later_finish_and_candidate_receipt(
    setup, monkeypatch
):
    identity = dict(config.identity(), implementation_hash="c" * 64)
    monkeypatch.setattr(config, "identity", lambda: dict(identity))
    setup.active = storage.activate(setup.engine, lambda: NOW - timedelta(seconds=3))
    admit(setup)
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=20))
    assert day.candidates == 0 and day.ignored_candidates == 4 and day.source_candidates == 4
    assert day.status == "NO_CANDIDATES" and all(h.arms[0].net_usd is None for h in day.horizons)


def test_midday_new_activation_cannot_replace_the_existing_first_four_slots(setup, monkeypatch):
    sources = admit(setup)
    identity = dict(config.identity(), implementation_hash="d" * 64)
    monkeypatch.setattr(config, "identity", lambda: dict(identity))
    setup.active = storage.activate(setup.engine, lambda: NOW + timedelta(seconds=10))
    later = NOW + timedelta(minutes=10)
    scan(setup.engine, key="later", at=later)
    assert bindings.discover(setup.engine, later, clock=lambda: later)["admitted"] == 0
    day = scoring.build_day(setup.engine, NOW.date(), later)
    assert len(sources) == 4 and day.candidates == 0 and day.ignored_candidates == 4
    assert day.source_candidates == 4 and day.status == "NO_CANDIDATES"


def test_invalid_original_candidate_cannot_disappear_into_a_survivor_total(setup):
    sources = admit(setup)
    publish(setup, sources)
    capture(setup, sources)
    with setup.engine.begin() as connection:
        connection.execute(
            update(source_storage.ShadowEvent)
            .where(source_storage.ShadowEvent.key == sources[0].key)
            .values(checksum="bad")
        )
    day = scoring.build_day(setup.engine, NOW.date(), NOW + timedelta(seconds=4000))
    assert day.candidates == 3 and day.invalid_candidates == 1 and not day.universe_verified
    assert all(a.net_usd is None for h in day.horizons for a in h.arms)


def test_readers_make_no_capture_model_or_database_writes(setup, monkeypatch):
    sources = admit(setup, 1)
    publish(setup, sources)
    client = capture(setup, sources)
    writes = []
    event.listen(
        setup.engine,
        "before_cursor_execute",
        lambda _c, _cu, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
            else None
        ),
    )
    monkeypatch.setattr(collector, "tick", lambda *_a, **_k: pytest.fail("GET captured prices"))
    report = view.build(setup.engine, NOW + timedelta(seconds=4000))
    assert report.candidate_count == 1 and len(client.calls) == 6 and not writes
    assert report.inference == "NOT_PERFORMED" and report.winner is None


def test_stored_common_panel_reconciles_real_source_reference_math_and_preserves_horizons(setup):
    sources = admit(setup)
    publish(setup, sources, {"semantic_positive": "PAPER_SKIP"})
    capture(setup, sources)
    close = scoring.session_close(NOW.date())
    body = serialized(scoring.build_day(setup.engine, NOW.date(), close))
    assert body["status"] == "COMPLETE"
    save_day(setup, body)
    report = view.build(setup.engine, close + timedelta(seconds=3))
    assert (
        report.complete_common_sessions == 1
        and report.stored_days == 1
        and report.invalid_days == 0
    )
    assert [p.complete_sessions for p in report.progress] == [1, 1]
    fifteen = next(p for p in report.progress if p.horizon == "m15")
    assert fifteen.arms[0].net_usd == pytest.approx(278.256)
    assert fifteen.arms[1].net_usd == 0
    assert fifteen.arms[1].difference_from_base_usd == pytest.approx(-278.256)
    assert report.readiness == "INSUFFICIENT_COMPLETE_SESSIONS"


def test_one_completed_horizon_is_not_counted_as_two_complete_sessions(setup):
    sources = admit(setup, 1)
    publish(setup, sources)
    capture(setup, sources, include_m60=False)
    close = scoring.session_close(NOW.date())
    body = serialized(scoring.build_day(setup.engine, NOW.date(), close))
    assert body["status"] == "INCOMPLETE"
    save_day(setup, body)
    report = view.build(setup.engine, close + timedelta(seconds=3))
    assert [p.complete_sessions for p in report.progress] == [1, 0]
    assert report.complete_common_sessions == 0 and report.invalid_days == 0


@pytest.mark.parametrize(
    "damage",
    [
        "net",
        "state",
        "late",
        "source_price",
        "size",
        "duplicate_slot",
        "used_clock",
        "reference_clock",
    ],
)
def test_resealed_but_inconsistent_daily_result_is_not_counted_as_valid_history(setup, damage):
    sources = admit(setup, 1)
    publish(setup, sources)
    capture(setup, sources)
    close = scoring.session_close(NOW.date())
    body = serialized(scoring.build_day(setup.engine, NOW.date(), close))
    if damage == "net":
        body["horizons"][0]["arms"][0]["net_usd"] += 1
    elif damage == "state":
        body["rows"][0]["decisions"][0]["state"] = "PAPER_UNKNOWN"
    elif damage == "late":
        body["rows"][0]["decisions"][0]["available_at"] = (NOW + timedelta(seconds=58)).isoformat()
    elif damage == "source_price":
        body["rows"][0]["phases"][1]["own_price"] = 0
    elif damage == "size":
        body["rows"][0]["phases"][1]["reference_quantity"] = True
    elif damage == "duplicate_slot":
        body["rows"].append(copy.deepcopy(body["rows"][0]))
        body["candidates"] = 2
    elif damage == "used_clock":
        body["rows"][0]["decisions"][0]["used_at"] = ["2026-11-04T15:00:02"]
    else:
        body["rows"][0]["phases"][1]["confirmed_at"] = (close + timedelta(seconds=1)).isoformat()
    save_day(setup, body)
    report = view.build(setup.engine, close + timedelta(seconds=3))
    assert report.complete_common_sessions == 0 and report.invalid_days == 1


def test_old_source_identity_is_not_pooled_even_when_fusion_identity_matches(setup):
    sources = admit(setup, 1)
    publish(setup, sources)
    capture(setup, sources)
    close = scoring.session_close(NOW.date())
    body = serialized(scoring.build_day(setup.engine, NOW.date(), close))
    body["source_identity"]["implementation_hash"] = "9" * 64
    save_day(setup, body)
    report = view.build(setup.engine, close + timedelta(seconds=3))
    assert report.complete_common_sessions == 0 and report.ignored_days == 1


def shift_day(body, day):
    delta = day - date.fromisoformat(body["day"])

    def shift(value):
        if isinstance(value, dict):
            return {key: shift(item) for key, item in value.items()}
        if isinstance(value, list):
            return [shift(item) for item in value]
        if isinstance(value, str) and value.startswith("2026-11-04T"):
            return (datetime.fromisoformat(value) + delta).isoformat()
        return value

    out = shift(copy.deepcopy(body))
    out["day"] = day.isoformat()
    # The runtime activation is not shifted with synthetic source/decision clocks.
    out["activation_at"] = body["activation_at"]
    for row in out["rows"]:
        row["key"] = bindings.candidate_key(day, row["slot"])
        row["checksum"] = digest(("synthetic_future_candidate", day.isoformat(), row["slot"]))
    return out


def test_eighty_distinct_complete_future_sessions_are_required_without_horizon_pooling(setup):
    sources = admit(setup, 1)
    publish(setup, sources)
    capture(setup, sources)
    close = scoring.session_close(NOW.date())
    template = serialized(scoring.build_day(setup.engine, NOW.date(), close))
    calendar = load_live_alpha_settings().calendar
    day = NOW.date()
    last = close
    for number in range(80):
        while is_session_day(day, calendar) is not True:
            day += timedelta(days=1)
        body = shift_day(template, day)
        last = max(scoring.session_close(day), datetime.fromisoformat(body["as_of"])) + timedelta(
            seconds=2
        )
        save_day(setup, body, at=last)
        if number == 78:
            report = view.build(setup.engine, last + timedelta(seconds=1))
            assert (
                report.complete_common_sessions == 79
                and report.readiness == "INSUFFICIENT_COMPLETE_SESSIONS"
            )
        day += timedelta(days=1)
    report = view.build(setup.engine, last + timedelta(seconds=1))
    assert report.complete_common_sessions == 80
    assert [p.complete_sessions for p in report.progress] == [80, 80]
    assert (
        report.readiness == "READY_FOR_REVIEW"
        and report.inference == "NOT_PERFORMED"
        and report.winner is None
    )


def test_history_read_bound_is_explicit_and_cannot_promote_an_unseen_prefix(setup):
    for number in range(config.settings().history_read_limit + 1):
        day = NOW.date() - timedelta(days=number)
        storage.put(
            setup.engine,
            key=digest(("bounded_fixture", number)),
            kind="day_result",
            at=NOW,
            day=day,
            candidate_key=setup.active.key,
            payload={"identity": config.identity()},
            clock=lambda: NOW,
        )
    report = view.build(setup.engine, NOW)
    assert report.limited and report.complete_common_sessions == 0
    assert report.readiness == "INSUFFICIENT_COMPLETE_SESSIONS"
