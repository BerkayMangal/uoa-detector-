"""Independent boundaries for the additive prospective measurement stream."""
from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tests.unit._webapp_auth import set_web_auth

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("opening", "2518f30d9d7bc6f19b6d27aced19651f8861e204076d2737725b74e0eda6d276"),
        ("news_research", "2a389a08c1a2f48d41c08b39066401c1a0ee3d5f1c13d6afed93fdc273e27ed3"),
    ),
)
def test_new_measurement_preserves_frozen_experiment_implementations(
    name: str, expected: str,
) -> None:
    """Pin pre-5.58 source bytes, independently of cached runtime hash helpers."""
    folder = ROOT / "webapp" / name
    body = b"".join(p.name.encode() + p.read_bytes() for p in sorted(folder.glob("*.py")))
    if name == "news_research":
        contract = ROOT / "docs/phase-5.53-news-evidence-acceptance.md"
        body += contract.name.encode() + contract.read_bytes()
    assert hashlib.sha256(body).hexdigest() == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("opening_desk_v1.yaml", "d22d08ff23b86702c98222147f340b33e17f9550e73240c8ef7333ca04f787eb"),
        ("news_research_v1.yaml", "2551202732bebc67197f0d30e390bfe5ef0f7de411dc67c15a8392c8ec455185"),
    ),
)
def test_new_measurement_preserves_frozen_experiment_profiles(name: str, expected: str) -> None:
    assert hashlib.sha256((ROOT / "profiles" / name).read_bytes()).hexdigest() == expected


@pytest.fixture
def external_io_forbidden(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def blocked(*_args: object, **_kwargs: object) -> None:
        calls.append("external_http")
        raise AssertionError("Stored measurement GET attempted external HTTP")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    return calls


@pytest.fixture
def stored_desk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_io_forbidden: list[str],
) -> Iterator[tuple[TestClient, Engine, dict[str, str], list[str]]]:
    from webapp import main
    from webapp.board.db import make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'timed-read-only.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    headers = set_web_auth(monkeypatch)
    writes: list[str] = []

    def executed(
        _connection: object, _cursor: object, sql: str, _parameters: object,
        _context: object, _many: bool,
    ) -> None:
        first = sql.lstrip().split(None, 1)[0].upper()
        if first in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "REPLACE", "TRUNCATE"}:
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    client = TestClient(main.app)
    try:
        yield client, engine, headers, writes
        assert external_io_forbidden == []
    finally:
        client.close()
        engine.dispose()


