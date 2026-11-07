"""Independent clocks, held-out boundaries, mirror costs and frozen-grid accounting."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError
from webapp.calibration.model import Bar
from webapp.calibration.settings import calendar
from webapp.fusion_research import features, report, search
from webapp.fusion_research.model import (
    Candidate,
    FeatureRow,
    Observation,
    OutcomeRow,
    digest,
    grid,
    ticker_estimates,
)
from webapp.fusion_research.replay import Metrics, outcomes, replay
from webapp.fusion_research.settings import Fold, settings

from uoa_detector.live_alpha.calendar import is_session_day

AT = datetime(2026, 7, 1, 15, tzinfo=UTC)


@pytest.fixture(scope="module")
def bars():
    cfg = settings()
    day = cfg.source_start
    days = []
    while day <= cfg.source_end:
        if is_session_day(day, calendar()) is True:
            days.append(day)
        day += timedelta(days=1)
    out = []
    for i, d in enumerate(days):
        for ticker, base, movement in [
            ("SPY", 500.0, 0.04),
            ("AAA", 100.0, 0.12),
            ("BBB", 120.0, -0.05),
        ]:
            op = base + i * movement + np.sin(i / 5) * 0.4
            close = op + (0.3 if ticker != "BBB" else -0.2)
            out.append(
                Bar(ticker, d, op, max(op, close) + 1, min(op, close) - 1, close, 1000.0 + i % 17)
            )
    return tuple(out)


@pytest.fixture(scope="module")
def prepared(bars):
    return features.prepare(bars)


def label(ticker, index, day, net=1.0, *, known=None):
    entry = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=14)
    exit_ = entry + timedelta(minutes=15)
    return OutcomeRow(
        event_id=f"{ticker}-{index}",
        ticker=ticker,
        session=day,
        method="paired_reference",
        instrument="stock",
        horizon="15m",
        state="observed",
        entry_at=entry,
        exit_at=exit_,
        entry_known_at=entry + timedelta(seconds=1),
        exit_known_at=exit_ + timedelta(seconds=1),
        mature_at=exit_ + timedelta(seconds=1),
        known_at=known or exit_ + timedelta(seconds=1),
        net_usd=net,
        excess_usd=net,
    )


def test_registered_grid_has_exact_unique_trial_budget():
    values = grid()
    assert len(values) == len({c.key for c in values}) == 5184
    assert len({c.family for c in values}) == 6
    assert len({c.lookback for c in values}) == 4
    assert {c.direction for c in values} == {"up", "down"}
    assert {c.horizon for c in values} == {1, 3, 5}
    assert settings().source_end == date(2026, 9, 18)


def test_reserved_path_refused_before_any_file_read(tmp_path, monkeypatch):
    target = tmp_path / "study_g" / "bars.csv"
    monkeypatch.setattr(Path, "open", lambda *_a, **_k: pytest.fail("reserved file opened"))
    with pytest.raises(ValueError, match="reserved"):
        features.load(target)


def test_postcutoff_date_is_rejected_before_numeric_label_parsing(tmp_path):
    p = tmp_path / "bars.csv"
    p.write_text(
        "ticker,day,open,high,low,close,volume\nAAA,2026-09-21,UNREAD,UNREAD,UNREAD,UNREAD,UNREAD\n"
    )
    with pytest.raises(ValueError, match="registered reused window"):
        features.load(p)


def test_optional_modality_unknowns_never_become_zero_or_false():
    known = Observation(
        name="direction", value=False, known_at=AT, source="fixture", source_hash=digest("source")
    )
    row = FeatureRow(
        event_id="e", ticker="AAA", session=AT.date(), event_at=AT, known_at=AT, news=(known,)
    )
    assert row.usable(AT, (("news", "direction"),)) == (True, ())
    assert row.usable(AT, (("fundamental", "surprise"),)) == (
        False,
        ("fundamental.surprise:unknown",),
    )
    with pytest.raises(ValidationError):
        Observation(name="absent")


def test_future_receipts_cannot_enter_a_past_event_or_trade():
    feature = Observation(
        name="sentiment",
        value="up",
        known_at=AT + timedelta(seconds=1),
        source="fixture",
        source_hash=digest("source"),
    )
    with pytest.raises(ValidationError, match="row cannot precede"):
        FeatureRow(
            event_id="e", ticker="AAA", session=AT.date(), event_at=AT, known_at=AT, news=(feature,)
        )
    row = FeatureRow(
        event_id="e",
        ticker="AAA",
        session=AT.date(),
        event_at=AT,
        known_at=AT + timedelta(seconds=1),
        news=(feature,),
    )
    assert row.usable(AT, (("news", "sentiment"),)) == (False, ("event_not_yet_known",))


def test_label_requires_actual_maturity_and_null_is_not_zero():
    row = label("AAA", 1, AT.date(), net=0.0)
    assert row.available(row.known_at)
    assert not row.available(row.known_at - timedelta(microseconds=1))
    with pytest.raises(ValidationError, match="unknown label"):
        OutcomeRow(
            event_id="missing",
            ticker="AAA",
            session=AT.date(),
            method="paired",
            instrument="stock",
            horizon="15m",
            state="missing",
            net_usd=0.0,
            missing=("no_exit",),
        )
    with pytest.raises(ValidationError):
        row.model_copy(update={"exit_at": AT - timedelta(days=2)}).model_validate(
            row.model_dump() | {"exit_at": AT - timedelta(days=2)}
        )


def test_ticker_estimates_shrink_using_training_only_and_require_validation_support(prepared):
    train = []
    valid = []
    for i in range(60):
        train.append(label("AAA", i, prepared.days[i // 3], 1.0 if i < 30 else -1.0))
        train.append(label("BBB", i, prepared.days[i // 3], -1.0))
    for i in range(20):
        valid.append(label("AAA", 60 + i, prepared.days[30 + i // 2], 999.0))
    values = ticker_estimates(tuple(train), tuple(valid), AT)
    a, b = values
    assert a.mode == "SHRUNK_TICKER" and a.weight == 0.5
    assert a.training_win_fraction == pytest.approx(0.375)
    assert b.mode == "GLOBAL_FALLBACK" and b.training_win_fraction == pytest.approx(0.25)
    changed = tuple(r.model_copy(update={"net_usd": -999.0}) for r in valid)
    assert ticker_estimates(tuple(train), changed, AT) == values
    assert a.calibrated_success_probability is None


def test_mixed_label_panels_and_overlap_are_refused():
    a = label("AAA", 1, date(2026, 1, 5))
    b = label("AAA", 2, date(2026, 1, 6))
    with pytest.raises(ValueError, match="overlap"):
        ticker_estimates((a,), (a,), AT)
    with pytest.raises(ValueError, match="cannot be pooled"):
        ticker_estimates((a,), (b.model_copy(update={"horizon": "60m"}),), AT)


def test_future_validation_labels_do_not_create_ticker_support(prepared):
    rows = tuple(label("AAA", i, prepared.days[i // 3]) for i in range(60))
    future = tuple(
        label("AAA", 60 + i, prepared.days[30 + i // 2], known=AT + timedelta(days=1))
        for i in range(20)
    )
    est = ticker_estimates(rows, future, AT)[0]
    assert est.mode == "GLOBAL_FALLBACK" and est.validation_units == 0


def test_changing_future_bars_does_not_change_prefix_features_or_rosters(bars, prepared):
    cutoff = prepared.days[140]
    changed = tuple(
        replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2)
        if b.day > cutoff
        else b
        for b in bars
    )
    after = features.prepare(changed)
    for key in prepared.scores:
        np.testing.assert_array_equal(prepared.scores[key][:141], after.scores[key][:141])
        np.testing.assert_array_equal(prepared.conditions[key][:141], after.conditions[key][:141])
    c = Candidate("relative_momentum", 3, 0.0, 0.0, "any", "up", 1)
    first, second = features.roster(prepared, c), features.roster(after, c)
    assert list(zip(first[0][first[0] <= 140], first[1][first[0] <= 140], strict=True)) == list(
        zip(second[0][second[0] <= 140], second[1][second[0] <= 140], strict=True)
    )


def test_calendar_gap_is_not_compressed_into_a_shorter_horizon(bars, prepared):
    absent = prepared.days[90]
    changed = features.prepare(
        tuple(b for b in bars if not (b.ticker == "SPY" and b.day == absent))
    )
    assert changed.days == prepared.days
    assert not changed.valid[90, changed.tickers.index("SPY")]
    assert not any(np.any(v[90:111]) for v in changed.conditions.values())


def test_roster_has_four_units_per_day_and_excludes_spy(prepared):
    c = Candidate("relative_momentum", 3, 0.0, 0.0, "any", "up", 1)
    rr, cc = features.roster(prepared, c)
    assert not np.any(cc == prepared.tickers.index("SPY"))
    assert np.max(np.bincount(rr, minlength=len(prepared.days))) <= 4


def arithmetic_panel(prepared):
    op = prepared.open.copy()
    cl = prepared.close.copy()
    i = 40
    j = prepared.tickers.index("AAA")
    spy = prepared.tickers.index("SPY")
    op[i + 1, j] = 100.0
    cl[i + 1, j] = 103.0
    op[i + 1, spy] = 500.0
    cl[i + 1, spy] = 510.0
    return replace(prepared, open=op, close=cl), i, j


def test_long_and_short_cost_math_is_independent_of_sign_flip(prepared):
    p, i, j = arithmetic_panel(prepared)
    long = outcomes(p, "up", 1, 5.0)
    short = outcomes(p, "down", 1, 5.0)
    assert long.quantity[i, j] == short.quantity[i, j] == 24
    assert long.net[i, j] == pytest.approx(69.564)
    assert short.net[i, j] == pytest.approx(-74.436)
    assert long.excess[i, j] == pytest.approx(23.988)
    assert long.net[i, j] + short.net[i, j] == pytest.approx(-2 * long.cost[i, j])


def test_missing_or_invalid_future_bar_is_unknown_and_never_a_zero_loss(prepared):
    p, i, j = arithmetic_panel(prepared)
    valid = p.valid.copy()
    valid[i + 1, j] = False
    p = replace(p, valid=valid)
    c = Candidate("relative_momentum", 3, 0.0, 0.0, "any", "up", 1)
    rr, cc = np.array([i], dtype=np.int64), np.array([j], dtype=np.int64)
    r = replay(p, c, (rr, cc), outcomes(p, "up", 1, 5.0), p.days[i], p.days[i + 1], ledger=True)
    assert r.metrics.selected == 1 and r.metrics.resolved == 0 and r.metrics.missing == 1
    assert r.metrics.net_usd is None and r.metrics.excess_usd is None
    assert r.ledger[0].net_usd is None and np.isnan(r.daily_excess[0])


def test_fold_end_excludes_unmatured_horizon_instead_of_using_next_fold_prices(prepared):
    p, i, j = arithmetic_panel(prepared)
    c = Candidate("relative_momentum", 3, 0.0, 0.0, "any", "up", 5)
    rr, cc = np.array([i], dtype=np.int64), np.array([j], dtype=np.int64)
    r = replay(p, c, (rr, cc), outcomes(p, "up", 5, 5.0), p.days[i], p.days[i + 3])
    assert r.metrics.boundary_excluded == 1 and r.metrics.selected == 0
    assert r.metrics.net_usd == 0.0


def test_shared_split_manifest_has_max_horizon_purge_plus_embargo(prepared, monkeypatch):
    manifest = search.split_manifest(prepared.days)
    assert all(len(f["gap_sessions"]) >= 6 for f in manifest["folds"])
    assert len(manifest["final_gap_sessions"]) >= 6
    cfg = settings()
    bad = cfg.model_copy(
        update={
            "folds": (
                Fold(
                    train_end=date(2026, 1, 30),
                    validation_start=date(2026, 2, 2),
                    validation_end=date(2026, 3, 20),
                ),
            )
        }
    )
    monkeypatch.setattr(search, "settings", lambda: bad)
    with pytest.raises(ValueError, match="purge"):
        search.split_manifest(prepared.days)


def metric(score):
    return Metrics(100, 100, 0, 0, 0, 30, 100.0, 100.0, score, 60, 40, 105.0, 5.0)


def test_selection_reads_training_metrics_only_and_breaks_ties_deterministically():
    a, b = grid()[:2]
    assert search.select_training(((a, metric(1.0)), (b, metric(2.0)))) == b
    assert search.select_training(((a, metric(1.0)), (b, metric(1.0)))) == min(
        (a, b), key=lambda c: c.key
    )
    assert search.select_training(((a, replace(metric(2.0), missing=1)),)) is None


def test_joint_resampling_uses_shared_dates_and_does_not_count_duplicates_as_independent():
    base = np.array([[1.0, -1.0, 2.0, -2.0, 1.0, -1.0], [-1.0, 2.0, -1.0, 2.0, -1.0, 2.0]])
    a = search.joint_resampling(base, (3, 3))
    b = search.joint_resampling(np.vstack((base, base[0])), (3, 3))
    assert a["maximum_centered_mean_draws_usd"] == b["maximum_centered_mean_draws_usd"]
    assert b["registered_vectors"] == 3 and b["unique_return_vectors"] == 2
    assert a["formal_significance_claim"] is False


def write_source(path, bars):
    with path.open("w") as f:
        w = csv.writer(f)
        w.writerow(("ticker", "day", "open", "high", "low", "close", "volume"))
        for b in bars:
            w.writerow((b.ticker, b.day, b.open, b.high, b.low, b.close, b.volume))


def test_plan_binds_source_grid_code_and_splits_before_returns(tmp_path, bars):
    p = tmp_path / "source.csv"
    write_source(p, bars)
    plan = search.plan(p)
    assert plan["candidate_count"] == 5184 and len(plan["candidate_keys"]) == 5184
    assert plan["maximum_run_seconds"] == 900
    p.write_text(p.read_text() + "\n")
    with pytest.raises(ValueError, match="changed after registration"):
        search.run(p, plan)


def test_bounded_stored_report_unavailable_without_schema_or_job(tmp_path):
    body = report.load(tmp_path)
    assert (
        body["status"] == "UNAVAILABLE"
        and body["winner"] is None
        and body["inference"] == "NOT_READY"
    )
    assert not list(tmp_path.iterdir())


def test_synthetic_end_to_end_sealed_plan_all_trials_controls_and_readonly_report(
    tmp_path, bars, monkeypatch
):
    # A deliberately small synthetic registry exercises orchestration; it is not
    # counted as part of the actual 5,184-cell market experiment.
    configs = grid()[:6]
    cfg = settings().model_copy(update={"registered_configurations": len(configs)})
    monkeypatch.setattr(search, "grid", lambda: configs)
    monkeypatch.setattr(search, "settings", lambda: cfg)
    monkeypatch.setattr(report, "settings", lambda: cfg)
    source = tmp_path / "synthetic.csv"
    write_source(source, bars)
    registered = search.plan(source)
    folder = tmp_path / "run"
    folder.mkdir()
    import json

    (folder / "plan.json").write_text(json.dumps(registered))
    progress = []
    result = search.run(source, registered, progress=progress.append)
    assert result["configuration_trials"] == 6 and len(result["grid"]) == 6
    assert result["reference_replay_evaluations"] >= 42
    assert result["winner"] is None and result["live_promotion"] is False
    assert result["inference"] == "NOT_READY" and result["fresh_sessions_in_this_dataset"] == 0
    assert result["joint_resampling"]["registered_vectors"] == 6
    assert progress[-1]["completed_configurations"] == 6
    report.write(folder, result)
    loaded = report.load(folder)
    assert loaded["status"] == "EXPLORATORY_COMPLETE"
    assert loaded["grid_artifact"]["rows"] == 6 and "grid" not in loaded
    snapshot = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert report.load(folder) == loaded
    assert snapshot == {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(ValueError, match="overwrite"):
        report.write(folder, result)
    (folder / "grid.jsonl.gz").write_bytes(b"tampered")
    assert report.load(folder)["status"] == "UNAVAILABLE"
