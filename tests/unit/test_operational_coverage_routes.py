"""New operational reads remain private and cannot run maintenance or rewrite history."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import AlfaBase, make_engine
from webapp.fusion_live import storage

from tests.unit._webapp_auth import set_web_auth

NOW = datetime(2026, 11, 10, 11, tzinfo=UTC)
ACTIVATED = datetime(2026, 11, 7, 9, 33, tzinfo=UTC)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'operational-routes.db'}")
    AlfaBase.metadata.create_all(engine)
    storage.activate(engine, lambda: ACTIVATED)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    yield engine
    engine.dispose()


@pytest.mark.parametrize(
    "path", ["/api/motor/haber/tani?day=2026-11-09", "/api/arastirma/veri-gozcusu"]
)
def test_operational_gets_are_private_select_only_without_provider_or_job(
    environment, monkeypatch, path
):
    statements = []
    event.listen(
        environment, "before_cursor_execute", lambda _c, _cur, sql, *_a: statements.append(sql)
    )
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    assert client.get(path).status_code == 401
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    assert "noindex" in response.headers["x-robots-tag"]
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
        for sql in statements
    )
    assert "Bearer " not in response.text and "compressed_payload" not in response.text
    assert "snapshot_json" not in response.text and "raw_body" not in response.text


def test_research_navigation_selects_the_same_day_for_all_forward_views(environment, monkeypatch):
    calls = []
    for name in (
        "shadow_view",
        "fusion_view",
        "semantic_view",
        "fundamental_view",
        "news_diagnostics",
    ):
        module = getattr(main, name)
        original = module.build

        def observe(engine, now, day=None, *, original=original, name=name):
            calls.append((name, day))
            return original(engine, now, day)

        monkeypatch.setattr(module, "build", observe)
    client = TestClient(main.app)
    response = client.get("/arastirma?day=2026-11-09", headers=set_web_auth(monkeypatch))
    assert response.status_code == 200
    assert calls == [
        (name, date(2026, 11, 9))
        for name in (
            "shadow_view",
            "fusion_view",
            "semantic_view",
            "fundamental_view",
            "news_diagnostics",
        )
    ]
    assert "Ölçüm günü <b>09.11.2026</b>" in response.text
    assert "Güncel seansa dön" in response.text
    assert "/arastirma?day=2026-11-06" not in response.text
    today = client.get("/arastirma", headers=set_web_auth(monkeypatch))
    assert today.status_code == 200
    assert "/arastirma?day=2026-11-09#fusion-engine" in today.text
    assert today.text.count('id="live-alert-controls"') == 1


def test_future_measurement_day_refused_before_any_database_read(monkeypatch):
    monkeypatch.setattr(main, "_now", lambda: NOW)
    monkeypatch.setattr(main, "_board_reader", lambda: pytest.fail("future-day database read"))
    client = TestClient(main.app)
    response = client.get("/arastirma?day=2026-11-11", headers=set_web_auth(monkeypatch))
    assert response.status_code == 400
