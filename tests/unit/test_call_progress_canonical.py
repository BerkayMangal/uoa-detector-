"""Real outcome writer IDs must agree in opening, list and detail projections."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from webapp import main
from webapp.alpha_lab.db import dumps
from webapp.board.db import make_engine, session_factory
from webapp.calibration.storage import DailyBar
from webapp.calibration.storage import ensure as ensure_daily
from webapp.call_research import progress, scorecard
from webapp.live_alpha import store
from webapp.opening import measurement, outcomes, storage
from webapp.opening import view as opening_view

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_opening_desk import T0, persist, source


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    result = make_engine(f"sqlite:///{tmp_path / 'canonical-progress.db'}")
    store.ensure_live_tables(result)
    storage.ensure(result)
    ensure_daily(result)
    return result


def record(engine: Engine, *, spy: bool = True) -> str:
    scan = source()
    if not spy:
        scan.snapshot["price_observations"].pop("SPY")
    persist(engine, scan)
    return scorecard.listing(engine, T0)[0].key


def minute(engine: Engine, minutes: int, *, own: float | None = 102, spy: float | None = 505) -> None:
    due = T0+timedelta(minutes=minutes)
    with session_factory(engine)() as session:
        for ticker, value in (("NVDA", own), ("SPY", spy)):
            if value is not None:
                session.add(storage.OpeningObservation(key=f"{ticker}-m{minutes}", ticker=ticker,
                    source_at=due, available_at=due+timedelta(seconds=1), value=value, scan_id="price-observation"))
        session.commit()


def readers(engine: Engine, now: datetime) -> tuple[progress.Row, tuple[progress.Metric, ...], measurement.Summary]:
    briefs = scorecard.listing(engine, now)
    listed = progress.listing(engine, briefs, now)
    assert len(listed.rows) == 1 and listed.rows[0].source_valid
    card = scorecard.get(engine, briefs[0].key, now)
    assert card is not None and card.source_valid
    return listed.rows[0], progress.detail(card, now), measurement.build(opening_view.build(engine, now).results, now)


@pytest.mark.parametrize("minutes", [15, 60])
def test_actual_minute_writer_has_identical_opening_list_detail_results(engine: Engine, minutes: int) -> None:
    record(engine)
    minute(engine, minutes)
    now = T0+timedelta(minutes=minutes, seconds=1)
    assert outcomes.measure(engine, now) >= 1
    with session_factory(engine)() as session:
        assert session.scalar(select(storage.OpeningOutcome).where(storage.OpeningOutcome.horizon == f"m{minutes}")) is not None
    row, detail, summary = readers(engine, now)
    listed = next(m for m in row.metrics if m.label == f"+{minutes} dk")
    shown = next(m for m in detail if m.label == listed.label)
    original = next(h for h in summary.horizons if h.label == listed.label)
    assert listed.status == shown.status == "observed"
    assert original.observed == original.matched == 1
    assert listed.value_pct == shown.value_pct == original.mean_pct == pytest.approx(2)
    assert listed.excess_pp == shown.excess_pp == original.mean_excess_pp == pytest.approx(1)


@pytest.mark.parametrize("own,spy,state", [(None, None, "missing"), (102, None, "partial")])
def test_real_missing_and_partial_outcomes_are_not_reported_awaiting(engine: Engine, own: float | None,
                                                                  spy: float | None, state: str) -> None:
    record(engine)
    minute(engine, 15, own=own, spy=spy)
    now = T0+timedelta(minutes=20, seconds=1)
    assert outcomes.measure(engine, now) == 1
    row, detail, summary = readers(engine, now)
    assert row.metrics[0].status == detail[0].status == state
    assert summary.horizons[0].missing == int(state == "missing")
    assert summary.horizons[0].partial == int(state == "partial")
    assert row.metrics[0].value_pct == detail[0].value_pct == summary.horizons[0].mean_pct
    assert row.metrics[0].excess_pp is None


@pytest.mark.parametrize("count,close_iso", [(0, "2026-10-28T20:00:00+00:00"), (1, "2026-10-29T20:00:00+00:00"),
    (3, "2026-11-02T21:00:00+00:00"), (5, "2026-11-04T21:00:00+00:00")])
def test_real_daily_close_ids_agree_across_all_readers(engine: Engine, count: int, close_iso: str) -> None:
    record(engine)
    close = datetime.fromisoformat(close_iso)
    now = close+timedelta(minutes=1)
    with session_factory(engine)() as session:
        for ticker, value in (("NVDA", 98), ("SPY", 495)):
            session.add(DailyBar(key=f"close-{ticker}", ticker=ticker, day=close.date(), observed_at=now,
                                 payload_json=dumps({"close": value})))
        session.commit()
    outcomes.measure(engine, now)
    row, detail, summary = readers(engine, now)
    label = "Bugün kapanış" if count == 0 else f"+{count} seans kapanış"
    listed = next(m for m in row.metrics if m.label == label)
    shown = next(m for m in detail if m.label == listed.label)
    original = next(h for h in summary.horizons if h.label == listed.label)
    assert listed.status == shown.status == "observed"
    assert listed.value_pct == shown.value_pct == original.mean_pct == pytest.approx(-2)
    assert listed.excess_pp == shown.excess_pp == original.mean_excess_pp == pytest.approx(-1)


def test_future_writer_receipt_stays_awaiting_until_available(engine: Engine) -> None:
    record(engine)
    minute(engine, 60)
    outcomes.measure(engine, T0+timedelta(minutes=61))
    row, detail, summary = readers(engine, T0+timedelta(minutes=60))
    assert row.metrics[1].status == detail[1].status == "awaiting"
    assert summary.horizons[1].awaiting == 1
    row, detail, _ = readers(engine, T0+timedelta(minutes=61))
    assert row.metrics[1].status == detail[1].status == "observed"


@pytest.mark.parametrize("alias", ["m60", "+60 dk"])
def test_duplicate_and_alias_collisions_are_invalid_not_last_wins(engine: Engine, alias: str) -> None:
    record(engine)
    minute(engine, 60)
    now = T0+timedelta(minutes=61)
    outcomes.measure(engine, now)
    with session_factory(engine)() as session:
        original = session.scalar(select(storage.OpeningOutcome).where(storage.OpeningOutcome.horizon == "m60"))
        assert original is not None
        body = json.loads(original.payload_json)
        body["directional_underlying_pct"] = 90.0
        session.add(storage.OpeningOutcome(key="duplicate", primary_key=original.primary_key, horizon=alias,
                    observed_at=original.observed_at, payload_json=dumps(body)))
        session.commit()
    row, detail, _ = readers(engine, now)
    assert row.metrics[1].status == detail[1].status == "invalid"
    assert row.metrics[1].value_pct is None and detail[1].value_pct is None


def test_unknown_ids_cannot_acquire_results_but_legacy_labels_still_work(engine: Engine) -> None:
    record(engine)
    minute(engine, 60)
    now = T0+timedelta(minutes=61)
    outcomes.measure(engine, now)
    original = opening_view.build(engine, now).results[0]
    body = dict(original.marks)["+60 dk"]
    unknown = replace(original, marks=(("m600", body),))
    legacy = replace(original, marks=(("+60 dk", body),))
    assert progress.metrics(unknown, now)[1].status == "awaiting"
    assert progress.metrics(legacy, now)[1].status == "observed"


@pytest.mark.parametrize("damage", ["source", "profile", "target"])
def test_canonical_marks_keep_existing_provenance_and_target_guards(engine: Engine, damage: str) -> None:
    key = record(engine)
    minute(engine, 60)
    now = T0+timedelta(minutes=61)
    outcomes.measure(engine, now)
    with session_factory(engine)() as session:
        if damage == "source":
            scan = session.scalar(select(store.LiveScan))
            assert scan is not None
            body = json.loads(scan.snapshot_json)
            body["cards"][0]["plain_reason"] = "Tampered source"
            scan.snapshot_json = dumps(body)
        elif damage == "profile":
            cycle = session.scalar(select(storage.OpeningCycle))
            assert cycle is not None
            body = json.loads(cycle.payload_json)
            body["profile_hash"] = "wrong"
            cycle.payload_json = dumps(body)
        else:
            outcome = session.scalar(select(storage.OpeningOutcome).where(storage.OpeningOutcome.horizon == "m60"))
            assert outcome is not None
            body = json.loads(outcome.payload_json)
            body["target_at"] = (T0+timedelta(minutes=59)).isoformat()
            outcome.payload_json = dumps(body)
        session.commit()
    row = progress.listing(engine, scorecard.listing(engine, now), now).rows[0]
    card = scorecard.get(engine, key, now)
    assert card is not None
    detail = progress.detail(card, now)
    state = "invalid" if damage == "target" else "unverified"
    assert row.metrics[1].status == detail[1].status == state
    assert row.metrics[1].value_pct is None


def test_updated_call_cannot_inherit_canonical_first_call_returns(engine: Engine) -> None:
    record(engine)
    persist(engine, source(T0+timedelta(minutes=1), "update"))
    minute(engine, 60)
    now = T0+timedelta(minutes=61)
    outcomes.measure(engine, now)
    listed = progress.listing(engine, scorecard.listing(engine, now, first_only=False), now)
    original = next(r for r in listed.rows if r.first)
    updated = next(r for r in listed.rows if not r.first)
    card = scorecard.get(engine, updated.brief.key, now)
    assert card is not None
    assert original.metrics[1].status == "observed" and listed.observed == listed.matched == 1
    assert updated.metrics[1].status == progress.detail(card, now)[1].status == "update"
    assert updated.metrics[1].value_pct is None


def test_canonical_results_private_gets_are_read_only(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    key = record(engine)
    minute(engine, 60)
    now = T0+timedelta(minutes=61)
    outcomes.measure(engine, now)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: now)
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("GET must not request vendor data")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    writes: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, parameters, context, executemany:
                 writes.append(sql) if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) else None)
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    for path in ("/karne", f"/karne/{key}"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        assert "Ölçüldü" in response.text
    assert writes == []