@pytest.mark.parametrize("path", ("/olcum", "/api/olcum"))
@pytest.mark.parametrize("configured", (True, False))
def test_measurement_auth_gate_precedes_storage_or_capture_access(
    monkeypatch: pytest.MonkeyPatch, external_io_forbidden: list[str], path: str, configured: bool,
) -> None:
    from webapp import main

    attempts: list[str] = []

    def forbidden() -> None:
        attempts.append("private_backend")
        raise AssertionError("Authentication must run before measurement backends")

    if configured:
        set_web_auth(monkeypatch)
    else:
        monkeypatch.delenv("WEB_AUTH_USER", raising=False)
        monkeypatch.delenv("WEB_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(main, "_board_reader", forbidden)
    response = TestClient(main.app).get(path)
    assert response.status_code == (401 if configured else 503)
    assert attempts == [] and external_io_forbidden == []


@pytest.mark.parametrize("initialized", (False, True))
def test_actual_measurement_gets_are_select_only_and_empty_is_not_a_zero_return(
    stored_desk: tuple[TestClient, Engine, dict[str, str], list[str]], initialized: bool,
) -> None:
    from webapp.live_alpha import store as live_store
    from webapp.opening import storage as opening_storage
    from webapp.timed_measurement import storage

    client, engine, headers, writes = stored_desk
    if initialized:
        live_store.ensure_live_tables(engine)
        opening_storage.ensure(engine)
        storage.ensure(engine)
        writes.clear()
    for path in ("/api/olcum?day=2026-11-05", "/olcum?day=2026-11-05"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "noindex" in response.headers["x-robots-tag"]
        if path.startswith("/api"):
            body = response.json()
            assert body["rows"] == []
            assert body["summary"]["own_coverage_pct"] is None
            assert body["summary"]["paired_coverage_pct"] is None
            assert body["summary"]["own_observed"] in (None, 0)
            assert body["summary"]["paired"] in (None, 0)
            assert not body["first_live_observation_pending"] or body["activation_at"] is not None
            if not initialized:
                assert body["status"] == "STORAGE_UNAVAILABLE"
        else:
            assert 'href="/olcum"' in response.text
            assert "London" in response.text or "Londra" in response.text
    assert writes == []


def test_measurement_page_preserves_all_fifteen_navigation_destinations(
    stored_desk: tuple[TestClient, Engine, dict[str, str], list[str]],
) -> None:
    class Navigation(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_nav = False
            self.links: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "nav" and "desk-nav" in (values.get("class") or "").split():
                self.in_nav = True
            if self.in_nav and tag == "a" and values.get("href"):
                self.links.append(str(values["href"]))
                assert "hidden" not in values

        def handle_endtag(self, tag: str) -> None:
            if tag == "nav":
                self.in_nav = False

    client, _engine, headers, writes = stored_desk
    response = client.get("/olcum", headers=headers)
    assert response.status_code == 200
    nav = Navigation()
    nav.feed(response.text)
    assert len(nav.links) == 15
    assert set(nav.links) == {
        "/", "/acilis", "/pozisyonlar", "/sonuclar", "/olcum", "/karne", "/haberler",
        "/arastirma", "/laboratuvar", "/stratejiler", "/journal", "/defter", "/alfa", "/gamma", "/opsiyon",
    }
    assert writes == []


def test_public_health_exposes_only_safe_measurement_aggregates(
    monkeypatch: pytest.MonkeyPatch, external_io_forbidden: list[str],
) -> None:
    from webapp import main

    safe = {"status": "ok", "at": NOW.isoformat(), "targets": 2, "active": 1, "attempts": 1, "assessed": 0}
    monkeypatch.setattr(main.timed_service, "STATE", safe | {
        "ticker": "PRIVATE_TICKER", "payload": {"private_quote": 999},
        "database_url": "PRIVATE_DATABASE", "exception": "PRIVATE_EXCEPTION",
    })
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json()["timed_measurement"] == safe
    assert "PRIVATE_" not in response.text and external_io_forbidden == []


@pytest.mark.asyncio
async def test_real_lifespan_keeps_health_live_while_measurement_native_io_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise actual startup isolation, rather than testing an isolated helper."""
    from webapp import main

    from tests.unit.test_alfa_refresher_lifespan import _live_config

    started = threading.Event()
    released = threading.Event()
    owners: list[threading.Thread] = []
    calls: list[str] = []
    database_url = "postgresql://fixture:local-only@never-contacted.invalid/fixture"

    async def idle(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    async def blocked(_url: str) -> None:
        calls.append(_url)
        owners.append(threading.current_thread())
        started.set()
        assert released.wait(5)
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "live_config_from_env", lambda: _live_config(database_url))
    for name in ("run_live_worker", "gamma_refresh_loop", "board_refresh_loop", "_board_prerender_loop"):
        monkeypatch.setattr(main, name, idle)
    monkeypatch.setattr(main, "_start_live_alpha_thread", lambda _url: None)
    monkeypatch.setattr(main, "shared_news_enabled", lambda: False)
    monkeypatch.delenv("DAILY_REPAIR_REQUEST", raising=False)
    monkeypatch.setattr(main.timed_service, "enabled", lambda: True)
    monkeypatch.setattr(main.timed_service, "loop", blocked)
    try:
        async with main._lifespan(main.app):
            assert await asyncio.to_thread(started.wait, 3)
            assert owners[0].ident != threading.get_ident()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://testserver",
            ) as client:
                response = await asyncio.wait_for(client.get("/health"), 1)
            assert response.status_code == 200 and response.json()["ok"] is True
            assert calls == [database_url]
        # Shutdown cancellation returns without blocking on the native call and
        # does not ask the supervisor to spawn an overlapping replacement.
        assert calls == [database_url] and owners[0].is_alive()
    finally:
        released.set()
        if owners:
            await asyncio.to_thread(owners[0].join, 3)
            assert not owners[0].is_alive()
    assert calls == [database_url]


@pytest.mark.asyncio
async def test_real_single_attempt_transport_failure_is_durable_without_a_fake_receipt_clock(
    tmp_path: Path,
) -> None:
    """A known finished network failure is not an abandoned, replayable claim."""
    from pydantic import SecretStr
    from webapp.board.db import make_engine
    from webapp.timed_measurement import storage
    from webapp.timed_measurement.model import LegInput, Target, digest
    from webapp.timed_measurement.transport import QuoteTransport

    from uoa_detector.calibration.profile import UnusualWhalesSettings

    engine = make_engine(f"sqlite:///{tmp_path / 'failed-wire.db'}")
    storage.ensure(engine)
    activation = storage.activate(engine, lambda: NOW - timedelta(minutes=16))
    published = NOW - timedelta(minutes=15)
    target = Target(
        key=digest((activation.key, "synthetic-first", "m15")), activation_key=activation.key,
        profile_hash=activation.profile_hash, opening_profile_hash=activation.opening_profile_hash,
        primary_key="synthetic-primary", decision_key="synthetic-decision", ticker="AAPL",
        day=NOW.date(), direction="up", instrument="stock", published_at=published,
        entry_reference=100, entry_source_at=published, spy_entry=500, spy_entry_source_at=published,
        source_scan_id="synthetic-scan", source_snapshot_hash="a" * 64, source_card_hash="b" * 64,
        horizon="m15", horizon_minutes=15, target_at=NOW, deadline=NOW + timedelta(seconds=300),
        eligible=True, reason="", discovered_at=NOW,
    )
    assert storage.save_target(engine, target)
    request_at = NOW + timedelta(seconds=2)
    attempt = storage.claim(engine, target, 1, request_at)
    assert attempt is not None
    calls: list[str] = []

    def timed_out(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ReadTimeout("synthetic private provider detail", request=request)

    async def reserve() -> None:
        reservation = storage.reserve_leg(
            engine, attempt, "own", "AAPL", request_at, observed=157, shared_cap=24000,
        )
        assert reservation.granted

    client = QuoteTransport(
        api_key=SecretStr("synthetic-local-only"), settings=UnusualWhalesSettings(),
        timeout_seconds=8, max_response_bytes=65536, clock=lambda: request_at,
        transport=httpx.MockTransport(timed_out),
    )
    try:
        packet = await client.fetch("AAPL", before_request=reserve, deadline=target.deadline)
        assert packet.status in {"transport", "timeout"}
        assert packet.wire_attempted and packet.requested_at == request_at
        assert packet.received_at is None and packet.raw_body is None
        finished_at = request_at + timedelta(seconds=1)
        leg = storage.record_leg(engine, attempt, LegInput(
            leg="own", ticker="AAPL", status=packet.status, reason=packet.status,
            requested_at=packet.requested_at, received_at=packet.received_at,
            wire_attempted=packet.wire_attempted, http_status=packet.http_status,
            source_path="/api/stock/AAPL/quote",
        ), lambda: finished_at)
        assert leg.received_at is None and leg.price is None
        assert leg.receipt_at == leg.confirmed_at == finished_at
        completion = storage.complete(
            engine, attempt, leg, None, finished_at, reason="transport",
            own_confirmed_at=finished_at, clock=lambda: finished_at,
        )
        assert completion.own_key == leg.key and completion.spy_key is None
        assert len(storage.completions(engine, target.key, finished_at)) == 1
        assert storage.claim(engine, target, 1, NOW + timedelta(seconds=35)) is None
        assert storage.claim(engine, target, 2, NOW + timedelta(seconds=35)) is not None
        assert calls == ["https://api.unusualwhales.com/api/stock/AAPL/quote"]
    finally:
        await client.aclose()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_spy_available", (True, False))
async def test_original_publication_to_real_capture_to_read_only_report_keeps_old_streams_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_spy_available: bool,
) -> None:
    """No mocked discovery or report can hide a cross-layer binding regression."""
    from pydantic import SecretStr
    from webapp import main
    from webapp.board import quota_ledger
    from webapp.board.db import make_engine
    from webapp.live_alpha import store as live_store
    from webapp.opening import storage as opening_storage
    from webapp.opening.model import clock as opening_clock
    from webapp.timed_measurement import bindings, collector, storage, view
    from webapp.timed_measurement.transport import QuoteTransport

    from tests.unit.test_opening_desk import BASE, project, source
    from uoa_detector.calibration.profile import UnusualWhalesSettings

    engine = make_engine(f"sqlite:///{tmp_path / 'real-first-call.db'}")
    live_store.ensure_live_tables(engine)
    opening_storage.ensure(engine)
    storage.ensure(engine)
    published = NOW - timedelta(minutes=15)
    activation = storage.activate(engine, lambda: published - timedelta(seconds=1))
    original_scan = source(published, "synthetic-original")
    if not entry_spy_available:
        original_scan.snapshot["price_observations"]["SPY"] = {}
    advice = project(original_scan, published)
    assert advice.action == "BUY"
    live_store.write_scan(engine, original_scan, BASE.policy_version)
    assert opening_storage.publish(engine, original_scan, published, opening_clock(published), (advice,))
    discovered = bindings.discover(engine, activation, published + timedelta(seconds=1))
    assert {t.horizon for t in discovered} == {"m15", "m60"}
    target = next(t for t in discovered if t.horizon == "m15")
    assert target.spy_entry == (500 if entry_spy_available else None)
    assert target.entry_reference == 100

    # A later update is another publication, while the original primary and its
    # benchmark/target identity remain fixed.
    update_at = published + timedelta(minutes=2)
    update_scan = source(update_at, "synthetic-update", price=101)
    update_advice = project(update_scan, update_at)
    live_store.write_scan(engine, update_scan, BASE.policy_version)
    assert opening_storage.publish(engine, update_scan, update_at, opening_clock(update_at), (update_advice,))
    with Session(engine) as session:
        update_record = session.scalar(select(opening_storage.OpeningDecision).where(
            opening_storage.OpeningDecision.published_at == update_at,
        ))
        assert update_record is not None
        update_key = update_record.key
    assert bindings.discover(engine, activation, update_at) == ()
    assert all(t.decision_key == target.decision_key for t in storage.targets(engine, update_at))

    # An existing shared key-day floor is synthetic setup, not an extra probe.
    quota_ledger.reserve(engine, day=NOW.date(), n=1, cap=24000, observed=150, now=published)
    with Session(engine) as session:
        old_observations = session.scalar(select(func.count()).select_from(opening_storage.OpeningObservation))
        old_outcomes = session.scalar(select(func.count()).select_from(opening_storage.OpeningOutcome))
    capture_at = NOW + timedelta(seconds=2)
    wires: list[str] = []
    statements: list[str] = []

    def executed(
        _connection: object, _cursor: object, sql: str, _parameters: object,
        _context: object, _many: bool,
    ) -> None:
        if sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}:
            statements.append(sql)

    def quote_response(request: httpx.Request) -> httpx.Response:
        wires.append(request.url.path)
        return httpx.Response(200, json={"data": {
            "market_time": "regular",
            "last_trade": {"price": "505" if "/SPY/" in request.url.path else "102",
                           "time": int(capture_at.timestamp() * 1000)},
        }}, headers={"x-uw-daily-req-count": "157"})

    event.listen(engine, "before_cursor_execute", executed)
    transport = QuoteTransport(
        api_key=SecretStr("synthetic-local-only"), settings=UnusualWhalesSettings(),
        timeout_seconds=8, max_response_bytes=65536, clock=lambda: capture_at,
        transport=httpx.MockTransport(quote_response),
    )
    try:
        result = await collector.tick(engine, transport, capture_at, clock=lambda: capture_at)
        assert result.assessed == 1
        assert wires == ["/api/stock/NVDA/quote", "/api/stock/SPY/quote"]
        outcome = storage.assessment(engine, target.key, capture_at)
        assert outcome is not None and outcome.return_pct == pytest.approx(2)
        if entry_spy_available:
            assert outcome.excess_pp == pytest.approx(1)
        else:
            assert outcome.status == "partial" and outcome.excess_pp is None
            assert outcome.reason == "missing_entry_spy"
        # A terminal valid pair needs no further quotes even when its original
        # entry benchmark was absent and a later publication supplied one.
        later = capture_at + timedelta(seconds=35)
        await collector.tick(engine, transport, later, clock=lambda: later)
        assert len(wires) == 2
        assert not any("alfa_opening_observation" in sql or "alfa_opening_outcome" in sql for sql in statements)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(opening_storage.OpeningObservation)) == old_observations
            assert session.scalar(select(func.count()).select_from(opening_storage.OpeningOutcome)) == old_outcomes
        fresh = view.build(engine, capture_at, NOW.date())
        assert fresh.summary.own_observed == 1
        assert fresh.summary.paired == int(entry_spy_available)
        assert fresh.summary.pending == 1
        updated = view.build_call(engine, capture_at, update_key)
        assert updated.status == "UPDATE_NOT_ELIGIBLE" and updated.rows == ()
        assert updated.canonical_key == target.decision_key
        statements.clear()
        monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
        monkeypatch.setattr(main, "_now", lambda: capture_at)
        client = TestClient(main.app)
        try:
            response = client.get("/api/olcum?day=2026-11-05", headers=set_web_auth(monkeypatch))
            assert response.status_code == 200
            assert response.json()["summary"]["paired"] == int(entry_spy_available)
            assert statements == [] and len(wires) == 2
            # Corruption is injected only into this synthetic database. A
            # recorded assessment cannot outlive its physical evidence proof.
            assert outcome.own_key is not None
            with Session(engine) as session:
                packet_metadata = session.scalar(select(storage.TimedPacket.payload_json).where(
                    storage.TimedPacket.key == outcome.own_key,
                ))
            for damage in ({"payload_json": "{"}, {"payload_json": packet_metadata, "compressed_body": b"bad gzip"}):
                with engine.begin() as connection:
                    connection.execute(update(storage.TimedPacket).where(
                        storage.TimedPacket.key == outcome.own_key,
                    ).values(**damage))
                statements.clear()
                unknown = client.get("/api/olcum?day=2026-11-05", headers=set_web_auth(monkeypatch))
                assert unknown.status_code == 200
                measured = next(row for row in unknown.json()["rows"] if row["key"] == target.key)
                assert measured["own_pct"] is None and measured["excess_pp"] is None
                assert statements == [] and len(wires) == 2
        finally:
            client.close()
    finally:
        await transport.aclose()
        engine.dispose()
