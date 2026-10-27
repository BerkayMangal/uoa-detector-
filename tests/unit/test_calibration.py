"""Temporal causality, executable budgets and frozen forward-only instructions."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from webapp import main
from webapp.board.db import make_engine
from webapp.calibration import model, monitor, report, search, storage
from webapp.calibration.settings import calendar, settings

from tests.unit._webapp_auth import set_web_auth
from uoa_detector.live_alpha.calendar import next_session_after

C = model.Config(5, 0, 0, 0, "any", 2)


def days(start: date, count: int) -> list[date]:
    result = [start]
    for _ in range(count-1):
        nxt = next_session_after(result[-1], calendar())
        assert nxt is not None
        result.append(nxt)
    return result


def bar(ticker: str, d: date, px: float = 100) -> model.Bar:
    return model.Bar(ticker, d, px, px+2, px-2, px+1, 1000)


def feature(ticker: str, d: date, lag: float = 2) -> model.Feature:
    return model.Feature(ticker, d, lag, 0.5, 1.5, 0.5, 1, True)


def test_grid_is_bounded_unique_and_preserves_primary_horizon() -> None:
    grid = model.grid()
    assert len(grid) == len({c.id for c in grid}) == 1728
    assert settings().primary_sessions == 3 and settings().secondary_sessions == (1, 5)


def test_features_are_prefix_only_and_a_missing_session_is_not_compressed() -> None:
    dates = days(date(2026, 1, 5), 30)
    prefix = tuple(bar(t, d, 100+i*(1 if t == "SPY" else 0.5)) for t in ("AAA", "SPY") for i,d in enumerate(dates))
    first = model.features(prefix, 5)
    assert first
    future = tuple(bar(t, d, 9999) for t in ("AAA", "SPY") for d in days(next_session_after(dates[-1], calendar()), 5))  # type: ignore[arg-type]
    assert tuple(f for f in model.features(prefix+future, 5) if f.day <= dates[-1]) == first
    missing = tuple(b for b in prefix if not (b.ticker == "AAA" and b.day == dates[15]))
    assert not model.features(missing, 5)


def test_atr_and_volume_use_prior_sessions_not_the_signal_day() -> None:
    dates = days(date(2026, 1, 5), 25)
    panel = tuple(bar(t,d) for t in ("AAA", "SPY") for d in dates)
    baseline = model.features(panel, 5)[-1]
    changed = tuple(replace(b, high=1000, volume=1000000) if b.ticker == "AAA" and b.day == dates[-1] else b for b in panel)
    f = model.features(changed, 5)[-1]
    assert f.recovery == baseline.recovery and f.volume == 1000


def test_declared_horizon_skips_weekend_and_labor_day() -> None:
    assert model.horizon_days(date(2026,9,4), 3) == (date(2026,9,8), date(2026,9,10))
    assert model.horizon_days(date(2027,12,31), 3) is None


def test_cash_is_spent_at_open_before_same_day_exits_release_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "settings", lambda: settings().model_copy(update={"capital_usd":1000.0, "position_usd":1000.0}))
    ds = days(date(2026,1,5), 5)
    panel = tuple(bar(t,d) for t in ("AAA", "BBB", "SPY") for d in ds)
    fs = (feature("AAA", ds[0]), feature("BBB", ds[1]))
    m, trades = search.simulate(C, fs, panel, ds[0], ds[-1], 2, 5)
    assert len(trades) == m.closed == 1
    assert trades[0].quantity == 9
    assert trades[0].debit == pytest.approx(900.45)
    assert trades[0].pnl == pytest.approx(9*((101*.9995)-(100*1.0005)))
    assert trades[0].spy_excess == pytest.approx(0)


def test_cash_and_ranking_are_fixed_and_no_concurrent_reentry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "settings", lambda: settings().model_copy(update={"capital_usd":1000.0, "position_usd":1000.0}))
    ds = days(date(2026,1,5), 7)
    panel = tuple(bar(t,d) for t in ("AAA", "BBB", "SPY") for d in ds)
    fs = (feature("BBB", ds[0]), feature("AAA", ds[0]), feature("AAA", ds[1]))
    _, ledger = search.simulate(C, fs, panel, ds[0], ds[-1], 3, 5)
    assert len(ledger) == 1 and ledger[0].ticker == "AAA"


def test_missing_exit_never_uses_a_later_close_or_releases_cash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "settings", lambda: settings().model_copy(update={"capital_usd":1000.0, "position_usd":1000.0}))
    ds = days(date(2026,1,5), 7)
    panel = tuple(bar(t,d) for t in ("AAA", "BBB", "SPY") for d in ds if not(t == "AAA" and d == ds[2]))
    m, ledger = search.simulate(C, (feature("AAA",ds[0]), feature("BBB",ds[3])), panel, ds[0], ds[-1], 2, 5)
    assert m.closed == 0 and m.unresolved == 1 and ledger[0].credit is None


def test_splits_exclude_trades_whose_declared_exit_crosses_boundary() -> None:
    ds = days(date(2026,1,5), 5)
    panel = tuple(bar(t,d) for t in ("AAA", "SPY") for d in ds)
    m, ledger = search.simulate(C, (feature("AAA",ds[2]),), panel, ds[0], ds[-1], 3, 5)
    assert m.closed == 0 and ledger == ()
    s = settings()
    assert len(days(s.train_end, 8)) == 8
    assert days(s.train_end, 8)[-1] == s.validation_start
    assert days(s.validation_end, 8)[-1] == s.test_start


def test_selection_has_no_test_input_and_rejects_incomplete_or_small_samples() -> None:
    good = search.Metrics(30,0,0,100,1,5,0.1,30)
    better = replace(good, spy_excess_pp=0.2)
    alt = replace(C, lag=1)
    assert search.choose(((C,good,good),(alt,good,better))) == alt
    for bad in (replace(good,closed=29), replace(good,net_usd=-1),replace(good,spy_excess_pp=None),replace(good,matched=29),replace(good,unresolved=1),replace(good,max_drawdown_pct=21)):
        assert search.choose(((C,good,bad),)) is None


def test_future_test_prices_do_not_change_validation_choice() -> None:
    ds = days(date(2026,1,5), 8)
    bars = tuple(bar(t,d) for t in ("AAA", "SPY") for d in ds)
    fs = (feature("AAA",ds[0]),)
    before = search.simulate(C,fs,bars,ds[0],ds[3],3,5)
    changed = tuple(replace(b, close=1, low=1) if b.day > ds[3] else b for b in bars)
    assert search.simulate(C,fs,changed,ds[0],ds[3],3,5) == before


def payload(d: date, px: float = 100) -> list[dict[str, object]]:
    return [{"date":str(d),"market_time":"r","open":px,"high":px+2,"low":px-2,"close":px+1,"volume":1000}]


def test_side_capture_is_settled_asof_immutable_and_preserves_conflicts(tmp_path: Path) -> None:
    e = make_engine(f"sqlite:///{tmp_path/'capture.db'}")
    day = date(2026,11,27)
    before = datetime(2026,11,27,17,tzinfo=UTC)
    storage.capture_payload(e,"AAA",payload(day),before)
    assert storage.bars_at(e,before) == ()
    after = before + timedelta(hours=5)
    storage.capture_payload(e,"AAA",payload(day),after)
    storage.capture_payload(e,"AAA",payload(day,500),after)
    assert storage.bars_at(e,before) == () and storage.bars_at(e,after)[0].open == 100
    storage.capture_payload(e,"BAD",payload(day)+payload(day,500),after)
    assert len(storage.bars_at(e,after)) == 1


def stub_report() -> dict[str, Any]:
    from dataclasses import asdict
    return {"verdict":"EXPERIMENTAL_FORWARD_ONLY","selected":{"config":asdict(C)},"report_sha256":"frozen", "tickers":["AAA","SPY"]}


def test_prospective_decision_precedes_entry_and_exact_prices_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = make_engine(f"sqlite:///{tmp_path/'forward.db'}")
    monkeypatch.setattr(report,"load",stub_report)
    signal = date(2026,10,28)
    now = datetime(2026,10,28,21,tzinfo=UTC)
    monkeypatch.setattr(monitor,"features",lambda *_:(feature("AAA",signal),))
    for t in ("AAA","SPY"):
        storage.capture_payload(e,t,payload(signal),now)
    monitor.observe(e,now)
    with Session(e) as s:
        assert len(list(s.scalars(select(storage.Decision)))) == 1
    entry = date(2026,10,29)
    entry_seen = now + timedelta(days=1)
    for t in ("AAA","SPY"):
        storage.capture_payload(e,t,payload(entry,101),entry_seen)
    monitor.observe(e,entry_seen)
    assert monitor.view(e,entry_seen)["paper"][0]["status"] == "open"
    end = date(2026,11,2)
    end_seen = datetime(2026,11,2,22,tzinfo=UTC)
    for t in ("AAA","SPY"):
        storage.capture_payload(e,t,payload(end,106 if t == "AAA" else 102),end_seen)
    monitor.observe(e,end_seen)
    v=monitor.view(e,end_seen)
    assert v["closed"] == v["matched"] == 1 and v["pnl"] > 0
    storage.capture_payload(e,"AAA",payload(end,1),end_seen)
    monitor.observe(e,end_seen)
    assert monitor.view(e,end_seen)["pnl"] == v["pnl"]
    assert monitor.view(e,now)["closed"] == 0


def test_late_observation_never_creates_a_historical_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = make_engine(f"sqlite:///{tmp_path/'late.db'}")
    monkeypatch.setattr(report,"load",stub_report)
    day = date(2026,10,28)
    late = datetime(2026,10,29,15,tzinfo=UTC)
    monkeypatch.setattr(monitor,"features",lambda *_:(feature("AAA",day),))
    storage.capture_payload(e,"SPY",payload(day),late)
    monitor.observe(e,late)
    assert monitor.view(e,late)["total"] == 0


@pytest.mark.asyncio
async def test_failed_monitor_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor, "STATE", {})
    def failed(*_: Any) -> None:
        raise RuntimeError("synthetic")
    monkeypatch.setattr(monitor,"observe",failed)
    await monitor.run(make_engine(f"sqlite:///{tmp_path/'fail.db'}"),datetime(2026,10,28,tzinfo=UTC))
    assert monitor.STATE["status"] == "failed"


def test_private_report_route_keeps_auth_and_noindex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e=make_engine(f"sqlite:///{tmp_path/'private.db'}")
    monkeypatch.setattr(main,"_board_reader",lambda:SimpleNamespace(engine=e))
    monkeypatch.setattr(monitor,"view",lambda *_:{"report":{"verdict":"NO_CANDIDATE","configuration_trials":1728,"train_eligible":0,"validation_eligible":0,"rows":0,"tickers":[],"portfolio_evaluations":3456},"paper":[],"closed":0,"total":0,"pnl":None,"matched":0,"covered":0,"expected":70,"job":{}})
    headers=set_web_auth(monkeypatch)
    client=TestClient(main.app)
    assert client.get('/kalibrasyon').status_code == 401
    r=client.get('/kalibrasyon',headers=headers)
    assert r.status_code == 200 and 'Yeni kalibrasyon PAPER adayı açılmadı' in r.text
    assert 'noindex' in r.headers['x-robots-tag'] and r.headers['cache-control'] == 'private, no-store'


def test_frozen_report_rejects_changed_profile_code_and_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from webapp.alpha_lab.db import digest
    p=tmp_path/'report.json'
    monkeypatch.setattr(report,'REPORT_PATH',p)
    body={"profile_sha256":settings().profile_hash,"code_sha256":search.code_hash(),"verdict":"NO_CANDIDATE"}
    for changes in ({"profile_sha256":"wrong"},{"code_sha256":"wrong"},{"verdict":"tampered"}):
        modified=body|changes
        p.write_text(json.dumps(modified|{"report_sha256":digest(body) if 'verdict' in changes else digest(modified)}))
        with pytest.raises(ValueError):
            report.load.__wrapped__()
    p.write_text(json.dumps(body|{"report_sha256":digest(body)}))
    assert report.load.__wrapped__()['verdict'] == 'NO_CANDIDATE'


def test_volume_missing_never_satisfies_positive_volume_gate() -> None:
    f=replace(feature('AAA',date(2026,10,28)),volume=None)
    assert model.matches(f,C)
    assert not model.matches(f,replace(C,volume=1))


def test_price_above_unit_budget_remains_unfilled_not_zero_profit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e=make_engine(f"sqlite:///{tmp_path/'expensive.db'}")
    monkeypatch.setattr(report,'load',stub_report)
    day=date(2026,10,28)
    now=datetime(2026,10,28,22,tzinfo=UTC)
    monkeypatch.setattr(monitor,'features',lambda *_:(feature('AAA',day),))
    storage.capture_payload(e,'SPY',payload(day),now)
    monitor.observe(e,now)
    entry_seen=now+timedelta(days=1)
    storage.capture_payload(e,'AAA',payload(date(2026,10,29),3000),entry_seen)
    monitor.observe(e,entry_seen)
    v=monitor.view(e,entry_seen)
    assert v['paper'][0]['status']=='unfilled' and v['closed']==0 and v['pnl'] is None
