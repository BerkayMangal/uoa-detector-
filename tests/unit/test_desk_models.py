"""Known option arithmetic and prospective cached-reference comparisons."""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp.board.atm import AlfaAtm
from webapp.desk import benchmarks, capture, db, scenarios
from webapp.live_alpha import store

from tests.unit.test_live_alpha_job import T0, _seed
from tests.unit.test_position_desk import paper


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    e = _seed(f"sqlite:///{tmp_path / 'references.db'}")
    db.ensure(e)
    yield e
    e.dispose()


def scan(at: Any, price: float = 100, spy: float = 500, spy_time: Any = None) -> store.ScanRow:
    return store.ScanRow(str(at), at, at, "LIVE", "ok", "r", {"price_observations": {
        "NVDA": {"spot": price, "source_at": at.isoformat(), "session": at.date().isoformat()},
        "SPY": {"spot": spy, "source_at": (spy_time or at).isoformat(), "session": at.date().isoformat()},
    }})


def finish(e: Engine, at: Any) -> None:
    with e.begin() as c:
        c.execute(update(store.LivePaper).values(state="CLOSED_SIMULATED", exit_at=at, exit_price=110, pnl_usd=99.5))


def test_entry_snapshot_is_immutable_and_missing_old_entry_is_not_repaired(engine: Engine) -> None:
    paper(engine)
    assert capture.capture_benchmarks(engine, scan(T0), T0) == 1
    assert capture.capture_benchmarks(engine, scan(T0, price=999), T0) == 0
    with Session(engine) as s:
        saved = s.scalar(select(db.Benchmark))
        assert saved is not None and json.loads(saved.payload_json)["points"]["NVDA"]["value"] == 100
    end = T0 + timedelta(minutes=3)
    finish(engine, end)
    capture.capture_benchmarks(engine, scan(end, price=110, spy=525), end)
    result = benchmarks.build(engine, end)
    assert result.spy_known == 1 and result.rows[0].spy_excess == pytest.approx(5)
    assert result.rows[0].pnl == 99.5


@pytest.mark.parametrize("missing_peer", [False, True])
def test_peers_are_frozen_and_one_missing_peer_keeps_comparison_unknown(engine: Engine, monkeypatch: pytest.MonkeyPatch, missing_peer: bool) -> None:
    paper(engine)
    monkeypatch.setattr(capture, "_peers", lambda *_: ("P1", "P2"))
    with Session(engine) as s:
        for ticker, price in (("P1", 100), ("P2", 200)):
            s.add(AlfaAtm(ticker=ticker, expiry=T0.date(), strike=100, stock_price=price, fetched_at=T0, trade_date=T0.date()))
        s.commit()
    capture.capture_benchmarks(engine, scan(T0), T0)
    monkeypatch.setattr(capture, "_peers", lambda *_: ("OTHER",))
    end = T0 + timedelta(minutes=3)
    finish(engine, end)
    with engine.begin() as c:
        c.execute(update(AlfaAtm).where(AlfaAtm.ticker.in_(["P1", "P2"])).values(stock_price=AlfaAtm.stock_price * 1.1, fetched_at=end))
        if missing_peer:
            c.execute(update(AlfaAtm).where(AlfaAtm.ticker == "P2").values(fetched_at=end + timedelta(seconds=1)))
    capture.capture_benchmarks(engine, scan(end, 110, 525), end)
    result = benchmarks.build(engine, end)
    assert result.spy_known == 1
    assert result.peers_known == (0 if missing_peer else 1)
    assert result.rows[0].peer_excess == (None if missing_peer else pytest.approx(0))


def test_skew_future_prices_and_asof_availability_are_not_silently_paired(engine: Engine) -> None:
    paper(engine)
    capture.capture_benchmarks(engine, scan(T0), T0)
    end = T0 + timedelta(minutes=3)
    finish(engine, end)
    capture.capture_benchmarks(engine, scan(end, 110, 525, end - timedelta(seconds=61)), end + timedelta(seconds=5))
    assert benchmarks.build(engine, end).spy_known == 0
    assert benchmarks.build(engine, end + timedelta(seconds=5)).spy_known == 0


def test_historical_entry_is_not_backfilled_from_a_new_exit_quote(engine: Engine) -> None:
    paper(engine)
    end = T0 + timedelta(minutes=3)
    finish(engine, end)
    capture.capture_benchmarks(engine, scan(end, 110, 525), end)
    result = benchmarks.build(engine, end)
    assert result.total == 1 and result.spy_known == result.peers_known == 0


