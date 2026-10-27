"""Recorded scope, funding, manual intent and alert immutability."""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp import main
from webapp.board.atm import AlfaAtm
from webapp.desk import capture, db, exit_alerts, positions
from webapp.journal import TradeRow
from webapp.live_alpha import alerts, store

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_live_alpha_job import T0, _seed


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    e = _seed(f"sqlite:///{tmp_path / 'desk.db'}")
    db.ensure(e)
    cast(Table, TradeRow.__table__).create(e, checkfirst=True)
    original = dict(capture.STATE)
    yield e
    capture.STATE.clear()
    capture.STATE.update(original)
    e.dispose()


def paper(e: Engine, identity: str = "p", state: str = "PAPER_OPEN", option: bool = False) -> None:
    plan = {"policy_version": "live_alpha_v1", "stock_plan": {"stop": 97, "target": 106},
            "structure": {"multiplier": 100, "commission_usd": 1.3}}
    with Session(e) as session:
        session.add(store.LivePaper(paper_id=identity, opportunity_id=f"2026-09-24:NVDA:{identity}", rec_id="rec", ticker="NVDA", direction="up",
            instrument="long_call" if option else "stock", plan_json=json.dumps(plan), state=state,
            created_at=T0 - timedelta(minutes=5), entry_at=T0 if state != "PAPER_PENDING" else None,
            entry_price=2 if option else 100.05 if state != "PAPER_PENDING" else None,
            quantity=2 if option else 10 if state != "PAPER_PENDING" else None,
            last_mark=1.8 if option else 100, last_mark_at=T0))
        session.commit()


def manual(e: Engine, identity: str = "m", direction: str = "bullish", instrument: str = "shares") -> None:
    with Session(e) as session:
        session.add(TradeRow(id=identity, created_at=T0 - timedelta(minutes=10), entry_ts=T0 - timedelta(minutes=10),
            ticker="NVDA", direction=direction, instrument=instrument, contracts=10,
            entry_price=100 if instrument == "shares" else 2, entry_underlying_px=100, entry_spy_px=500,
            thesis="owner record", status="open"))
        session.commit()


def test_pending_is_unfunded_and_manual_totals_remain_separate(engine: Engine) -> None:
    paper(engine)
    paper(engine, "pending", "PAPER_PENDING")
    manual(engine)
    view = positions.build(engine, T0)
    assert len(view.paper.rows) == 2 and view.paper.allocation == pytest.approx(1000.5)
    assert view.paper.risk == pytest.approx(30.5)
    assert next(p for p in view.paper.rows if p.id == "p").pnl == pytest.approx(-1)
    assert view.manual.allocation == 1000 and view.manual.risk is None and view.manual.unknown_risk == 1
    assert view.manual.rows[0].reason == "Dayanak stop/hedef planı eklenmemiş."


def test_option_risk_and_closable_mark_include_whole_quantities_and_model_fees(engine: Engine) -> None:
    paper(engine, option=True)
    row = positions.build(engine, T0).paper.rows[0]
    assert row.allocation == 400 and row.risk == pytest.approx(402.6)
    assert row.pnl == pytest.approx(-42.6)
    assert positions.build(engine, T0 + timedelta(seconds=901)).paper.rows[0].pnl is None
    with engine.begin() as c:
        c.execute(update(store.LivePaper).values(last_mark_at=T0 + timedelta(seconds=2)))
    assert positions.build(engine, T0).paper.rows[0].pnl is None


@pytest.mark.parametrize(("instrument", "direction"), [("shares", "bearish"), ("unknown", "bullish"), ("shares", "unknown")])
def test_unsupported_manual_side_or_multiplier_does_not_invent_risk(engine: Engine, instrument: str, direction: str) -> None:
    manual(engine, instrument=instrument, direction=direction)
    row = positions.build(engine, T0).manual.rows[0]
    assert row.risk is None and row.pnl is None and row.action == "DATA_WAIT"


