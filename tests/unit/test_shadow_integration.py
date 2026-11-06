"""Source → prospective capture → verified private API and owned worker lifetime."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables
from webapp.live_alpha import store as live
from webapp.news_research import storage as news_storage
from webapp.shadow_capture import bindings, collector, config, service, storage

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_news_adapter import NOW, news, scan
from tests.unit.test_shadow_collector import Client, Clock


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_CAPTURE_ENABLED", "true")
    e = make_engine(f"sqlite:///{tmp_path / 'source-capture.db'}")
    live.ensure_live_tables(e)
    news_storage.ensure(e)
    storage.ensure(e)
    ensure_quota_tables(e)
    active = storage.activate(e, lambda: NOW - timedelta(hours=1))
    yield SimpleNamespace(engine=e, active=active)
    collector._HALTS.clear()
    collector._COOLDOWNS.clear()
    e.dispose()


def test_actual_source_to_three_paired_references_reaches_private_select_only_api(
    setup, monkeypatch
):
    e = setup.engine
    scan(e)
    news(e)
    clock = Clock(NOW + timedelta(seconds=2))
    assert bindings.discover(e, clock(), clock=clock)["admitted"] == 1
    client = Client(clock)
    for second, prices in [
        (64, {"NVDA": 100.0, "SPY": 500.0}),
        (966, {"NVDA": 103.0, "SPY": 510.0}),
        (3666, {"NVDA": 98.0, "SPY": 495.0}),
    ]:
        clock.at = NOW + timedelta(seconds=second)
        client.prices = prices
        asyncio.run(collector.tick(e, client, clock(), clock=clock))
    assert len(client.calls) == 6
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    monkeypatch.setattr(main, "_now", clock)
    headers = set_web_auth(monkeypatch)
    writes = []
    event.listen(
        e,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
            else None
        ),
    )
    c = TestClient(main.app)
    path = "/api/arastirma/aday-olcum"
    assert c.get(path).status_code == 401
    response = c.get(path, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "shadow_price_v1" and body["candidate_count"] == 1
    phases = {p["phase"]: p for p in body["rows"][0]["phases"]}
    assert all(p["state"] == "observed" for p in phases.values())
    assert phases["m15"]["own_pct"] == pytest.approx(3)
    assert phases["m15"]["excess_pp"] == pytest.approx(1)
    assert phases["m60"]["own_pct"] == pytest.approx(-2)
    assert phases["m60"]["model_net_usd"] == pytest.approx(-50.376)
    assert body["winner"] is None and body["inference"] == "NOT_PERFORMED"
    assert len(client.calls) == 6 and not writes
    assert "no-store" in response.headers["cache-control"]
    assert "noindex" in response.headers["x-robots-tag"]
    assert "raw_body" not in response.text and "blob" not in response.text


def test_missing_storage_get_never_bootstraps_schema_or_activation(tmp_path, monkeypatch):
    e = make_engine(f"sqlite:///{tmp_path / 'missing.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    writes = []
    event.listen(
        e,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE"))
            else None
        ),
    )
    r = TestClient(main.app).get("/api/arastirma/aday-olcum", headers=set_web_auth(monkeypatch))
    assert r.status_code == 200 and r.json()["status"] == "STORAGE_UNAVAILABLE"
    assert r.json()["candidate_count"] is None and not writes
    e.dispose()


def test_owned_worker_resumes_unclaimed_capture_with_existing_activation(setup, monkeypatch):
    e = setup.engine
    scan(e)
    news(e)
    clock = Clock(NOW + timedelta(seconds=2))
    bindings.discover(e, clock(), clock=clock)
    clock.at = NOW + timedelta(seconds=64)
    client = Client(clock)
    closed = []

    async def close():
        closed.append(True)

    client.aclose = close
    monkeypatch.setattr(service, "configured_engine", lambda _url: e)
    asyncio.run(service.loop("fixture", client_factory=lambda: client, clock=clock, max_cycles=1))
    assert client.calls == ["NVDA", "SPY"] and closed == [True]
    assert service.safe_status()["activation_at"] == setup.active.confirmed_at.isoformat()
    assert service.safe_status()["status"] == "active"


def test_disabled_owned_worker_does_no_database_or_provider_work(monkeypatch):
    monkeypatch.setenv("SHADOW_CAPTURE_ENABLED", "false")
    monkeypatch.setattr(service, "configured_engine", lambda *_: pytest.fail("disabled DB access"))
    asyncio.run(
        service.loop("unused", client_factory=lambda: pytest.fail("disabled provider access"))
    )


def test_database_pool_is_separate_and_has_native_query_timeout_protection(monkeypatch):
    calls = []
    marker = object()
    monkeypatch.setattr(service, "create_engine", lambda *a, **k: calls.append((a, k)) or marker)
    assert service.configured_engine("postgresql://u:p@localhost/db") is marker
    args = calls[0][1]
    assert args["pool_size"] == 2 and args["max_overflow"] == 0 and args["pool_timeout"] == 2
    assert "statement_timeout=2000" in args["connect_args"]["options"]
    assert "max_parallel_workers_per_gather=0" in args["connect_args"]["options"]
    assert config.settings().module_daily_cap == 96