def test_european_option_known_values_and_put_call_parity() -> None:
    call = scenarios.option_value(100, 100, 1, 0.2, 0.05, 0, "call")
    put = scenarios.option_value(100, 100, 1, 0.2, 0.05, 0, "put")
    assert call == pytest.approx(10.4505835722)
    assert put == pytest.approx(5.5735260223)
    assert call - put == pytest.approx(100 - 100 * math.exp(-0.05))
    assert scenarios.option_value(105, 100, 0, 0.2, 0.05, 0, "call") == 5


def test_iv_inversion_refuses_impossible_prices() -> None:
    assert scenarios.implied_vol(100, 100, 1, 9.925053717, "call") == pytest.approx(0.2)
    assert scenarios.implied_vol(100, 100, 1, 200, "call") is None
    assert scenarios.implied_vol(100, 100, 0, 2, "call") is None


@pytest.mark.parametrize("right,vol", [("wrong", 0.2), ("call", -1)])
def test_invalid_model_inputs_are_rejected(right: str, vol: float) -> None:
    with pytest.raises(ValueError):
        scenarios.option_value(100, 100, 1, vol, 0.04, 0, right)


def scenario_card() -> dict[str, Any]:
    return {"recommendation": "BUY", "direction": "up", "spot_ref": 100, "spot_ref_at": T0.isoformat(),
        "stock_plan": {"shares": 10}, "options": [{"kind": "long_call", "readiness": "READY",
            "entry_debit": 0.306, "lots": 3, "multiplier": 100, "commission_usd": 1.3,
            "legs": [{"option_symbol": "NVDA260924C00100000", "right": "call", "strike": 100,
                "expiry": T0.date().isoformat(), "bid": 0.28, "ask": 0.3, "is_long": True,
                "fetched_at": T0.isoformat(), "implied_volatility": 0.2, "iv_at": T0.isoformat()}]}]}


def test_scenarios_settle_expired_options_and_include_whole_lot_costs() -> None:
    result = scenarios.build(scenario_card(), T0)
    flat_call = next(c for c in result.summary if c.kind == "long_call" and c.move_pct == 0)
    flat_stock = next(c for c in result.summary if c.kind == "stock" and c.move_pct == 0)
    assert flat_call.pnl == pytest.approx(-95.7)
    assert flat_stock.pnl == pytest.approx(-1)
    assert flat_call.iv_basis == "UW son işlem IV"
    assert not scenarios.build(scenario_card(), T0, withdrawn=True).cases
    assert not scenarios.build(scenario_card(), T0 + timedelta(seconds=901)).cases


def test_stale_or_future_provider_iv_uses_explicit_model_basis() -> None:
    card = scenario_card()
    card["options"][0]["legs"][0]["iv_at"] = (T0 + timedelta(seconds=1)).isoformat()
    result = scenarios.build(card, T0)
    assert any(c.iv_basis == "Mid kotasyondan model IV" for c in result.cases if c.kind == "long_call")


def test_newer_price_breaking_the_plan_withdraws_view_and_alert_without_rewriting_decision(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from webapp.live_alpha import alerts, job, page

    from tests.unit.test_live_alpha_job import FakeUW, _cycle, _move_spot

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return T0 if tz is None else T0.astimezone(tz)

    monkeypatch.setattr(job, "datetime", Clock)
    first = _cycle(engine, FakeUW(), T0)
    frozen = first.snapshot["cards"][0]
    assert frozen["recommendation"] == "BUY"
    _move_spot(engine, 96, T0 + timedelta(seconds=1))
    view = page.build_view(engine, T0 + timedelta(seconds=2))
    assert view.actionable[0].tone == "gray" and "stopa ulaştı" in view.actionable[0].withdrawn
    assert alerts.feed(engine, T0 + timedelta(seconds=2)).signals == ()
    saved = store.latest_scan(engine)
    assert saved is not None and saved.snapshot["cards"][0] == frozen


def test_existing_contract_response_preserves_iv_without_changing_instrument_choice(engine: Engine) -> None:
    from tests.unit.test_live_alpha_job import FakeUW, _cycle

    class WithIV(FakeUW):
        async def request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
            response = await super().request_json(path, **kwargs)
            if path.endswith("/option-contracts"):
                for item in response["data"]:
                    item["implied_volatility"] = "0.42"
            return response

    before = _cycle(engine, FakeUW(), T0).snapshot["cards"][0]
    after = _cycle(engine, WithIV(), T0 + timedelta(seconds=1)).snapshot["cards"][0]
    assert before["instrument"] == after["instrument"]
    assert after["options"][0]["legs"][0]["implied_volatility"] == 0.42
    assert after["options"][0]["legs"][0]["iv_at"] is not None
