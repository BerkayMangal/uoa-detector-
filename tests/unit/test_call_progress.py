"""No invented measurements, inherited returns, hidden missingness or page writes."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from webapp import main
from webapp.alpha_lab.db import dumps
from webapp.board.db import make_engine
from webapp.call_research import progress, repair_operator, scorecard
from webapp.live_alpha import store
from webapp.opening import storage

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_opening_desk import T0, persist, source


@pytest.fixture
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'progress.db'}")
    store.ensure_live_tables(e)
    storage.ensure(e)
    persist(e, source())
    return e


def mark(engine, minutes=60, status="complete", wrong_target=False):
    due = T0 + timedelta(minutes=minutes)
    with Session(engine) as s:
        primary = s.scalar(
            select(storage.OpeningPrimary).where(storage.OpeningPrimary.kind == "entry")
        )
        s.add(
            storage.OpeningOutcome(
                key=f"mark-{minutes}",
                primary_key=primary.key,
                horizon=f"+{minutes} dk",
                observed_at=due + timedelta(seconds=1),
                payload_json=dumps(
                    dict(
                        status=status,
                        observed_at=due + timedelta(seconds=1),
                        target_at=due - timedelta(minutes=1) if wrong_target else due,
                        directional_underlying_pct=2.0,
                        directional_excess_pp=1.5 if status == "complete" else None,
                        spy_pct=0.5 if status == "complete" else None,
                    )
                ),
            )
        )
        s.commit()


def view(engine, at=T0 + timedelta(minutes=61), updates=False):
    return progress.listing(engine, scorecard.listing(engine, at, first_only=not updates), at)


def test_not_due_and_due_without_a_price_record_are_different(engine):
    early = view(engine, T0 + timedelta(minutes=10))
    late = view(engine)
    assert early.rows[0].intraday[0].status == "pending"
    assert late.rows[0].intraday[0].status == "awaiting"
    assert early.observed == late.observed == 0
    assert late.rows[0].intraday[0].value_pct is None


def test_exact_first_entry_marks_are_visible_but_update_cannot_inherit(engine):
    mark(engine, 15)
    mark(engine, 60)
    persist(engine, source(T0 + timedelta(minutes=1), "update"))
    result = view(engine, updates=True)
    assert result.first_entries == 1 and result.observed == result.matched == 1
    first = next(r for r in result.rows if r.first)
    update = next(r for r in result.rows if not r.first)
    assert first.intraday[1].value_pct == 2 and first.intraday[1].excess_pp == 1.5
    assert update.intraday[1].status == "update" and update.intraday[1].value_pct is None
    assert update.canonical_key == first.brief.key


def test_mismatched_target_is_invalid_in_list_and_detail(engine):
    mark(engine, wrong_target=True)
    result = view(engine)
    assert result.rows[0].intraday[1].status == "invalid" and result.observed == 0
    card = scorecard.get(engine, result.rows[0].brief.key, T0 + timedelta(minutes=61))
    assert progress.detail(card, T0 + timedelta(minutes=61))[1].status == "invalid"


def test_missing_benchmark_keeps_underlying_separate(engine):
    mark(engine, status="missing_reference")
    result = view(engine)
    assert result.observed == 1 and result.matched == 0
    m = result.rows[0].intraday[1]
    assert m.status == "partial" and m.value_pct == 2 and m.excess_pp is None


def test_future_outcome_receipt_is_not_available_early(engine):
    mark(engine)
    result = view(engine, T0 + timedelta(minutes=60))
    assert result.observed == 0 and result.rows[0].intraday[1].status == "awaiting"


@pytest.mark.parametrize("damage", ["source", "profile"])
def test_changed_provenance_never_supplies_a_measured_return(engine, damage):
    mark(engine)
    with Session(engine) as s:
        row = (
            s.scalar(select(store.LiveScan))
            if damage == "source"
            else s.scalar(select(storage.OpeningCycle))
        )
        body = json.loads(row.snapshot_json if damage == "source" else row.payload_json)
        if damage == "source":
            body["cards"][0]["stock_plan"]["entry_ref"] = 999
            row.snapshot_json = dumps(body)
        else:
            body["profile_hash"] = "wrong"
            row.payload_json = dumps(body)
        s.commit()
    result = view(engine)
    assert result.observed == result.matched == 0
    assert all(m.status == "unverified" for m in result.rows[0].metrics)


def test_duplicates_do_not_inflate_scope_and_queries_are_bounded(engine):
    at = T0 + timedelta(minutes=61)
    brief = scorecard.listing(engine, at)[0]
    queries = []

    def executed(_c, _cur, sql, _params, _ctx, _many):
        if sql.lstrip().upper().startswith("SELECT"):
            queries.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    result = progress.listing(engine, (brief,) * 50, at)
    assert result.first_entries == 1 and len(result.rows) == 1
    assert len(queries) <= 5


def test_private_gets_never_repair_or_mutate(engine, monkeypatch):
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: T0 + timedelta(minutes=61))

    async def forbidden(*args):
        pytest.fail("operator called by HTTP")

    monkeypatch.setattr(repair_operator, "once", forbidden)
    writes = []

    def executed(_c, _cur, sql, _params, _ctx, _many):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")):
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    key = scorecard.listing(engine, T0)[0].key
    for path in ("/karne", "/karne/" + key, "/api/arastirma/veri-tamamlama"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "noindex" in response.headers["x-robots-tag"]
    assert writes == []
