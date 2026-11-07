"""Actual source receipts → durable bounded inference → read-only causal report."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event, update
from webapp.board.db import make_engine
from webapp.live_alpha import store as live
from webapp.news_research import storage as physical
from webapp.semantic_news import config, service, sources, storage, view
from webapp.semantic_news.model import Metadata, canonical
from webapp.semantic_news.transport import MetaResult, Packet, Transport
from webapp.shadow_capture import bindings
from webapp.shadow_capture import storage as shadow

from tests.unit.test_news_adapter import NOW, scan, snapshot


class Clock:
    def __init__(self, at=NOW + timedelta(seconds=3)):
        self.at = at

    def __call__(self):
        return self.at


def metadata(**changes):
    values = dict(
        version="semantic_news_v1",
        model="test-grok",
        prompt_hash=config.settings().expected_prompt_hash,
        schema_hash=config.settings().expected_schema_hash,
        configured=True,
        max_attempts=8,
        max_input_bytes=16384,
        max_items=3,
        max_headline_characters=1600,
        max_output_tokens=2048,
        timeout_seconds=45,
    )
    values.update(changes)
    return Metadata(**values)


def output(request, clock, **changes):
    items = []
    for item in request.items:
        items.append(
            dict(
                id=item.id,
                ticker=request.ticker,
                relation="direct",
                event="guidance",
                direction="positive",
                certainty="confirmed",
                materiality="high",
                reason="Issuer raised its outlook.",
                evidence=[
                    dict(quote=item.headline, field=f) for f in ("direction", "event", "certainty")
                ],
                limitations=["Headline only."],
            )
        )
    values = dict(
        version="semantic_news_v1",
        input_hash=request.request_id,
        **{k: v for k, v in metadata().binding().items() if k != "version"},
        status="completed",
        analysis_started_at=clock().isoformat(),
        analysis_finished_at=clock().isoformat(),
        recorded_at=clock().isoformat(),
        items=items,
        usage=dict(input_tokens=100, output_tokens=100, cost_usd=None),
    )
    values.update(changes)
    return values


class Client:
    def __init__(self, clock, transform=None):
        self.clock = clock
        self.calls = []
        self.transform = transform
        self.closed = False

    async def metadata(self):
        return MetaResult("configured", metadata())

    async def analyze(self, request, *, deadline, before_request):
        await before_request()
        self.calls.append(request)
        requested = self.clock()
        body = output(request, self.clock)
        if self.transform:
            body = self.transform(body)
        return Packet("ok", requested, self.clock(), 200, canonical(body), True)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_NEWS_ENABLED", "true")
    e = make_engine(f"sqlite:///{tmp_path / 'semantic.db'}")
    live.ensure_live_tables(e)
    physical.ensure(e)
    shadow.ensure(e)
    storage.ensure(e)
    shadow.activate(e, lambda: NOW - timedelta(hours=1))
    yield e
    service._HALTS.clear()
    service._INFLIGHT.clear()
    e.dispose()


def seed(engine, tickers=("NVDA",), *, headlines=None, empty=False, limited=False):
    data = snapshot()
    flow = data["flow_observations"]["NVDA"]
    price = data["news_research_context"]["prices"]["NVDA"]
    observation = data["news_observations"]["NVDA"]
    data["flow_observations"] = {t: dict(flow) for t in tickers}
    data["news_observations"] = {
        t: {**observation, "state": "checked_none" if empty else "checked_found"} for t in tickers
    }
    data["news_research_context"]["prices"].update({t: dict(price) for t in tickers})
    scan(engine, data=data)
    for ticker in tickers:
        rows = (
            []
            if empty
            else (
                headlines
                if headlines is not None
                else [
                    dict(
                        id="source-" + ticker,
                        headline=ticker + " raises guidance",
                        source="IssuerWire",
                        tickers=[ticker],
                        created_at=(NOW - timedelta(hours=1)).isoformat(),
                    )
                ]
            )
        )
        physical.append(
            engine,
            ticker,
            {"data": rows, **({"limited": True} if limited else {})},
            NOW - timedelta(seconds=30),
            NOW - timedelta(seconds=20),
            clock=lambda: NOW - timedelta(seconds=10),
            application_checked_at=NOW - timedelta(seconds=30),
        )
    assert bindings.discover(
        engine, NOW + timedelta(seconds=2), clock=lambda: NOW + timedelta(seconds=2)
    )["admitted"] == len(tickers)
    return shadow.list_events(engine, "candidate", NOW + timedelta(seconds=3), day=NOW.date())


def run(engine, client, clock):
    return asyncio.run(service.tick(engine, client, metadata(), clock(), clock=clock))


def test_real_source_receipt_through_analysis_reaches_read_only_verified_view(engine):
    parent = seed(engine)[0]
    clock = Clock()
    client = Client(clock)
    result = run(engine, client, clock)
    assert result["claimed"] == result["completed"] == 1 and len(client.calls) == 1
    writes = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip()
            .upper()
            .startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
            else None
        ),
    )
    report = view.build(engine, clock())
    assert report.status == "AVAILABLE" and report.completed == 1 and report.reserved == 1
    row = report.rows[0]
    assert row.candidate_key == parent.key and row.status == "completed"
    assert row.direction == "positive" and row.supports is True and row.eligible_before_entry
    assert (
        row.model == "test-grok"
        and row.input_tokens == row.output_tokens == 100
        and row.cost_usd is None
    )
    assert row.source_packet_key == parent.payload["news_packet_key"]
    assert row.available_at <= row.deadline and row.input_count == 1 and not row.checked_empty
    assert row.items[0].evidence[0].quote == client.calls[0].items[0].headline
    assert not writes and len(client.calls) == 1
    text = json.dumps(asdict(report), default=str)
    assert "raw_body" not in text and "compressed" not in text and "Bearer" not in text


def test_four_claims_commit_before_four_concurrent_provider_awaits(engine):
    seed(engine, ("AAPL", "AMD", "MSFT", "NVDA"))
    clock = Clock()

    class Concurrent(Client):
        async def analyze(self, request, *, deadline, before_request):
            assert storage.count(engine, "claim", clock(), day=NOW.date()) == 4
            await before_request()
            self.calls.append(request)
            if len(self.calls) == 4:
                ready.set()
            await asyncio.wait_for(ready.wait(), 2)
            return Packet("ok", clock(), clock(), 200, canonical(output(request, clock)), True)

    async def go():
        nonlocal ready
        ready = asyncio.Event()
        client = Concurrent(clock)
        a, b = await asyncio.gather(
            service.tick(engine, client, metadata(), clock(), clock=clock),
            service.tick(engine, client, metadata(), clock(), clock=clock),
        )
        assert len(client.calls) == 4 and a["completed"] + b["completed"] == 4
        assert len({r.request_id for r in client.calls}) == 4

    ready = None
    asyncio.run(go())
    assert view.build(engine, clock()).completed == 4


def test_restart_does_not_replay_any_claimed_request(engine):
    seed(engine)
    clock = Clock()
    assert run(engine, Client(clock), clock)["completed"] == 1
    second = Client(clock)
    assert run(engine, second, clock)["claimed"] == 0 and second.calls == []


def test_unconfirmed_orphan_claim_holds_day_then_halts_without_wire(engine):
    parent = seed(engine)[0]
    clock = Clock()
    binding = service.bind(engine, metadata(), clock(), clock)
    source = sources.source(engine, parent, clock())
    claim = service._claim(engine, source, binding, clock(), clock)
    assert claim is not None
    client = Client(clock)
    assert run(engine, client, clock)["status"] == "bootstrap_hold" and not client.calls
    clock.at = source.entry_target
    assert run(engine, client, clock)["status"] == "halted" and not client.calls
    assert view.build(engine, clock()).rows[0].status == "unknown"


def test_existing_future_ownership_after_clock_rollback_cannot_spend_again(engine):
    seed(engine)
    clock = Clock()
    storage.put(
        engine,
        key="future",
        kind="claim",
        at=clock() + timedelta(seconds=5),
        day=NOW.date(),
        unique_owner="other",
        payload={"deadline": (clock() + timedelta(seconds=30)).isoformat()},
        clock=lambda: clock() + timedelta(seconds=5),
    )
    client = Client(clock)
    assert run(engine, client, clock)["status"] == "halted" and not client.calls


def test_source_cutoff_cannot_be_repaired_with_late_semantic_interpretation(engine):
    seed(engine)
    clock = Clock(NOW + timedelta(seconds=58))
    client = Client(clock)
    assert run(engine, client, clock)["claimed"] == 0 and not client.calls
    row = view.build(engine, clock()).rows[0]
    assert row.status == "skipped" and row.supports is None and not row.eligible_before_entry


def test_packet_available_after_deadline_is_explicitly_late_not_scored(engine, monkeypatch):
    parent = seed(engine)[0]
    clock = Clock()
    original = storage._insert

    def delayed(e, model, values):
        result = original(e, model, values)
        if model is storage.SemanticEvent and values.get("kind") == "response":
            clock.at = parent.confirmed_at + timedelta(seconds=56)
        return result

    monkeypatch.setattr(storage, "_insert", delayed)
    result = run(engine, Client(clock), clock)
    assert result["completed"] == 0
    row = view.build(engine, clock()).rows[0]
    assert row.status == "late" and row.supports is None and not row.eligible_before_entry


@pytest.mark.parametrize(
    "status,httpcode", [("auth", 401), ("rate_limited", 429), ("unknown_provider_outcome", None)]
)
def test_provider_refusal_unknown_outcome_halts_and_is_never_retried(engine, status, httpcode):
    seed(engine)
    clock = Clock()

    class Refused(Client):
        async def analyze(self, request, *, deadline, before_request):
            await before_request()
            self.calls.append(request)
            return Packet(status, clock(), clock(), httpcode, None, True)

    client = Refused(clock)
    assert run(engine, client, clock)["status"] == "halted"
    assert run(engine, client, clock)["requests"] == 0 and len(client.calls) == 1
    assert view.build(engine, clock()).rows[0].supports is None


def test_changed_observed_model_binding_cannot_silently_mix_versions(engine):
    seed(engine)
    clock = Clock()
    assert service.bind(engine, metadata(), clock(), clock) is not None
    client = Client(clock)
    result = asyncio.run(
        service.tick(engine, client, metadata(model="different-model"), clock(), clock=clock)
    )
    assert result["status"] == "binding_unavailable" and client.calls == []
    assert service.halted(engine, NOW.date(), clock())


def test_verified_empty_packet_is_known_empty_without_model_or_budget(engine):
    parent = seed(engine, empty=True)[0]
    clock = Clock()
    client = Client(clock)
    run(engine, client, clock)
    row = view.build(engine, clock()).rows[0]
    assert row.candidate_key == parent.key and row.checked_empty
    assert row.status == "skipped" and row.reason == "known_empty" and row.supports is False
    assert row.eligible_before_entry and row.direction == "neutral"
    assert not client.calls and storage.count(engine, "claim", clock(), day=NOW.date()) == 0


def test_limited_empty_packet_is_unknown_not_a_no_news_veto_pass(engine):
    seed(engine, empty=True, limited=True)
    clock = Clock()
    client = Client(clock)
    run(engine, client, clock)
    row = view.build(engine, clock()).rows[0]
    assert not row.checked_empty and row.supports is None and not client.calls


def test_latest_three_source_bound_headlines_preserve_limited_coverage(engine):
    rows = [
        dict(
            id=str(i),
            headline=f"NVDA raises product outlook {i}",
            source="Wire",
            tickers=["NVDA"],
            created_at=(NOW - timedelta(hours=i + 1)).isoformat(),
        )
        for i in range(5)
    ]
    parent = seed(engine, headlines=rows)[0]
    source = sources.source(engine, parent, NOW + timedelta(seconds=3))
    assert len(source.request.items) == 3 and source.source_headline_count == 5
    assert [x.headline for x in source.request.items] == [r["headline"] for r in rows[:3]]
    clock = Clock()
    run(engine, Client(clock), clock)
    assert view.build(engine, clock()).rows[0].input_limited


def test_missing_schema_get_does_no_ddl_or_job(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    statements = []
    event.listen(e, "before_cursor_execute", lambda _c, _cur, sql, *_a: statements.append(sql))
    assert view.build(e, NOW).status == "STORAGE_UNAVAILABLE"
    assert not any(
        s.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE")) for s in statements
    )
    e.dispose()


def test_disabled_service_touches_neither_database_nor_provider(monkeypatch):
    monkeypatch.setenv("SEMANTIC_NEWS_ENABLED", "false")
    monkeypatch.setattr(service, "configured_engine", lambda *_: pytest.fail("disabled database"))
    asyncio.run(service.loop("unused", client_factory=lambda: pytest.fail("disabled provider")))


def test_own_worker_closes_client_and_uses_observed_metadata(engine, monkeypatch):
    seed(engine)
    clock = Clock()
    client = Client(clock)
    monkeypatch.setattr(service, "configured_engine", lambda _: engine)
    asyncio.run(service.loop("fixture", client_factory=lambda: client, clock=clock, max_cycles=1))
    assert client.closed and len(client.calls) == 1
    assert service.safe_status()["status"] == "active"


def test_raw_output_tampering_cannot_remain_a_completed_interpretation(engine):
    seed(engine)
    clock = Clock()
    run(engine, Client(clock), clock)
    with engine.begin() as conn:
        conn.execute(
            update(storage.SemanticEvent)
            .where(storage.SemanticEvent.kind == "response")
            .values(blob=b"corrupt")
        )
    clock.at = NOW + timedelta(minutes=2)
    row = view.build(engine, clock()).rows[0]
    assert row.status == "unknown" and row.supports is None


def test_transport_auth_bound_url_no_redirect_or_hidden_retry(engine):
    parent = seed(engine)[0]
    clock = Clock()
    request = sources.source(engine, parent, clock()).request
    calls = []
    reserved = []

    async def handler(req):
        assert reserved == [True]
        calls.append(req)
        assert req.headers["authorization"] == "Bearer local-service-fixture"
        assert json.loads(req.content)["request_id"] == request.request_id
        return httpx.Response(307, headers={"Location": "https://other.example/token-leak"})

    async def go():
        transport = Transport(
            "local-service-fixture", clock=clock, transport=httpx.MockTransport(handler)
        )

        async def before():
            reserved.append(True)

        try:
            packet = await transport.analyze(
                request, deadline=clock() + timedelta(seconds=40), before_request=before
            )
            assert packet.status == "provider_refused" and packet.http_status == 307
        finally:
            await transport.aclose()

    asyncio.run(go())
    assert len(calls) == 1 and str(calls[0].url) == config.settings().endpoint


def test_authenticated_metadata_read_does_not_post_or_trigger_inference():
    calls = []

    async def handler(req):
        calls.append(req)
        assert req.method == "GET" and str(req.url) == config.settings().endpoint + "/meta"
        return httpx.Response(200, json=metadata().model_dump())

    async def go():
        transport = Transport("local-meta-fixture", transport=httpx.MockTransport(handler))
        try:
            result = await transport.metadata()
            assert result.status == "configured" and result.value.model == "test-grok"
        finally:
            await transport.aclose()

    asyncio.run(go())
    assert len(calls) == 1
