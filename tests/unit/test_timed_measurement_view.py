"""Recorded methods and physical clocks stay distinct in the read-only report."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from webapp.alpha_lab.db import digest, dumps
from webapp.board.db import make_engine
from webapp.live_alpha import store as live_storage
from webapp.opening import storage as opening_storage
from webapp.shared_news import storage as shared_storage
from webapp.timed_measurement import storage, view
from webapp.timed_measurement.config import profile_hash
from webapp.timed_measurement.model import Activation, Assessment, Completion, LegEvidence, Target

from tests.unit.test_opening_desk import T0, persist, source


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    result = make_engine(f"sqlite:///{tmp_path / 'timed-report.db'}")
    storage.ensure(result)
    yield result
    result.dispose()


def activate(engine: Engine) -> Activation:
    return storage.activate(engine, clock=lambda: T0 - timedelta(minutes=1))


def target(activation: Activation | None = None, *, key: str = "target", minutes: int = 15) -> Target:
    if activation is None:
        activation = Activation(key="activation", profile_hash=profile_hash(), opening_profile_hash="opening", activated_at=T0 - timedelta(minutes=1))
    return Target(
        key=key, activation_key=activation.key, profile_hash=activation.profile_hash,
        opening_profile_hash=activation.opening_profile_hash, primary_key="first", decision_key="call",
        ticker="NVDA", day=T0.date(), direction="up", instrument="stock", published_at=T0,
        entry_reference=100, entry_source_at=T0, spy_entry=500, spy_entry_source_at=T0,
        source_scan_id="scan", source_snapshot_hash="snapshot", source_card_hash="card",
        horizon=f"m{minutes}", horizon_minutes=minutes, target_at=T0 + timedelta(minutes=minutes),
        deadline=T0 + timedelta(minutes=minutes, seconds=300), eligible=True, reason="", discovered_at=T0,
    )


def evidence(t: Target, *, own_price: float = 110) -> tuple[tuple[LegEvidence, ...], Completion, Assessment]:
    def leg(kind: str, at: int, price: float) -> LegEvidence:
        return LegEvidence(
            key=kind, attempt_key="attempt", leg=kind, ticker=t.ticker if kind == "own" else "SPY",
            status="ok", reason="", requested_at=t.target_at + timedelta(seconds=at),
            received_at=t.target_at + timedelta(seconds=at + 1), receipt_at=t.target_at + timedelta(seconds=at + 2),
            confirmed_at=t.target_at + timedelta(seconds=at + 3), price=price,
            trade_at=t.target_at + timedelta(seconds=at), http_status=200,
            payload_hash="packet", wire_body_hash="wire", wire_attempted=True,
            market_phase="regular", source_path=f"/api/stock/{t.ticker if kind == 'own' else 'SPY'}/quote",
        )
    legs = (leg("own", 2, own_price), leg("spy", 8, 505))
    completion = Completion(
        key="attempt", attempt_key="attempt", target_key=t.key, ordinal=1,
        completed_at=t.target_at + timedelta(seconds=15), confirmed_at=t.target_at + timedelta(seconds=16),
        own_key="own", spy_key="spy", reason="", own_confirmed_at=t.target_at + timedelta(seconds=6),
        spy_confirmed_at=t.target_at + timedelta(seconds=12),
    )
    own_pct = (own_price / 100 - 1) * 100
    assessed = Assessment(key=t.key, target_key=t.key, assessed_at=t.target_at + timedelta(seconds=17),
                          status="observed", reason="paired_reference", own_key="own", spy_key="spy",
                          return_pct=own_pct, excess_pp=own_pct - 1)
    return legs, completion, assessed


def test_empty_missing_and_initialized_storage_never_create_results(tmp_path: Path, engine: Engine) -> None:
    empty = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    missing = view.build(empty, T0)
    assert missing.status == "STORAGE_UNAVAILABLE"
    assert missing.summary.eligible is missing.summary.own_observed is None
    assert missing.summary.own_coverage_pct is None
    assert missing.horizons == ()  # No made-up zero target denominators without storage.
    assert missing.first_live_observation_pending is None
    assert opening_storage.OpeningPrimary.__tablename__ not in inspect(empty).get_table_names()
    initialized = view.build(engine, T0)
    assert initialized.status == "INACTIVE" and initialized.rows == ()
    assert initialized.summary.eligible == 0 and initialized.summary.mature == 0
    assert initialized.summary.own_coverage_pct is None
    assert initialized.paper == initialized.legacy == ()
    empty.dispose()


def test_only_stored_values_use_matching_actual_confirmation_clocks() -> None:
    t = target()
    legs, completion, assessed = evidence(t)
    result = view._row(t, assessed, legs, t.target_at + timedelta(seconds=20), (completion,))
    assert result.state == "complete"
    assert result.own_pct == pytest.approx(10) and result.excess_pp == pytest.approx(9)
    assert result.own_confirmed_at == completion.own_confirmed_at
    assert result.spy_confirmed_at == completion.spy_confirmed_at
    assert result.own_source_age_seconds == 4
    assert result.own_recording_lag_seconds == 3
    assert result.exit_skew_seconds == 6
    counts = view._coverage((result,), 1)
    assert counts.mature == counts.own_observed == counts.paired == 1
    assert counts.own_coverage_pct == counts.paired_coverage_pct == 100


@pytest.mark.parametrize("case", ["absent_completion", "late_actual_confirmation", "aged_actual_confirmation", "different_attempt", "future_assessment"])
def test_unconfirmed_or_late_records_never_become_a_paired_result(case: str) -> None:
    t = target()
    legs, completion, assessed = evidence(t)
    completions = (completion,)
    if case == "absent_completion":
        completions = ()
    elif case == "late_actual_confirmation":
        completions = (completion.model_copy(update={"own_confirmed_at": t.deadline + timedelta(seconds=1)}),)
    elif case == "aged_actual_confirmation":
        completions = (completion.model_copy(update={"own_confirmed_at": t.target_at + timedelta(seconds=63)}),)
    elif case == "different_attempt":
        legs = (legs[0], legs[1].model_copy(update={"attempt_key": "foreign"}))
    else:
        assessed = assessed.model_copy(update={"assessed_at": t.deadline + timedelta(hours=1)})
    result = view._row(t, assessed, legs, t.deadline + timedelta(seconds=10), completions)
    assert result.excess_pp is None
    if case != "different_attempt":
        assert result.own_pct is None
    assert result.state in {"unknown", "partial", "assessment_pending"}


def test_missing_entry_and_exit_spy_are_different_and_not_zero() -> None:
    t = target()
    legs, completion, assessed = evidence(t)
    missing_entry = view._row(t.model_copy(update={"spy_entry": None}),
        assessed.model_copy(update={"status": "partial", "excess_pp": None, "reason": "missing_entry_spy"}),
        legs, t.deadline, (completion,))
    assert missing_entry.own_pct == pytest.approx(10) and missing_entry.excess_pp is None
    assert missing_entry.entry_spy_missing and not missing_entry.exit_spy_missing
    missing_exit = view._row(t, assessed.model_copy(update={"status": "partial", "spy_key": None, "excess_pp": None, "reason": "missing_exit_spy"}),
                            legs[:1], t.deadline, (completion,))
    assert not missing_exit.entry_spy_missing and missing_exit.exit_spy_missing
    assert missing_exit.excess_pp is None and missing_exit.own_pct == pytest.approx(10)
    assert missing_entry.reasons != missing_exit.reasons


def test_real_zero_and_horizon_denominators_survive_without_pooling() -> None:
    t = target()
    legs, completion, assessed = evidence(t, own_price=100)
    first = view._row(t, assessed, legs, t.deadline, (completion,))
    assert first.own_pct == 0 and first.excess_pp == -1
    future = view._row(target(key="sixty", minutes=60), None, (), t.deadline)
    summaries = view._horizons((first, future))
    assert summaries[0].mean_pct == 0 and summaries[0].own_observed == 1
    assert summaries[1].mean_pct is None and summaries[1].mature == 0
    assert summaries[1].own_coverage_pct is None


def test_unknown_and_rejected_leg_are_not_a_missing_trade_or_loss() -> None:
    t = target()
    legs, completion, assessed = evidence(t)
    failed = legs[0].model_copy(update={"status": "invalid_source", "reason": "last_trade_outside_window"})
    result = view._row(t, assessed.model_copy(update={"status": "unknown", "reason": "incomplete_attempt", "return_pct": None, "excess_pp": None}),
                       (failed,), t.deadline, (completion,))
    coverage = view._coverage((result,), 1)
    assert result.state == "unknown" and result.own_pct is result.excess_pp is None
    assert coverage.unknown == coverage.rejected == 1 and coverage.missing == 0
    assert any("seans/tazelik" in reason for reason in result.reasons)


def test_report_is_select_only_and_does_not_call_initializers(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    activate(engine)
    storage.save_target(engine, target(storage.activation(engine, T0)))
    monkeypatch.setattr(storage, "ensure", lambda *_: pytest.fail("GET cannot initialize"))
    writes: list[str] = []
    def execute(_c: object, _cursor: object, sql: str, _parameters: object, _context: object, _many: bool) -> None:
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER")):
            writes.append(sql)
    event.listen(engine, "before_cursor_execute", execute)
    try:
        report = view.build(engine, T0, T0.date())
        assert report.summary.pending == 1 and report.summary.own_coverage_pct is None
        assert writes == []
    finally:
        event.remove(engine, "before_cursor_execute", execute)


def test_sample_bound_and_current_profile_are_explicit(engine: Engine) -> None:
    active = activate(engine)
    for index in range(201):
        storage.save_target(engine, target(active, key=f"target-{index}"))
    foreign = target(active, key="foreign").model_copy(update={"activation_key": "old", "profile_hash": "old"})
    storage.save_target(engine, foreign)
    report = view.build(engine, T0)
    assert report.limited and report.status == "LIMITED"
    assert len(report.rows) == report.summary.eligible == 200
    assert all(row.key != "foreign" for row in report.rows)
    assert report.summary.own_observed == 0 and report.summary.own_coverage_pct is None


def test_unverified_numeric_assessment_cannot_prove_first_live_success(engine: Engine) -> None:
    active = activate(engine)
    t = target(active)
    storage.save_target(engine, t)
    _, _, assessed = evidence(t)
    storage.assess(engine, assessed)
    report = view.build(engine, t.deadline)
    assert report.first_live_observation_pending is None
    assert report.summary.own_observed == 0 and report.rows[0].own_pct is None
    assert report.rows[0].state == "unknown"


@pytest.mark.parametrize("corrupt", ["target", "assessment"])
def test_corrupt_record_makes_population_unknown_instead_of_empty_or_zero(engine: Engine, corrupt: str) -> None:
    active = activate(engine)
    t = target(active)
    storage.save_target(engine, t)
    _, _, assessed = evidence(t)
    storage.assess(engine, assessed)
    with engine.begin() as connection:
        if corrupt == "target":
            connection.execute(update(storage.TimedTarget).where(storage.TimedTarget.key == t.key)
                               .values(payload_json=t.model_copy(update={"entry_reference": 999}).model_dump_json()))
        else:
            connection.execute(update(storage.TimedAssessment).where(storage.TimedAssessment.target_key == t.key)
                               .values(payload_json=assessed.model_copy(update={"return_pct": 999}).model_dump_json()))
    report = view.build(engine, t.deadline)
    assert report.status == "UNVERIFIED" and "bütünlüğü doğrulanamadı" in report.reason
    assert report.summary.eligible is report.summary.mature is report.summary.own_observed is None
    assert report.rows == report.horizons == ()
    assert report.first_live_observation_pending is None
    assert report.summary.own_coverage_pct is report.summary.paired_coverage_pct is None


def test_preactivation_first_and_later_update_do_not_inherit_new_results(engine: Engine) -> None:
    live_storage.ensure_live_tables(engine)
    opening_storage.ensure(engine)
    persist(engine, source())
    persist(engine, source(T0 + timedelta(minutes=1), "update"))
    with Session(engine) as session:
        first = session.scalar(select(opening_storage.OpeningPrimary).where(opening_storage.OpeningPrimary.kind == "entry"))
        assert first is not None and first.decision_key
        keys = list(session.scalars(select(opening_storage.OpeningDecision.key).where(opening_storage.OpeningDecision.action == "BUY").order_by(opening_storage.OpeningDecision.published_at)))
    storage.activate(engine, clock=lambda: T0 + timedelta(minutes=2))
    original = view.build_call(engine, T0 + timedelta(minutes=3), first.decision_key)
    update = view.build_call(engine, T0 + timedelta(minutes=3), keys[-1])
    assert original.status == "PRE_ACTIVATION" and original.first and original.rows == ()
    assert update.status == "UPDATE_NOT_ELIGIBLE" and update.first is False
    assert update.canonical_key == first.decision_key and update.rows == ()


def test_legacy_gross_and_paper_policy_instrument_cohorts_stay_separate(engine: Engine) -> None:
    live_storage.ensure_live_tables(engine)
    opening_storage.ensure(engine)
    persist(engine, source())
    close = T0.replace(hour=20, minute=0)
    with Session(engine) as session:
        first = session.scalar(select(opening_storage.OpeningPrimary).where(opening_storage.OpeningPrimary.kind == "entry"))
        assert first is not None
        session.add(opening_storage.OpeningOutcome(key=digest((first.key, "c0")), primary_key=first.key, horizon="c0", observed_at=close + timedelta(minutes=2),
            payload_json=dumps(dict(status="complete", target_at=close, observed_at=close + timedelta(minutes=2), directional_underlying_pct=10, directional_excess_pp=9, spy_pct=1))))
        for identity, instrument, policy, pnl in [("paper-a", "stock", "paper_one", 500), ("paper-b", "long_call", "paper_two", -1), ("paper-unknown", "stock", "paper_one", None)]:
            session.add(live_storage.LivePaper(paper_id=identity, opportunity_id=identity, rec_id="rec", ticker="NVDA", direction="up", instrument=instrument,
                plan_json=dumps(dict(policy_version=policy)), state="CLOSED_SIMULATED", created_at=T0, entry_at=T0, entry_price=100, quantity=1,
                exit_at=close, exit_price=101, exit_reason="fixture", pnl_usd=pnl))
        session.commit()
    report = view.build(engine, close + timedelta(minutes=3), T0.date())
    assert report.summary.first_calls == 1
    assert report.legacy[0].method == "legacy_gross_c0" and report.legacy[0].mean_pct == 10
    assert report.legacy[0].mean_excess_pp == 9 and report.legacy[0].paired == 1
    assert {(g.policy, g.instrument) for g in report.paper} == {("paper_one", "stock"), ("paper_two", "long_call")}
    assert {g.pnl_usd for g in report.paper} == {500, -1}
    assert next(g for g in report.paper if g.policy == "paper_one").unknown == 1
    assert not hasattr(report, "equity")


def test_london_default_and_paper_day_are_explicit(engine: Engine) -> None:
    # Standard-time London and the summer one-hour offset use their own dates.
    now = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)
    report = view.build(engine, now)
    assert report.day == date(2026, 10, 31)
    summer = view.build(engine, datetime(2026, 8, 1, 23, 30, tzinfo=UTC))
    assert summer.day == date(2026, 8, 2)
    assert "ABD seans günü" in report.day_basis and "Europe/London" in report.paper_day_basis
    with pytest.raises(ValueError):
        view.build(engine, T0.replace(tzinfo=None))


def test_stored_news_failure_is_not_a_connected_source(engine: Engine) -> None:
    shared_storage.ensure(engine)
    shared_storage.append(engine, "mahalle", T0, T0 + timedelta(seconds=1), None, "transport_error", clock=lambda: T0 + timedelta(seconds=2))
    report = view.build(engine, T0 + timedelta(seconds=3))
    mahalle = next(feed for feed in report.news_feeds if feed.provider == "mahalle")
    assert mahalle.status == "unknown" and "başarılı değil" in mahalle.reason
    assert mahalle.last_received_at == T0 + timedelta(seconds=1)
    assert mahalle.last_confirmed_at == T0 + timedelta(seconds=2)
    assert len(report.news_research.arms) == 5 and report.news_research.inference == "NOT_PERFORMED"
