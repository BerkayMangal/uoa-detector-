"""One private canonical alert controller must not introduce reads/writes on public pages."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import create_tables, make_engine
from webapp.journal import JournalRepo

from tests.unit._webapp_auth import set_web_auth

PRIVATE_SCREENS = ("/", "/acilis", "/pozisyonlar", "/haberler", "/journal")
NOW = datetime(2026, 11, 4, 15, tzinfo=UTC)


class Markup(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.ids: Counter[str] = Counter()
        self.attributes: dict[str, dict[str, str | None]] = {}
        self.scripts: list[str] = []
        self.form_depth = 0
        self.controller_inside_form = False
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.form_depth += 1
        identity = values.get("id")
        if identity:
            self.ids[identity] += 1
            self.attributes[identity] = values
            if identity == "live-alert-controls" and self.form_depth:
                self.controller_inside_form = True
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.form_depth = max(0, self.form_depth - 1)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def blocked(*_args: object, **_kwargs: object) -> None:
        attempts.append("external_http")
        raise AssertionError("A stored-data GET must not make an external HTTP request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(main.pricing, "snapshot", blocked)
    return attempts


@pytest.fixture
def desk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: list[str]
) -> Iterator[tuple[TestClient, dict[str, str], list[str]]]:
    url = f"sqlite:///{tmp_path / 'signal-clarity.db'}"
    engine = make_engine(url)
    create_tables(engine)
    main.live_page.ensure_ready(engine)
    journal = JournalRepo(url)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_journal", lambda: journal)
    monkeypatch.setattr(main, "_now", lambda: NOW)
    monkeypatch.setenv("SHARED_NEWS_ENABLED", "true")
    writes: list[str] = []

    def executed(
        _connection: object, _cursor: object, sql: str, _parameters: object,
        _context: object, _many: bool,
    ) -> None:
        if sql.lstrip().split(None, 1)[0].upper() in {
            "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE", "TRUNCATE",
        }:
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    event.listen(journal._engine, "before_cursor_execute", executed)
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    try:
        yield client, headers, writes
        assert no_network == []
    finally:
        client.close()
        engine.dispose()
        journal._engine.dispose()


@pytest.mark.parametrize("path", PRIVATE_SCREENS)
def test_private_screens_share_one_canonical_controller_without_get_side_effects(
    desk: tuple[TestClient, dict[str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    client, headers, writes = desk
    cfg = main.live_alerts.settings()
    settings_calls: list[str] = []

    def settings() -> object:
        settings_calls.append("read")
        return cfg

    monkeypatch.setattr(main.live_alerts, "settings", settings)
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]
    page = Markup(response.text)
    for identity in (
        "signal-alerts", "live-alert-controls", "signal-toast", "alerts-toggle",
        "alerts-desktop", "alerts-desktop-test", "alerts-test", "alerts-scope",
        "alerts-exits", "alerts-channel-status", "alerts-status", "signal-refresh",
    ):
        assert page.ids[identity] == 1, (path, identity)
    assert sum(urlsplit(url).path == "/assets/live-alerts.js" for url in page.scripts) == 1
    controller = page.attributes["live-alert-controls"]
    assert controller["data-feed-url"] == "/api/acilis/uyarilar"
    assert controller["data-poll-seconds"] == str(cfg.poll_seconds)
    assert page.attributes["alerts-toggle"]["aria-pressed"] == "false"
    assert not page.controller_inside_form
    assert settings_calls == ["read"]
    assert writes == []


def test_public_introduction_never_loads_alerts_or_reads_private_configuration(
    monkeypatch: pytest.MonkeyPatch, no_network: list[str],
) -> None:
    attempts: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        attempts.append("private_access")
        raise AssertionError("Public introduction must remain static")

    for name in ("_board_reader", "_repo", "_journal", "_gamma"):
        monkeypatch.setattr(main, name, forbidden)
    monkeypatch.setattr(main.live_alerts, "settings", forbidden)
    monkeypatch.delenv("WEB_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_AUTH_PASSWORD", raising=False)
    response = TestClient(main.app).get("/hakkinda")
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "index, follow"
    page = Markup(response.text)
    assert not any(urlsplit(url).path == "/assets/live-alerts.js" for url in page.scripts)
    for identity in ("signal-alerts", "live-alert-controls", "signal-toast", "alerts-toggle", "alerts-desktop"):
        assert page.ids[identity] == 0
    assert attempts == [] and no_network == []


def test_unauthenticated_private_pages_cannot_reach_alert_settings_or_backends(
    monkeypatch: pytest.MonkeyPatch, no_network: list[str],
) -> None:
    set_web_auth(monkeypatch)
    attempts: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        attempts.append("private_access")
        raise AssertionError("Authentication must run before private page context")

    monkeypatch.setattr(main, "_board_reader", forbidden)
    monkeypatch.setattr(main, "_journal", forbidden)
    monkeypatch.setattr(main.live_alerts, "settings", forbidden)
    client = TestClient(main.app)
    for path in PRIVATE_SCREENS:
        response = client.get(path)
        assert response.status_code == 401
        assert 'id="live-alert-controls"' not in response.text
    assert attempts == [] and no_network == []
