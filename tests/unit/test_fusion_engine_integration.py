"""Real source→four feature workers→pre-entry decisions→priced daily accounting."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import AlfaBase, make_engine
from webapp.board.quota_ledger import ensure_quota_tables
from webapp.fundamental_context import service as financial_service
from webapp.fundamental_context import storage as financial_store
from webapp.fusion_live import service, storage, view
from webapp.semantic_news import service as semantic_service
from webapp.shadow_capture import collector
from webapp.shadow_capture import storage as source_store

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_fundamental_context_core import Client as FinancialClient
from tests.unit.test_news_adapter import NOW
from tests.unit.test_semantic_news_runtime import Client as SemanticClient
from tests.unit.test_semantic_news_runtime import metadata, seed
from tests.unit.test_shadow_collector import Client as QuoteClient
from tests.unit.test_shadow_collector import Clock


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for name in (
        "FUSION_ENGINE_ENABLED",
        "SEMANTIC_NEWS_ENABLED",
        "FUNDAMENTAL_CONTEXT_ENABLED",
        "SHADOW_CAPTURE_ENABLED",
    ):
        monkeypatch.setenv(name, "true")
    engine = make_engine(f"sqlite:///{tmp_path / 'engine-e2e.db'}")
    AlfaBase.metadata.create_all(engine)
    ensure_quota_tables(engine)
    source_store.activate(engine, lambda: NOW - timedelta(hours=1))
    active = storage.activate(engine, lambda: NOW - timedelta(minutes=10))
    financial_store.activate(engine, lambda: NOW - timedelta(minutes=10))
    clock = Clock(NOW + timedelta(seconds=5))
    yield SimpleNamespace(
        engine=engine, active=active, clock=clock, calls=[], closed=[], status="ok"
    )
    semantic_service._HALTS.clear()
    semantic_service._INFLIGHT.clear()
    financial_service._HALTS.clear()
    financial_service._OWN.clear()
    collector._HALTS.clear()
    collector._COOLDOWNS.clear()
    engine.dispose()


def prepare(env):
    seed(env.engine)
    first = service.tick(env.engine, env.clock(), clock=env.clock)
    assert first["decisions"] == 1 and first["pending"] == 3
    records = storage.list_events(env.engine, "decision", env.clock(), day=NOW.date())
    assert len(records) == 1 and records[0].arm == "base"
    env.clock.at = NOW + timedelta(seconds=14)
    model = SemanticClient(env.clock)
    result = asyncio.run(
        semantic_service.tick(env.engine, model, metadata(), env.clock(), clock=env.clock)
    )
    assert result["completed"] == 1 and len(model.calls) == 1

    async def pause(seconds):
        env.clock.at += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    asyncio.run(
        financial_service.tick(
            env.engine,
            env.clock(),
            client_factory=lambda: FinancialClient(env),
            clock=env.clock,
            sleep=pause,
        )
    )
    assert len(env.calls) == 2
    result = service.tick(env.engine, env.clock(), clock=env.clock)
    assert result["decisions"] == 4 and result["pending"] == 0
    decisions = storage.list_events(env.engine, "decision", env.clock(), day=NOW.date())
    assert len(decisions) == 4 and all(d.payload["state"] == "PAPER_ACCEPT" for d in decisions)
    assert all(d.available_at and d.available_at < NOW + timedelta(seconds=57) for d in decisions)
    assert all(d.payload["feature_row"]["event_id"] == d.candidate_key for d in decisions)
    return model


def test_real_multi_source_chain_to_three_quotes_and_daily_publication_select_only_api(
    environment, monkeypatch
):
    env = environment
    model = prepare(env)
    client = QuoteClient(env.clock)
    for second, prices in [
        (64, {"NVDA": 100.0, "SPY": 500.0}),
        (966, {"NVDA": 103.0, "SPY": 510.0}),
        (3666, {"NVDA": 98.0, "SPY": 495.0}),
    ]:
        env.clock.at = NOW + timedelta(seconds=second)
        client.prices = prices
        asyncio.run(collector.tick(env.engine, client, env.clock(), clock=env.clock))
    assert len(client.calls) == 6
    report = view.build(env.engine, env.clock())
    assert len(report.rows) == 1 and all(h.common_complete for h in report.horizons)
    assert all(a.net_usd == pytest.approx(69.564) for a in report.horizons[0].arms)
    assert all(a.net_usd == pytest.approx(-50.376) for a in report.horizons[1].arms)
    assert all(d.verified and d.timely for d in report.rows[0].decisions)
    # Outcomes never replay a model or change its four pre-entry decisions.
    assert service.tick(env.engine, env.clock(), clock=env.clock)["decisions"] == 4
    assert len(model.calls) == 1 and len(env.calls) == 2
    env.clock.at = NOW + timedelta(days=1)
    assert service.close_day(env.engine, env.clock(), clock=env.clock) == 1
    assert service.close_day(env.engine, env.clock(), clock=env.clock) == 0
    after = view.build(env.engine, env.clock(), NOW.date())
    assert after.stored_days == 1 and after.complete_common_sessions == 1
    assert after.readiness == "INSUFFICIENT_COMPLETE_SESSIONS" and after.winner is None
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=env.engine))
    monkeypatch.setattr(main, "_now", env.clock)
    writes = []
    event.listen(
        env.engine,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
            else None
        ),
    )
    c = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    for path in ("/api/motor", "/api/motor/haber", "/api/motor/temel", "/api/motor/arastirma"):
        assert c.get(path).status_code == 401
        response = c.get(path + "?day=" + NOW.date().isoformat(), headers=headers)
        assert response.status_code == 200, (path, response.text)
        assert "no-store" in response.headers["cache-control"]
        assert "noindex" in response.headers["x-robots-tag"]
        assert "raw_body" not in response.text and "Bearer " not in response.text
    assert not writes and len(client.calls) == 6 and len(model.calls) == 1 and len(env.calls) == 2


def test_disabled_fusion_worker_never_opens_database_or_runs_job(monkeypatch):
    monkeypatch.setenv("FUSION_ENGINE_ENABLED", "false")
    monkeypatch.setattr(
        service, "configured_engine", lambda *_: pytest.fail("disabled database access")
    )
    asyncio.run(service.loop("unused", max_cycles=1))


def test_late_model_does_not_rewrite_earlier_pending_into_paper_entry(environment):
    env = environment
    seed(env.engine)
    assert service.tick(env.engine, env.clock(), clock=env.clock)["pending"] == 3
    env.clock.at = NOW + timedelta(seconds=60)
    result = service.tick(env.engine, env.clock(), clock=env.clock)
    assert result["decisions"] == 4 and result["pending"] == 0
    decisions = storage.list_events(env.engine, "decision", env.clock(), day=NOW.date())
    assert len(decisions) == 4
    assert {d.arm: d.payload["state"] for d in decisions} == {
        "base": "PAPER_ACCEPT",
        "semantic_positive": "PAPER_UNKNOWN",
        "semantic_veto": "PAPER_UNKNOWN",
        "semantic_fundamental": "PAPER_UNKNOWN",
    }
    assert asdict(view.build(env.engine, env.clock()))["complete_common_sessions"] == 0
