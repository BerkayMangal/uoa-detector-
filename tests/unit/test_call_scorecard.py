"""Read-only call provenance, first-entry ownership and as-of context."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Table, event, select
from sqlalchemy.orm import Session
from webapp import main
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import make_engine
from webapp.calibration.storage import DailyBar
from webapp.calibration.storage import ensure as ensure_bars
from webapp.call_research import availability, context, coverage, scorecard
from webapp.call_research.storage import ensure as ensure_research
from webapp.live_alpha import store
from webapp.opening import storage

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_opening_desk import T0, persist, source


@pytest.fixture
def engine(tmp_path: Path):
    e = make_engine(f"sqlite:///{tmp_path / 'cards.db'}")
    store.ensure_live_tables(e)
    storage.ensure(e)
    ensure_bars(e)
    ensure_research(e)
    yield e
    e.dispose()


def test_update_cannot_inherit_the_first_entrys_measurements(engine) -> None:
    persist(engine, source())
    persist(engine, source(T0 + timedelta(minutes=1), "update"))
    with Session(engine) as session:
        primary = session.scalar(
            select(storage.OpeningPrimary).where(storage.OpeningPrimary.kind == "entry")
        )
        assert primary is not None
        records = session.scalars(
            select(storage.OpeningDecision).order_by(storage.OpeningDecision.published_at)
        ).all()
        update_key = records[-1].key
        key = primary.decision_key
        assert key is not None
        session.add(
            storage.OpeningOutcome(
                key="mark",
                primary_key=primary.key,
                horizon="+15 dk",
                observed_at=T0 + timedelta(minutes=16),
                payload_json=dumps(
                    dict(
                        status="complete",
                        observed_at=T0 + timedelta(minutes=16),
                        target_at=T0 + timedelta(minutes=15),
                        directional_underlying_pct=2.0,
                        directional_excess_pp=1.0,
                        spy_pct=1.0,
                    )
                ),
            )
        )
        session.add_all(
            [
                storage.OpeningObservation(
                    key="low",
                    ticker="NVDA",
                    source_at=T0 + timedelta(minutes=5),
                    available_at=T0 + timedelta(minutes=6),
                    value=99.0,
                    scan_id="scan",
                ),
                storage.OpeningObservation(
                    key="high",
                    ticker="NVDA",
                    source_at=T0 + timedelta(minutes=10),
                    available_at=T0 + timedelta(minutes=11),
                    value=101.0,
                    scan_id="scan",
                ),
            ]
        )
        session.commit()
    first = scorecard.get(engine, key, T0 + timedelta(minutes=20))
    assert first is not None
    update = scorecard.get(engine, update_key, T0 + timedelta(minutes=20))
    assert update is not None
    assert first.is_first_entry and first.source_valid and first.costs
    assert len(first.marks) == 1 and first.marks[0][1] is not None
    assert first.excursion is not None and first.excursion.adverse_pct == pytest.approx(-1)
    assert first.excursion.favorable_pct == pytest.approx(1)
    assert update.excursion is None
    assert not update.is_first_entry and update.canonical_key == first.key and update.marks == ()
    assert len(scorecard.listing(engine, T0 + timedelta(minutes=20))) == 1
    assert len(scorecard.listing(engine, T0 + timedelta(minutes=20), first_only=False)) == 2
    assert scorecard.listing(engine, T0 + timedelta(minutes=20), ticker="NFLX") == ()
    assert scorecard.get(engine, key, T0 - timedelta(seconds=1)) is None


def test_a_changed_source_snapshot_cannot_supply_invented_costs(engine) -> None:
    persist(engine, source())
    key = scorecard.listing(engine, T0)[0].key
    with Session(engine) as session:
        row = session.get(store.LiveScan, "scan")
        assert row is not None
        body = json.loads(row.snapshot_json)
        body["cards"][0]["options"] = [{"kind": "long_call", "entry_cost_usd": 9999}]
        row.snapshot_json = dumps(body)
        session.commit()
    result = scorecard.get(engine, key, T0)
    assert result is not None
    assert not result.source_valid and result.costs == ()


def test_private_pages_never_run_an_optimizer_or_write_records(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist(engine, source())
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: T0)
    from webapp.call_research import offline

    monkeypatch.setattr(offline, "run", lambda *_: pytest.fail("HTTP optimizer"))
    client = TestClient(main.app)
    headers = set_web_auth(monkeypatch)
    key = scorecard.listing(engine, T0)[0].key
    mutations = []

    def executed(_c, _cur, sql, _params, _ctx, _many):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            mutations.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    for path in ("/karne", "/karne/" + key, "/arastirma"):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=headers)
        assert (
            response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        )
        assert "noindex" in response.headers["x-robots-tag"]
    assert mutations == []
    assert client.get("/karne/not-found", headers=headers).status_code == 404


def test_asof_context_is_frozen_and_does_not_backfill_after_the_call(engine) -> None:
    at = datetime(2026, 11, 2, 15, tzinfo=UTC)
    # Isolated statement fixture; context does not need a tradable BUY or plan.
    with Session(engine) as session:
        session.add(
            storage.OpeningDecision(
                key="call",
                cycle_key="fixture",
                ticker="NFLX",
                day=at.date(),
                action="WAIT",
                published_at=at,
                payload_json="{}",
            )
        )
        session.commit()
    rows = []
    for name, closes in {
        "NFLX": (100, 102),
        "SPY": (100, 99),
        "QQQ": (100, 100),
        "XLC": (100, 101),
    }.items():
        for day, close in zip((date(2026, 10, 29), date(2026, 10, 30)), closes, strict=True):
            rows.append(
                dict(
                    key=digest((name, day)),
                    ticker=name,
                    day=day,
                    observed_at=datetime(2026, 10, 30, 21, 30, tzinfo=UTC),
                    payload_json=dumps(
                        dict(open=close, high=close + 1, low=close - 1, close=close)
                    ),
                )
            )
    append(engine, cast(Table, DailyBar.__table__), rows)
    availability.adopt(engine, coverage.targets(), at, lambda: at)
    points, _ = coverage.observed(engine, at, verified=True)
    assert context.capture(engine, points, at) == 1
    result = context.view(engine, "call", at)
    assert result is not None
    values = {p.ticker: p for p in result.points}
    assert result.sector_etf == "XLC" and values["SPY"].relative_pp == pytest.approx(3)
    assert values["QQQ"].relative_pp == pytest.approx(2) and values[
        "XLC"
    ].relative_pp == pytest.approx(1)
    assert context.capture(engine, (), at + timedelta(hours=1)) == 0
    assert context.view(engine, "call", at + timedelta(hours=1)) == result


def test_context_cannot_use_prices_first_received_after_the_call(engine) -> None:
    at = datetime(2026, 11, 2, 15, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            storage.OpeningDecision(
                key="late-prices",
                cycle_key="fixture",
                ticker="NFLX",
                day=at.date(),
                action="WAIT",
                published_at=at,
                payload_json="{}",
            )
        )
        session.commit()
    rows = [
        dict(
            key=digest(("NFLX", day)),
            ticker="NFLX",
            day=day,
            observed_at=at + timedelta(hours=1),
            payload_json=dumps(dict(open=100, high=103, low=99, close=102)),
        )
        for day in (date(2026, 10, 29), date(2026, 10, 30))
    ]
    append(engine, cast(Table, DailyBar.__table__), rows)
    now = at + timedelta(hours=2)
    availability.adopt(engine, coverage.targets(), now, lambda: now)
    points, _ = coverage.observed(engine, now, verified=True)
    context.capture(engine, points, now)
    result = context.view(engine, "late-prices", now)
    assert result is not None
    assert next(p for p in result.points if p.ticker == "NFLX").return_pct is None


def test_capture_membership_covers_the_entire_original_universe_with_bounded_new_context() -> None:
    from webapp.tournament.report import load as original

    targets = coverage.targets()
    assert len(targets) == len(set(targets)) == 82
    assert set(original()["tickers"]) <= set(targets)
    assert {"NFLX", "SPY", "QQQ", "XLC", "XLK", "XLRE"} <= set(targets)
    assert set(targets) <= set(coverage.daily_close_targets())
    assert "NFLX" in coverage.prospective_stocks()
    assert "QQQ" not in coverage.prospective_stocks() and "XLC" not in coverage.prospective_stocks()


def test_recorded_option_roundtrip_cost_is_per_structure_with_commission_once() -> None:
    rows = scorecard.costs(
        {
            "options": [
                dict(
                    kind="long_call",
                    readiness="READY",
                    lots=2,
                    entry_debit=4.1,
                    exit_credit=3.9,
                    multiplier=100,
                    commission_usd=2,
                    entry_cost_usd=410,
                    max_loss_usd=412,
                    roundtrip_cost_pct=5.3658536585,
                    breakeven=104.1,
                )
            ]
        }
    )
    assert len(rows) == 1 and rows[0].units == 2
    assert rows[0].roundtrip_per_unit == pytest.approx(22)
    assert rows[0].entry_per_unit == 410 and rows[0].planned_risk_per_unit == 412
    assert scorecard.number(True) is None and scorecard.number(float("inf")) is None


def test_future_target_cannot_supply_a_measured_return_before_it_happens(engine) -> None:
    persist(engine, source())
    key = scorecard.listing(engine, T0)[0].key
    with Session(engine) as session:
        primary = session.scalar(
            select(storage.OpeningPrimary).where(storage.OpeningPrimary.decision_key == key)
        )
        assert primary is not None
        session.add(
            storage.OpeningOutcome(
                key="future-mark",
                primary_key=primary.key,
                horizon="+15 dk",
                observed_at=T0 + timedelta(minutes=1),
                payload_json=dumps(
                    dict(
                        status="complete",
                        observed_at=T0 + timedelta(minutes=1),
                        target_at=T0 + timedelta(minutes=15),
                        directional_underlying_pct=999.0,
                        directional_excess_pp=999.0,
                        spy_pct=0.0,
                    )
                ),
            )
        )
        session.commit()
    result = scorecard.get(engine, key, T0 + timedelta(minutes=2))
    assert result is not None
    assert result.marks == (("+15 dk", None),)


def test_snapshot_completed_after_the_call_cannot_prove_original_costs(engine) -> None:
    persist(engine, source())
    key = scorecard.listing(engine, T0)[0].key
    with Session(engine) as session:
        row = session.get(store.LiveScan, "scan")
        assert row is not None
        row.finished_at = T0 + timedelta(minutes=1)
        session.commit()
    result = scorecard.get(engine, key, T0 + timedelta(minutes=2))
    assert result is not None
    assert not result.source_valid and result.costs == ()


def test_no_post_call_prices_do_not_become_zero_excursion(engine) -> None:
    persist(engine, source())
    key = scorecard.listing(engine, T0)[0].key
    result = scorecard.get(engine, key, T0)
    assert result is not None and result.excursion is not None
    assert result.excursion.count == 0
    assert result.excursion.favorable_pct is None and result.excursion.adverse_pct is None
