"""Read actual original publications/outcomes; never optimize or repair them."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from webapp import call_review, main
from webapp.board.db import make_engine
from webapp.call_research import progress
from webapp.live_alpha import store
from webapp.opening import outcomes

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_call_progress_canonical import engine as engine
from tests.unit.test_call_progress_canonical import minute, record
from tests.unit.test_opening_desk import T0, persist, source


@pytest.mark.parametrize("spy", [True, False])
def test_actual_writer_is_reviewed_with_coverage_and_original_first_identity(engine, spy):
    key = record(engine)
    minute(engine, 60, own=98, spy=505 if spy else None)
    now = T0 + timedelta(minutes=66)
    outcomes.measure(engine, now)
    report = call_review.build(engine, now)
    metric = next(m for m in report.metrics if m.horizon == "+60 dk")
    assert report.total == report.verified == len(report.cases) == 1
    assert report.cases[0].key == key
    assert metric.observed == metric.negative == 1 and metric.positive == metric.flat == 0
    assert metric.mean_pct == pytest.approx(-2)
    assert metric.paired == int(spy)
    assert (
        metric.matched_excess_pp == pytest.approx(-3) if spy else metric.matched_excess_pp is None
    )
    assert report.inference == "NOT_PERFORMED" and report.winner is None
    assert {g.axis for g in report.groups} == {"role", "instrument", "day"}
    assert all(sum(m.observed for m in g.metrics) == 1 for g in report.groups)


def test_pending_and_missing_never_become_losses_and_updates_never_double_count(engine):
    record(engine)
    persist(engine, source(T0 + timedelta(minutes=1), "later"))
    report = call_review.build(engine, T0 + timedelta(minutes=2))
    assert len(report.cases) == report.total == 1
    assert all(
        m.observed == m.negative == m.paired == 0 and m.mean_pct is None for m in report.metrics
    )
    assert report.metrics[0].states == {"pending": 1}


def test_corrupt_original_source_suppresses_numeric_results(engine):
    record(engine)
    minute(engine, 60)
    now = T0 + timedelta(minutes=66)
    outcomes.measure(engine, now)
    with engine.begin() as c:
        c.execute(update(store.LiveScan).values(snapshot_json="{}"))
    report = call_review.build(engine, now)
    assert report.verified == 0 and report.cases[0].role == "unknown"
    assert all(
        m.observed == 0 and m.mean_pct is None and m.states == {"unverified": 1}
        for m in report.metrics
    )


def test_benchmark_uses_only_matched_records_and_repeated_days_are_not_independent_sessions():
    at = T0
    base = call_review.Case(
        "a",
        "A",
        at.date(),
        at,
        "unknown",
        "stock",
        True,
        (),
        (progress.Metric("c0", "observed", -2, -1),),
    )
    partial = replace(
        base, key="b", ticker="B", metrics=(progress.Metric("c0", "partial", 10, None),)
    )
    invalid = replace(
        base, key="c", verified=False, metrics=(progress.Metric("c0", "observed", 900, 900),)
    )
    m = call_review.summarize((base, partial, invalid))[0]
    assert m.calls == 3 and m.observed == 2 and m.paired == m.measured_sessions == 1
    assert (
        m.mean_pct == 4
        and m.matched_own_pct == -2
        and m.matched_spy_pct == -1
        and m.matched_excess_pp == -1
    )


def test_real_listing_bound_is_disclosed_without_claiming_the_full_population(engine, monkeypatch):
    from webapp.call_research import scorecard

    record(engine)
    later = T0 + timedelta(days=1)
    persist(engine, source(later, "next-session"))
    limited = call_review.settings().model_copy(update={"display_limit": 1})
    monkeypatch.setattr(call_review, "settings", lambda: limited)
    monkeypatch.setattr(scorecard, "settings", lambda: limited)
    report = call_review.build(engine, later + timedelta(minutes=1))
    assert report.total == 2 and len(report.cases) == report.limit == 1
    assert report.limited and report.status == "LIMITED"
    assert all(m.calls == 1 for m in report.metrics)


def test_original_features_are_not_replaced_by_later_source_updates(engine):
    from tests.unit.test_signal_explanation import T, publish
    from tests.unit.test_signal_explanation import source as original_source

    publish(engine, original_source())
    publish(engine, original_source(at=T + timedelta(minutes=1), identity="later",
                                  path="P2_market_relative_flow", price_changes={"spot": 150}))
    report = call_review.build(engine, T + timedelta(minutes=2))
    assert len(report.cases) == 1
    case = report.cases[0]
    assert case.role == "uw_news_selected" and case.verified
    assert case.price_move_atr == .5
    assert case.dominant_flow_pct == pytest.approx(600000 / 700000 * 100)
    assert case.relative_pp == pytest.approx((100 / 99 - 1) * 100 + .5)


@pytest.mark.parametrize("path", ["/inceleme", "/api/arastirma/cagri-inceleme"])
@pytest.mark.parametrize("initialized", [False, True])
def test_private_review_get_is_select_only_offline_and_honest_about_missing_storage(
    engine, tmp_path, monkeypatch, path, initialized
):
    e = engine if initialized else make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    monkeypatch.setattr(main, "_now", lambda: T0)

    def forbidden(*_a, **_k):
        pytest.fail("Vendor I/O on stored review GET")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    statements = []
    event.listen(e, "before_cursor_execute", lambda _c, _cur, sql, *_a: statements.append(sql))
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    assert client.get(path).status_code == 401 and statements == []
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and "noindex" in response.headers["x-robots-tag"]
    assert response.headers["cache-control"] == "private, no-store"
    assert not any(
        s.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER"))
        for s in statements
    )
    if path.startswith("/api"):
        b = response.json()
        assert b["winner"] is None and b["inference"] == "NOT_PERFORMED"
        assert b["status"] == ("EMPTY" if initialized else "STORAGE_UNAVAILABLE")
    if not initialized:
        e.dispose()
