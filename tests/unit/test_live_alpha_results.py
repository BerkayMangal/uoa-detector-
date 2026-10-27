"""Full-history reporting, missing-outcome honesty and stored-plan cost rendering."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from webapp.board.daily_close import ClosePoint
from webapp.board.db import make_engine
from webapp.board.lag_record import LagRecord
from webapp.live_alpha import page, results, store

from tests.unit._webapp_auth import set_web_auth
from uoa_detector.live_alpha.model import OptionQuote, to_jsonable
from uoa_detector.live_alpha.option_plan import price_view
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.options_alpha.settings import load_settings as load_options_alpha_settings

NOW = datetime(2026, 10, 27, 15, tzinfo=UTC)


def _paper(n: int, pnl: float | None = 10.0, **kwargs: Any) -> store.LivePaper:
    values: dict[str, Any] = {
        "paper_id": str(n), "opportunity_id": f"2026-10-27:NVDA:up:{n}", "rec_id": str(n),
        "ticker": "NVDA", "direction": "up", "instrument": "stock",
        "plan_json": store.dumps({"policy_version": "v1"}), "state": "CLOSED_SIMULATED",
        "created_at": NOW - timedelta(days=3), "entry_at": NOW - timedelta(days=2),
        "entry_price": 100.0, "quantity": 1, "exit_at": NOW + timedelta(minutes=n),
        "exit_price": 110.0, "pnl_usd": pnl, "exit_reason": "hedef",
    }
    values.update(kwargs)
    return store.LivePaper(**values)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'results.db'}")
    store.ensure_live_tables(engine)
    yield engine
    engine.dispose()


def test_all_history_counts_and_display_slice_do_not_change_pnl(engine: Engine) -> None:
    for n in range(205):
        assert store.create_paper(engine, _paper(n, -100.0 if n == 0 else 1.0))
    view = results.build_results(engine)
    assert view.summary.total == 205
    assert view.summary.closed == 205
    assert view.summary.pnl_usd == 104.0
    assert view.shown == len(view.rows) == 200
    home = page.build_view(engine, NOW)
    assert len(home.papers_closed) == 20
    assert home.paper_stats["closed"] == 205
    assert home.paper_stats["pnl_usd"] == 104.0


def test_unknown_unfilled_and_pending_outcomes_are_not_zero_pnl() -> None:
    papers = [
        _paper(1, None, entry_at=None, entry_price=None, quantity=None),
        _paper(2, None, state="UNRESOLVED"),
        _paper(3, 999, state="PAPER_PENDING"),
        _paper(4, 999, state="PAPER_OPEN"),
        _paper(5, 999, state="EXIT_SIGNALLED"),
        _paper(6, None),
        _paper(7, float("nan")),
        _paper(8, 50, exit_price=None),
    ]
    s = results.summarise([results.paper_result(p) for p in papers])
    assert (s.total, s.unfilled, s.unknown, s.pending, s.open, s.exit_pending) == (8, 1, 4, 1, 1, 1)
    assert s.closed == 0
    assert s.pnl_usd is s.average_usd is s.realised_decline_usd is None


def test_drawdown_uses_exit_order_and_batches_simultaneous_exits() -> None:
    rows = [results.paper_result(_paper(1, 100)), results.paper_result(_paper(2, -80)),
            results.paper_result(_paper(3, 20)), results.paper_result(_paper(4, -30))]
    s = results.summarise(list(reversed(rows)))
    assert s.pnl_usd == 10
    assert s.realised_decline_usd == 90
    assert (s.wins, s.losses, s.flats) == (2, 2, 0)
    simultaneous = [replace(rows[0], exit_at=NOW), replace(rows[1], exit_at=NOW)]
    assert results.summarise(simultaneous).realised_decline_usd == 0


def test_initial_loss_is_a_decline_and_zero_is_a_priced_outcome() -> None:
    rows = [results.paper_result(_paper(1, -20)), results.paper_result(_paper(2, 0))]
    s = results.summarise(rows)
    assert (s.closed, s.flats, s.realised_decline_usd) == (2, 1, 20)


def test_policy_and_instrument_results_stay_separate(engine: Engine) -> None:
    store.create_paper(engine, _paper(1, -20))
    store.create_paper(engine, _paper(2, 100, plan_json=store.dumps({"policy_version": "v2"})))
    store.create_paper(engine, _paper(3, -30, instrument="long_call"))
    view = results.build_results(engine)
    groups = {(g.policy, g.instrument): g.summary.pnl_usd for g in view.groups}
    assert groups == {("v1", "stock"): -20, ("v2", "stock"): 100, ("v1", "long_call"): -30}
    assert view.summary.pnl_usd == 50


def test_lag_coverage_uses_same_peers_and_separates_profile_versions() -> None:
    start, end, next_day = date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26)
    record = LagRecord(end, "A", "auto", "auto", ("B",), start, end, 5, -5, 0, -5, True, False, "v1")
    closes = {
        "A": (ClosePoint(start, 100), ClosePoint(end, 95), ClosePoint(next_day, 104.5)),
        "B": (ClosePoint(start, 100), ClosePoint(end, 100), ClosePoint(next_day, 102)),
    }
    summaries = results.summarise_lags([record, replace(record, version="v2")], closes)
    assert {s.version for s in summaries} == {"v1", "v2"}
    one = next(s for s in summaries if s.version == "v1" and s.group == "Geride kalanlar" and s.sessions == 1)
    assert (one.priced, one.total, one.mean_pp) == (1, 1, 8.0)
    assert all(s.mean_pp is None and s.priced == 0 for s in summaries if s.sessions > 1)
    missing_peer = results.summarise_lags([record], {**closes, "B": closes["B"][:2]})
    assert all(s.mean_pp is None for s in missing_peer)


def test_results_route_auth_empty_state_and_escaping(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import webapp.main as main

    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/sonuclar").status_code == 401
    response = client.get("/sonuclar", headers=headers)
    assert response.status_code == 200
    assert "Henüz PAPER kaydı yok" in response.text
    assert "Akran ölçümü tablosu henüz yok" in response.text
    assert 'data-paper-summary' in response.text
    store.create_paper(engine, _paper(1, None, state="UNRESOLVED", exit_reason="<script>bad()</script>"))
    response = client.get("/sonuclar", headers=headers)
    assert "Sonuç belirsiz" in response.text
    assert "&lt;script&gt;bad()&lt;/script&gt;" in response.text
    assert "<script>bad()" not in response.text


@pytest.mark.parametrize("missing_quote", [False, True])
def test_cost_comparison_uses_saved_prices_and_keeps_risk_blocker(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, missing_quote: bool,
) -> None:
    import webapp.main as main

    settings = load_live_alpha_settings()
    quote = OptionQuote("NVDA261120C00100000", "NVDA", "call", Decimal(100),
                        date(2026, 11, 20), None if missing_quote else Decimal("1.00"),
                        Decimal("1.10"), NOW, True)
    option = price_view("long_call", [(quote, True)], NOW, NOW.date(), settings.option_plan,
                        load_options_alpha_settings(), 100)
    expected_state = "QUOTE_PENDING" if missing_quote else "RISK_BLOCKED"
    assert option.readiness.value == expected_state
    card = {
        "opportunity_id": "test", "ticker": "NVDA", "recommendation": "BUY", "readiness": "READY",
        "market_mode": "LIVE", "decision_as_of": NOW.isoformat(), "direction": "up",
        "headline": "test", "why_today": "test", "blockers": [], "sources": [],
        "rec_id": "test", "profile_sha256": "test", "stock_plan": {
            "r_usd": 100, "notional_usd": 1000, "entry_ref": 100, "planned_risk_usd": 90,
            "stop": 91, "target": 118, "horizon_sessions": 5, "shares": 10, "note": "",
        },
        "options": [to_jsonable(option)],
        "instrument": {"preferred": "stock", "reason": "opsiyon risk bütçesine sığmıyor"},
    }
    store.write_scan(engine, store.ScanRow("s", NOW, NOW, "LIVE", "ok", "run", {"cards": [card]}), "v1")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: NOW)
    response = TestClient(main.app, headers=set_web_auth(monkeypatch)).get("/")
    assert response.status_code == 200
    assert 'data-instrument-comparison="NVDA"' in response.text
    assert "$1000.00" in response.text
    if missing_quote:
        assert option.entry_cost_usd is None
        assert "bugün işlem yok, NBBO boş" in response.text
    else:
        assert f"${option.entry_cost_usd:.2f}" in response.text
    assert expected_state in response.text
    assert "0 yapı" in response.text
    assert "Planda parasal toplam yok" in response.text
    assert "komisyon hariç" in response.text
    monkeypatch.setattr(main, "_now", lambda: NOW + timedelta(hours=2))
    body = TestClient(main.app, headers=set_web_auth(monkeypatch)).get("/").text
    assert "Aşağıdaki fiyatlar güncel işlem fiyatı değildir" in body
