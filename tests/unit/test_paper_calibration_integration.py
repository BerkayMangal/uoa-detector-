"""Causal PAPER observation, matched outcomes and private read-only endpoints."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp.board.db import make_engine
from webapp.live_alpha.store import ScanRow
from webapp.paper_calibration import observer, report, store
from webapp.paper_calibration.model import State
from webapp.paper_calibration.settings import settings

from tests.unit._webapp_auth import set_web_auth
from uoa_detector.live_alpha.settings import load_live_alpha_settings

LIVE = load_live_alpha_settings()
CFG = settings()
OPPORTUNITY = "2026-11-24:NVDA:up"
START = datetime(2026, 11, 24, 14, 35, tzinfo=UTC)


def _scan(
    started: datetime,
    stock: float | None,
    *,
    signal: str = "BUY",
    spy: float | None = 500,
    profile_sha: str = LIVE.profile_sha256,
    flow_current: bool = True,
    source_shift: timedelta = timedelta(seconds=-1),
) -> ScanRow:
    when = (started + source_shift).isoformat()
    ready = signal == "BUY"
    snapshot = {
        "flow_current": flow_current,
        "policy_version": LIVE.policy_version,
        "profile_sha256": profile_sha,
        "cards": [{
            "opportunity_id": OPPORTUNITY, "ticker": "NVDA", "direction": "up", "recommendation": signal,
            "readiness": "READY" if ready else "INVALID", "rec_id": "r1" if ready else "r2",
            "path": "P1_news_continuation", "headline": "NVDA fiyat teyidi",
            "decision_as_of": started.isoformat(), "policy_version": LIVE.policy_version,
            "profile_sha256": profile_sha, "instrument": {"preferred": "stock" if ready else "none"},
            "stock_plan": {"entry_ref": 100, "stop": 97, "target": 106, "chase_limit": 103, "shares": 25},
        }],
        "price_observations": {
            "NVDA": {"spot": stock, "source_at": when, "session": started.date().isoformat()},
            "SPY": {"spot": spy, "source_at": when, "session": started.date().isoformat()},
        },
        "flow_observations": {
            "NVDA": {
                "ticker": "NVDA", "run_id": "test", "prints_raw": 2, "prints_deduped": 2,
                "premium_up": 500000, "premium_down": 0, "premium_side_unknown": 0,
                "distinct_contracts": 2, "first_ts": when, "last_ts": when, "largest_premium": 500000,
                "largest_contract": None, "top_contracts": [], "strikes_seen": [],
            },
        },
        "news_research_context": {"prices": {"NVDA": {"atr": 2.0}}},
    }
    return ScanRow(
        scan_id=started.isoformat(), started_at=started, finished_at=started + timedelta(seconds=10),
        market_mode="LIVE", status="ok", run_id="test", snapshot=snapshot,
    )


def test_physical_scan_clock_watch_fill_paired_spy_and_stop_comparison(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    try:
        first = _scan(START, 100.0)
        watch = _scan(START + timedelta(minutes=6), 100.0, signal="WATCH")
        confirm = _scan(START + timedelta(minutes=12), 100.1)
        stops = _scan(START + timedelta(minutes=16), 96.9, spy=495.0)
        target = _scan(START + timedelta(minutes=61), 106.0, spy=510.0)
        for row in (first, watch, watch, confirm, stops, target, watch):
            observer.observe(engine, row, LIVE, CFG)
        record, rows = store.detail_inputs(engine, CFG.study_key, OPPORTUNITY)
        assert record is not None
        assert len(rows) == 5
        case = observer.Case.model_validate_json(record.document)
        legacy, guard, middle, wide = case.positions
        assert legacy.entry_at == watch.finished_at
        assert all(p.entry_at == confirm.finished_at for p in (guard, middle, wide))
        assert [p.exit_reason for p in case.positions] == ["stop", "stop", "target", "target"]
        assert all(p.state is State.CLOSED for p in case.positions)
        assert [p.stop for p in case.positions] == [97, 97, 96, 95]
        assert all(p.target == 106 for p in case.positions)
        assert all(p.planned_risk <= CFG.risk_usd for p in case.positions)
        assert [p.relative_pp for p in case.markouts] == [-2.1, 4.0]
        assert case.seed.published_at == first.finished_at
        watch_evidence = observer.Observation.model_validate_json(rows[1].document)
        assert watch_evidence.source_recommendation == "WATCH"
        assert [step.reason for step in watch_evidence.actions] == ["entry_legacy", *["entry_watch"] * 3]

        page = report.build(engine, "nvda", 1, OPPORTUNITY)
        assert page.cohort_total == page.total == 1
        assert page.detail is not None and page.detail.id == OPPORTUNITY
        assert len(page.events) == 5
        assert page.arms[0].closed == 1
        assert page.arms[1].paired == page.arms[1].paired_sessions == 1
        assert page.arms[0].delta_bar_pct is None
        assert max(a.delta_bar_pct for a in page.arms if a.delta_bar_pct is not None) == pytest.approx(100)
        assert page.arms[1].paired_delta_r == pytest.approx((guard.pnl_usd - legacy.pnl_usd) / CFG.risk_usd)
        assert page.arms[2].paired_delta_r == pytest.approx((middle.pnl_usd - guard.pnl_usd) / CFG.risk_usd)
    finally:
        engine.dispose()


def test_wrong_source_profile_and_future_price_fail_closed(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    try:
        observer.observe(engine, _scan(START - timedelta(minutes=1), 100.0, profile_sha="changed"), LIVE, CFG)
        assert report.build(engine, "", 1, "").cohort_total == 0
        observer.observe(engine, _scan(START, 100.0), LIVE, CFG)
        observer.observe(engine, _scan(START + timedelta(minutes=6), 100.0, source_shift=timedelta(seconds=5)),
                         LIVE, CFG)
        case, events = store.detail_inputs(engine, CFG.study_key, OPPORTUNITY)
        assert case is not None
        assert len(events) == 2
        assert all(p.state is State.PENDING for p in observer.Case.model_validate_json(case.document).positions)
        assert observer.Observation.model_validate_json(events[-1].document).quote is None
    finally:
        engine.dispose()


def test_skipped_horizon_session_does_not_sell_against_a_later_quote(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    try:
        observer.observe(engine, _scan(START, 100.0), LIVE, CFG)
        observer.observe(engine, _scan(START + timedelta(minutes=6), 100.0), LIVE, CFG)
        # 26 November is a holiday. The fifth following session is 2 December.
        observer.observe(engine, _scan(datetime(2026, 12, 3, 15, 0, tzinfo=UTC), 90.0, flow_current=False),
                         LIVE, CFG)
        case, _ = store.detail_inputs(engine, CFG.study_key, OPPORTUNITY)
        assert case is not None and not case.active
        positions = observer.Case.model_validate_json(case.document).positions
        assert all(p.state is State.UNKNOWN and p.exit_reason == "horizon_session_unobserved"
                   and p.pnl_usd is None for p in positions)
    finally:
        engine.dispose()


def test_private_html_and_json_are_select_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from webapp import main

    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    headers = set_web_auth(monkeypatch)
    observer.observe(engine, _scan(START, 100.0), LIVE, CFG)
    statements = []

    def executed(
        _connection: object, _cursor: object, sql: str, _parameters: object, _context: object, _many: bool,
    ) -> None:
        statements.append(sql.lstrip().split(None, 1)[0].upper())

    def no_external(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("A PAPER results GET cannot request vendor data")

    event.listen(engine, "before_cursor_execute", executed)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_external)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_external)
    try:
        client = TestClient(main.app)
        export = f"/api/paper-kalibrasyon/{OPPORTUNITY}"
        assert client.get("/paper-kalibrasyon").status_code == 401
        assert client.get(export).status_code == 401
        assert statements == []

        detail = client.get("/paper-kalibrasyon", params={"ticker": "nvda", "opportunity": OPPORTUNITY},
                            headers=headers)
        assert detail.status_code == 200
        assert 'data-paper-ready="true"' in detail.text
        assert "Olay günlüğü" in detail.text
        assert "Güncel AL" in detail.text
        assert detail.headers["cache-control"] == "private, no-store"
        assert client.get("/paper-kalibrasyon?page=0", headers=headers).status_code == 422
        assert client.get("/paper-kalibrasyon?state=invalid", headers=headers).status_code == 422
        assert client.get("/paper-kalibrasyon?day=24.11.2026", headers=headers).status_code == 422
        assert client.get("/paper-kalibrasyon?day=&state=all", headers=headers).status_code == 200
        filtered = client.get("/paper-kalibrasyon", params={"day": START.date(), "state": "pending"},
                              headers=headers)
        assert filtered.status_code == 200 and f'data-paper-case="{OPPORTUNITY}"' in filtered.text

        response = client.get(export, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["case"]["seed"]["opportunity_id"] == OPPORTUNITY
        assert len(body["observations"]) == 1
        assert body["configuration"]["profile_sha256"] == CFG.profile_sha256
        assert client.get("/api/paper-kalibrasyon/no-such-case", headers=headers).status_code == 404
        assert set(statements) <= {"SELECT", "PRAGMA"}
        client.close()
    finally:
        engine.dispose()


def test_worker_recovers_missed_scans_after_activation_using_only_archived_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from webapp.live_alpha import store as source_store

    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    cfg = CFG.model_copy(update={"recovery_batch": 1})
    old = _scan(START - timedelta(minutes=6), 99.0)
    first = _scan(START, 100.0)
    missed = _scan(START + timedelta(minutes=6), 100.0, signal="WATCH")
    current = _scan(START + timedelta(minutes=12), 100.1)

    spot_reads = []
    def absent(*_args: object, **_kwargs: object) -> dict[str, observer.inputs.SpotRead]:
        spot_reads.append("cache")
        return {}

    monkeypatch.setattr(observer.inputs, "latest_spots", absent)
    try:
        source_store.ensure_live_tables(engine)
        for row in (old, first, missed, current):
            source_store.write_scan(engine, row, LIVE.policy_version)
        observer._run(engine, first, LIVE, cfg)
        observer._run(engine, current, LIVE, cfg)
        # The one-scan recovery limit advances only through the missed scan.
        assert store.latest_observed_at(engine, cfg.study_key) == missed.finished_at
        observer._run(engine, current, LIVE, cfg)
        observer._run(engine, current, LIVE, cfg)
        record, rows = store.detail_inputs(engine, cfg.study_key, OPPORTUNITY)
        assert record is not None
        case = observer.Case.model_validate_json(record.document)
        assert case.seed.published_at == first.finished_at
        assert case.positions[0].entry_at == missed.finished_at
        assert case.positions[1].entry_at == current.finished_at
        assert [observer.Observation.model_validate_json(row.document).recovered for row in rows] == [
            False, True, False,
        ]
        assert len(spot_reads) == 3
        assert case.positions[0].fill_risk == pytest.approx(76.25)
    finally:
        engine.dispose()



def test_dashboard_filters_case_list_and_counts_simultaneous_arm_states(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    try:
        observer.observe(engine, _scan(START, 100.0, spy=None), LIVE, CFG)
        observer.observe(engine, _scan(START + timedelta(minutes=6), 100.0, signal="WATCH"), LIVE, CFG)
        for state in (report.CaseFilter.OPEN, report.CaseFilter.PENDING, report.CaseFilter.UNKNOWN):
            result = report.build(engine, "nvda", 1, "", state=state, day=START.date())
            assert result.total == result.cohort_total == 1
            assert result.open_cases == result.pending_cases == result.unknown_cases == 1
            assert result.exit_quote_cases == 0
            assert result.filtered and result.filter_state == state.value
            assert result.cases[0].id == OPPORTUNITY
        mismatch = report.build(engine, "", 1, "", state=report.CaseFilter.OPEN, day=date(2026, 11, 23))
        assert mismatch.total == 0 and mismatch.cohort_total == 1
        assert mismatch.cases == ()
        assert report.build(engine, "", 1, "", state=report.CaseFilter.CLOSED).total == 0
    finally:
        engine.dispose()
