"""Causal family comparison, executable accounting and frozen future observations."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.calibration.model import Bar
from webapp.calibration.settings import calendar
from webapp.calibration.storage import capture_payload
from webapp.tournament import context, engine, model, monitor, report, research, storage
from webapp.tournament.settings import settings

from uoa_detector.live_alpha.calendar import next_session_after, previous_session_before

C = model.Config("relative_recovery", 5, 0, 0, "any", 3, "equal_cash", 4)


def days(start: date, n: int) -> list[date]:
    out = [start]
    for _ in range(n - 1):
        nxt = next_session_after(out[-1], calendar())
        assert nxt is not None
        out.append(nxt)
    return out


def panel() -> tuple[Bar, ...]:
    ds = days(date(2026, 1, 5), 45)
    return tuple(
        Bar(
            t,
            d,
            100 + i if t == "SPY" else 100,
            102 + i if t == "SPY" else 102,
            98 + i if t == "SPY" else 98,
            101 + i if t == "SPY" else 101,
            1000,
        )
        for t in ("AAA", "BBB", "SPY")
        for i, d in enumerate(ds)
    )


def test_grid_has_exactly_7776_unique_trials_and_all_six_families() -> None:
    g = model.grid()
    assert len(g) == len({c.id for c in g}) == 7776
    assert {c.family for c in g} == set(settings().thresholds)
    assert {c.horizon for c in g} == {1, 3, 5}


def test_features_never_change_when_future_prices_are_added() -> None:
    source = panel()
    end = sorted({b.day for b in source})[30]
    prefix = tuple(b for b in source if b.day <= end)
    a, b = model.prepare(prefix), model.prepare(source)
    for key in a.scores:
        np.testing.assert_allclose(a.scores[key], b.scores[key][: len(a.days)], equal_nan=True)
        np.testing.assert_array_equal(a.conditions[key], b.conditions[key][: len(a.days)])


def test_missing_exchange_session_breaks_feature_window() -> None:
    source = panel()
    d = sorted({b.day for b in source})[-5]
    p = model.prepare(tuple(b for b in source if not (b.ticker == "AAA" and b.day == d)))
    assert not model.mask(p, C)[-1, p.tickers.index("AAA")]
    assert model.mask(p, C)[-1, p.tickers.index("BBB")]


def test_recovery_and_momentum_are_opposite_relative_conditions() -> None:
    p = model.prepare(panel())
    j = p.tickers.index("AAA")
    assert model.mask(p, C)[-1, j]
    assert not model.mask(p, replace(C, family="relative_momentum", threshold=1))[-1, j]
    assert not model.mask(p, replace(C, volume=1.5))[-1, j]


def test_breakout_uses_prior_high_and_compression_does_not_use_current_range() -> None:
    source = panel()
    end = max(b.day for b in source)
    changed = tuple(
        replace(b, open=104, high=107, low=103, close=105)
        if b.ticker == "AAA" and b.day == end
        else b
        for b in source
    )
    p = model.prepare(changed)
    j = p.tickers.index("AAA")
    assert model.mask(p, replace(C, family="range_breakout", threshold=0.25))[-1, j]
    assert model.mask(p, replace(C, family="volatility_compression", threshold=1))[-1, j]
    huge = tuple(
        replace(b, high=10000, low=1) if b.ticker == "AAA" and b.day == end else b for b in changed
    )
    q = model.prepare(huge)
    assert (
        p.scores["volatility_compression", 5][-1, j] == q.scores["volatility_compression", 5][-1, j]
    )


def selected(p: model.Prepared, events: list[tuple[int, str]]) -> model.BoolArray:
    mask = np.zeros(p.open.shape, dtype=np.bool_)
    for i, t in events:
        mask[i, p.tickers.index(t)] = True
    return mask


def test_cash_spend_precedes_same_day_exit_and_reference_is_matched_not_market_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine,
        "settings",
        lambda: settings().model_copy(update={"capital_usd": 1000.0, "position_usd": 1000.0}),
    )
    p = model.prepare(panel())
    sim = engine.simulate(
        p,
        replace(C, horizon=2),
        selected(p, [(23, "AAA"), (24, "BBB")]),
        p.days[23],
        p.days[28],
        5,
        ledger=True,
    )
    assert sim.metrics.closed == 1 and sim.trades[0].quantity == 9
    assert sim.trades[0].debit == pytest.approx(900.45)
    assert sim.metrics.net_usd == pytest.approx(9 * (101 * 0.9995 - 100 * 1.0005))
    assert sum(sim.daily_excess) * 1000 == pytest.approx(sim.metrics.excess_usd)


def test_known_open_is_not_discarded_because_future_close_is_missing() -> None:
    source = panel()
    ds = sorted({b.day for b in source})
    modified = tuple(
        replace(b, close=float("nan")) if b.ticker == "AAA" and b.day == ds[24] else b
        for b in source
    )
    p = model.prepare(modified)
    sim = engine.simulate(
        p, replace(C, horizon=1), selected(p, [(23, "AAA")]), ds[23], ds[28], 5, ledger=True
    )
    assert sim.metrics.unresolved == 1 and sim.metrics.missing_marks > 0
    assert sim.trades[0].quantity > 0 and sim.trades[0].credit is None
    assert not research.test_pass(sim.metrics)


def test_atr_budget_is_a_quantity_cap_not_a_fabricated_stop_fill() -> None:
    p = model.prepare(panel())
    s = settings()
    sim = engine.simulate(
        p,
        replace(C, sizing="atr_budget", horizon=1),
        selected(p, [(23, "AAA")]),
        p.days[23],
        p.days[28],
        5,
        ledger=True,
    )
    assert sim.trades[0].quantity == int(s.risk_usd / (s.risk_atr_multiple * 4))
    assert sim.trades[0].exit == p.days[24]


def good(net: float = 100, excess: float = 50) -> engine.Metrics:
    return engine.Metrics(20, 0, 0, 0, 20, net, excess, 5, 1, 12, 8)


def test_family_selection_has_no_final_test_input_and_requires_robust_folds() -> None:
    base = research.Score(C, (good(),) * 3, (good(),) * 3)
    alt = research.Score(replace(C, threshold=1), (good(),) * 3, (good(excess=100),) * 3)
    assert research.selections((base, alt)) == (alt,)
    bad = research.Score(C, (good(),) * 3, (good(net=-1, excess=-1),) * 2 + (good(),))
    assert not bad.eligible
    assert research.selections((bad,)) == ()
    assert not research.test_pass(replace(good(), unresolved=1))


def test_joint_bootstrap_preserves_identical_strategy_calendar_and_is_seeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        research, "settings", lambda: settings().model_copy(update={"bootstrap_reps": 20})
    )
    one = np.array([[0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.2, -0.2]], dtype=np.float64)
    a = research.bootstrap(one, (4, 4))
    b = research.bootstrap(np.repeat(one, 3, axis=0), (4, 4))
    np.testing.assert_allclose(a, b)
    np.testing.assert_allclose(a, research.bootstrap(one, (4, 4)))
    with pytest.raises(ValueError):
        research.bootstrap(one, (3, 3))


def test_api_arms_require_non_degraded_context_and_never_treat_missing_as_confirmation() -> None:
    data = {
        "flow": {"qualified": True},
        "modules": {
            "m21_dealer_gamma": {
                "branch": "full_short_and_proximate",
                "degraded": True,
                "metadata": {"provider_returned": "yes"},
            }
        },
    }
    assert context.arms(data) == ["price", "flow"]
    data["modules"]["m21_dealer_gamma"]["degraded"] = False
    assert context.arms(data) == ["price", "flow", "flow_gamma"]
    assert context.arms({}) == ["price"]


def fake_report() -> dict[str, Any]:
    return {
        "report_hash": "frozen",
        "tickers": ["NVDA", "SPY"],
        "winners": [{"id": C.id, "config": asdict(C), "paper_enabled": True}],
    }


def future_bars() -> tuple[Bar, ...]:
    ds = [date(2026, 10, 28)]
    for _ in range(25):
        prev = previous_session_before(ds[-1], calendar())
        assert prev is not None
        ds.append(prev)
    ds.reverse()
    return tuple(
        Bar(
            t,
            d,
            100 + i if t == "SPY" else 100,
            102 + i if t == "SPY" else 102,
            98 + i if t == "SPY" else 98,
            101 + i if t == "SPY" else 101,
            1000,
        )
        for t in ("NVDA", "SPY")
        for i, d in enumerate(ds)
    )


def persist_bars(e: Any, bars: tuple[Bar, ...], now: datetime) -> None:
    for ticker in {b.ticker for b in bars}:
        capture_payload(
            e,
            ticker,
            [
                dict(
                    date=str(b.day),
                    market_time="r",
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=b.volume,
                )
                for b in bars
                if b.ticker == ticker
            ],
            now,
        )


def test_first_api_context_and_arm_membership_never_repair_with_later_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'future.db'}")
    monkeypatch.setattr(report, "load", fake_report)
    now = datetime(2026, 10, 28, 22, tzinfo=UTC)
    persist_bars(e, future_bars(), now)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    monitor.observe(e, now)
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price", "flow", "flow_gamma"]})
    monitor.observe(e, now + timedelta(minutes=1))
    with Session(e) as session:
        assert len(list(session.scalars(select(storage.TournamentAudit)))) == 1
        ds = list(session.scalars(select(storage.TournamentDecision)))
        assert len(ds) == 1 and ds[0].arm == "price"
    assert monitor.view(e, now)["total"] == 1
    monkeypatch.setattr(context, "capture", lambda *_: {"arms": ["price"]})
    entry_seen = now + timedelta(days=1)
    persist_bars(
        e,
        (
            Bar("NVDA", date(2026, 10, 29), 100, 102, 98, 101, 1000),
            Bar("SPY", date(2026, 10, 29), 125, 127, 123, 126, 1000),
        ),
        entry_seen,
    )
    monitor.observe(e, entry_seen)
    assert (
        next(r for r in monitor.view(e, entry_seen)["rows"] if r["day"] == date(2026, 10, 28))[
            "state"
        ]
        == "open"
    )
    end = datetime(2026, 11, 2, 22, tzinfo=UTC)
    persist_bars(
        e,
        (
            Bar("NVDA", date(2026, 11, 2), 105, 107, 103, 106, 1000),
            Bar("SPY", date(2026, 11, 2), 126, 128, 124, 127, 1000),
        ),
        end,
    )
    monitor.observe(e, end)
    view = monitor.view(e, end)
    assert next(r for r in view["rows"] if r["day"] == date(2026, 10, 28))["state"] == "closed"
    assert next(r for r in view["rows"] if r["day"] == date(2026, 10, 28))["result"]["pnl_usd"] > 0
    monitor.observe(e, end)
    assert monitor.view(e, end)["total"] == 2
    assert monitor.view(e, now)["rows"][0]["state"] == "pending"


def test_late_candidate_never_creates_past_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'late.db'}")
    monkeypatch.setattr(report, "load", fake_report)
    now = datetime(2026, 10, 29, 16, tzinfo=UTC)
    persist_bars(e, future_bars(), now)
    monitor.observe(e, now)
    assert monitor.view(e, now)["total"] == 0


@pytest.mark.asyncio
async def test_failed_tournament_hook_preserves_other_live_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "STATE", {})

    def fail(*_: Any) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(monitor, "observe", fail)
    await monitor.run(
        make_engine(f"sqlite:///{tmp_path / 'failure.db'}"), datetime(2026, 10, 28, tzinfo=UTC)
    )
    assert monitor.STATE["status"] == "failed"


def test_actual_cached_context_honors_availability_and_latest_degradation(tmp_path: Path) -> None:
    from typing import cast

    from sqlalchemy import Table
    from webapp.alpha_lab import db
    from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry, ensure_telemetry_tables
    from webapp.live_alpha.store import LiveNewsCheck, ensure_live_tables

    e = make_engine(f"sqlite:///{tmp_path / 'context.db'}")
    db.ensure(e)
    ensure_telemetry_tables(e)
    ensure_live_tables(e)
    now = datetime(2026, 10, 28, 21, tzinfo=UTC)
    source = now - timedelta(hours=1, minutes=10)
    for i in range(2):
        payload = {
            "created_at": source.isoformat(),
            "option_chain": f"NVDA261120C{(100 + i) * 1000:08d}",
            "type": "call",
            "total_premium": "200000",
            "total_ask_side_prem": "200000",
            "total_bid_side_prem": "0",
            "has_multileg": False,
        }
        db.append(
            e,
            cast(Table, db.Alert.__table__),
            [
                dict(
                    key=str(i),
                    alert_id=str(i),
                    ticker="NVDA",
                    observed_at=source,
                    payload_json=db.dumps(payload),
                )
            ],
        )
    with Session(e) as ss:
        ss.add(
            AlfaPrintMeta(
                run_id="r",
                event_id="e",
                ticker="NVDA",
                option_chain="NVDA261120C00100000",
                fill_side="ask",
                option_type="call",
                strike="100",
                expiry=date(2026, 11, 20),
                print_ts=source,
                written_at=source,
            )
        )
        for stage, at in [
            ("m21_dealer_gamma", source),
            ("m27_opening_closing", now + timedelta(seconds=1)),
        ]:
            ss.add(
                AlfaStageTelemetry(
                    run_id="r",
                    event_id="e",
                    stage_name=stage,
                    branch="full_short_and_proximate" if "m21" in stage else "strong_opening",
                    metadata_json=db.dumps(
                        {"provider_returned": "yes", "stale_warning_fired": "no"}
                    ),
                    degraded=stage.startswith("m21"),
                    profile_content_hash="profile",
                    written_at=at,
                )
            )
        ss.add(
            LiveNewsCheck(
                check_id="n",
                ticker="NVDA",
                checked_at=source,
                state="failed",
                detail="quota",
                refs_json="[]",
            )
        )
        ss.commit()
    ctx = context.capture(e, "NVDA", date(2026, 10, 28), now)
    assert ctx["flow"]["qualified"] is True and ctx["arms"] == ["price", "flow"]
    assert "m27_opening_closing" not in ctx["modules"]
    assert ctx["news"]["state"] == "failed"
    assert context.capture(e, "NVDA", date(2026, 10, 28), now + timedelta(hours=5))["arms"] == [
        "price"
    ]


def test_equal_market_average_is_neither_above_nor_below() -> None:
    source = tuple(
        replace(b, open=100, high=102, low=98, close=100) if b.ticker == "SPY" else b
        for b in panel()
    )
    p = model.prepare(source)
    assert not model.mask(p, replace(C, regime="above_ma20"))[-1].any()
    assert not model.mask(p, replace(C, regime="below_ma20"))[-1].any()


def test_private_route_stays_authenticated_and_noindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from webapp import main

    from tests.unit._webapp_auth import set_web_auth

    e = make_engine(f"sqlite:///{tmp_path / 'route.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    monkeypatch.setattr(
        monitor,
        "view",
        lambda *_: {
            "report": {
                "configuration_trials": 7776,
                "eligible": 0,
                "sessions": 252,
                "distinct_portfolio_evaluations": 46656,
                "winners": [],
                "access": {},
                "grid_artifact": {},
            },
            "covered": 0,
            "expected": 70,
            "groups": [],
            "rows": [],
            "candidates": [],
        },
    )
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/stratejiler").status_code == 401
    response = client.get("/stratejiler", headers=headers)
    assert response.status_code == 200 and "7" in response.text
    assert (
        response.headers["cache-control"] == "private, no-store"
        and "noindex" in response.headers["x-robots-tag"]
    )