def test_manual_plan_post_preserves_revisions_and_never_closes_trade(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    manual(engine)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: T0)
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.post("/pozisyonlar/plan", data={"trade_id": "m", "stop": 98, "target": 105}).status_code == 401
    assert client.post("/pozisyonlar/plan", headers=headers | {"Origin": "https://foreign.example"}, data={"trade_id": "m", "stop": 98}).status_code == 403
    assert client.post("/pozisyonlar/plan", headers=headers, data={"trade_id": "m", "stop": 98, "target": 105}, follow_redirects=False).status_code == 303
    monkeypatch.setattr(main, "_now", lambda: T0 + timedelta(seconds=1))
    assert client.post("/pozisyonlar/plan", headers=headers, data={"trade_id": "m", "stop": 99, "target": 106}, follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert len(list(s.scalars(select(db.ManualPlan)))) == 2
        trade = s.get(TradeRow, "m")
        assert trade is not None and trade.status == "open"
    page = client.get("/pozisyonlar", headers=headers)
    assert page.status_code == 200 and "İşlem günlüğü" in page.text
    assert page.headers["cache-control"] == "private, no-store" and "noindex" in page.headers["x-robots-tag"]
    row = positions.build(engine, T0 + timedelta(seconds=1)).manual.rows[0]
    assert row.stop == 99 and row.target == 106 and row.risk == 10


@pytest.mark.parametrize("data", [
    {"stop": "nan"}, {"stop": "inf"}, {"stop": "0"}, {"stop": "-1"},
    {"stop": "98", "target": "nan"}, {"stop": "98", "target": "97"},
])
def test_invalid_manual_plan_numbers_or_order_are_rejected(engine: Engine, monkeypatch: pytest.MonkeyPatch, data: dict[str, str]) -> None:
    manual(engine)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    response = TestClient(main.app).post("/pozisyonlar/plan", headers=set_web_auth(monkeypatch), data={"trade_id": "m", **data})
    assert response.status_code == 400
    with Session(engine) as s:
        assert list(s.scalars(select(db.ManualPlan))) == []


def test_manual_crossing_is_a_recorded_review_not_a_sale_and_deduplicates(engine: Engine) -> None:
    manual(engine)
    db.append(engine, cast(Table, db.ManualPlan.__table__), [dict(key="plan", trade_id="m", created_at=T0, stop=98, target=106)])
    with engine.begin() as c:
        c.execute(update(AlfaAtm).where(AlfaAtm.ticker == "NVDA").values(stock_price=96, fetched_at=T0))
    scan = store.ScanRow("scan", T0, T0, "LIVE", "ok", "run", {})
    capture.observe(engine, scan, T0)
    first = exit_alerts.recent(engine, T0)
    assert len(first) == 1 and "MANUEL" in first[0].label and "Gerçek satış kaydedilmedi" in first[0].message
    capture.observe(engine, store.ScanRow("scan2", T0, T0, "LIVE", "ok", "run", {}), T0 + timedelta(seconds=1))
    assert exit_alerts.recent(engine, T0 + timedelta(seconds=1))[0].key == first[0].key
    with Session(engine) as s:
        trade = s.get(TradeRow, "m")
        assert trade is not None and trade.status == "open"
    with engine.begin() as c:
        c.execute(update(TradeRow).values(status="closed"))
    assert exit_alerts.recent(engine, T0 + timedelta(seconds=2)) == ()


def test_paper_exit_notice_can_be_delivered_after_session_close(engine: Engine) -> None:
    paper(engine, state="EXIT_SIGNALLED")
    now = T0.replace(hour=20, minute=5)
    store.append_event(engine, at=now, opportunity_id="2026-09-24:NVDA:p", kind="exit_signalled", actor="job", policy_version="live_alpha_v1", detail={"paper_id": "p", "reason": "stop"})
    feed = alerts.feed(engine, now)
    assert feed.status == "current" and len(feed.signals) == 1
    assert feed.signals[0].event_type == "exit" and "fiyatı bekleniyor" in feed.signals[0].message
    assert not alerts.feed(engine, now + timedelta(seconds=1801)).signals


def test_wrong_session_or_stale_underlying_never_triggers_a_manual_review(engine: Engine) -> None:
    manual(engine)
    db.append(engine, cast(Table, db.ManualPlan.__table__), [dict(key="plan", trade_id="m", created_at=T0, stop=98, target=106)])
    with engine.begin() as c:
        c.execute(update(AlfaAtm).values(stock_price=90, fetched_at=T0, trade_date=T0.date() - timedelta(days=1)))
    row = positions.build(engine, T0).manual.rows[0]
    assert row.spot is None and row.action == "DATA_WAIT"


@pytest.mark.asyncio
async def test_failed_desk_hook_does_not_raise_to_the_live_loop(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(capture, "observe", fail)
    await capture.run(engine, store.ScanRow("s", T0, T0, "LIVE", "ok", "r", {}), T0)
    assert capture.STATE["status"] == "failed" and capture.STATE["error"] == "RuntimeError"
