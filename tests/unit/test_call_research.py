"""Causal decisions and independent cash/reference arithmetic for daily proxies."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from webapp.calibration.model import Bar
from webapp.call_research.backtest import Signal, signals, simulate
from webapp.call_research.data import Dataset, load, prepare
from webapp.call_research.settings import Window, settings

DAYS = tuple(date(2026, 10, 19) + timedelta(days=i) for i in range(5))


def data() -> Dataset:
    bars = []
    for i, day in enumerate(DAYS):
        for name, close in [
            ("SPY", 100.0 - i),
            ("QQQ", 100.0),
            ("AAPL", 100.0 + i),
            ("AMD", 100.0 + i),
        ]:
            bars.append(Bar(name, day, close, max(close, 110.0), min(close, 90.0), close, 1000.0))
    return prepare(tuple(bars))


def test_signals_only_use_prices_available_at_their_decision_close() -> None:
    full = data()
    cutoff = DAYS[2]
    truncated = prepare(tuple(b for b in full.bars if b.day <= cutoff))
    assert signals(truncated) == tuple(s for s in signals(full) if s.decision_day <= cutoff)
    late = tuple(
        replace(b, close=b.close * 2, high=b.high * 2) if b.day > cutoff else b for b in full.bars
    )
    assert tuple(s for s in signals(prepare(late)) if s.decision_day <= cutoff) == signals(
        truncated
    )


def test_retest_and_control_have_the_same_next_day_decision_then_later_entry() -> None:
    candidates = signals(data())
    retest = next(s for s in candidates if s.arm == "retest" and s.ticker == "AAPL")
    control = next(s for s in candidates if s.arm == "delay_control" and s.ticker == "AAPL")
    assert (
        retest.signal_day == DAYS[1]
        and retest.decision_day == DAYS[2]
        and retest.entry_day == DAYS[3]
    )
    assert replace(retest, arm="delay_control") == control


def test_retest_does_not_fill_before_a_failed_close_confirmation() -> None:
    original = data()
    changed = tuple(
        replace(b, close=99.0, low=90.0) if b.ticker == "AAPL" and b.day == DAYS[2] else b
        for b in original.bars
    )
    candidates = signals(prepare(changed))
    assert any(
        s.arm == "delay_control" and s.signal_day == DAYS[1] and s.ticker == "AAPL"
        for s in candidates
    )
    assert not any(
        s.arm == "retest" and s.signal_day == DAYS[1] and s.ticker == "AAPL" for s in candidates
    )


def test_persistence_requires_every_exact_session_and_qqq_outperformance() -> None:
    original = data()
    assert any(s.arm == "persistence" for s in signals(original))
    missing = prepare(
        tuple(b for b in original.bars if not (b.ticker == "SPY" and b.day == DAYS[1]))
    )
    assert not any(s.arm == "persistence" for s in signals(missing))
    strong_qqq = prepare(
        tuple(
            replace(b, close=100.0 + 5 * DAYS.index(b.day), high=150.0) if b.ticker == "QQQ" else b
            for b in original.bars
        )
    )
    assert not any(s.arm == "persistence" for s in signals(strong_qqq))


def test_whole_shares_cash_and_costs_match_independent_arithmetic() -> None:
    original = data()
    bars = tuple(
        replace(b, open=100.0, close=110.0)
        if b.ticker == "AAPL"
        else replace(b, open=100.0, close=100.0)
        for b in original.bars
    )
    cfg = settings().model_copy(
        update={"capital_usd": 1000.0, "position_usd": 1000.0, "max_positions": 1}
    )
    f = Signal("AAPL", "baseline", DAYS[0], DAYS[0], DAYS[1], 0.01)
    result = simulate(
        prepare(bars),
        (f,),
        "baseline",
        Window(name="test", start=DAYS[0], end=DAYS[-1]),
        3,
        5.0,
        cfg,
    )
    assert result.closed == 1 and result.unresolved == 0
    t = result.trades[0]
    assert t.quantity == 9 and t.exit_day == DAYS[3]
    expected = 9 * 110 * 0.9995 - 9 * 100 * 1.0005
    assert t.pnl == pytest.approx(expected)
    reference = t.debit * (0.9995 / 1.0005 - 1)
    assert t.spy_excess == pytest.approx(expected - reference)
    assert t.qqq_excess == pytest.approx(expected - reference)
    assert result.equity[-1].cash == pytest.approx(1000 + expected)
    assert sum(e.spy_daily_excess_pp or 0 for e in result.equity) == pytest.approx(
        t.spy_excess / 1000 * 100
    )


def test_entries_cannot_reuse_cash_from_the_same_days_closing_exit() -> None:
    cfg = settings().model_copy(
        update={"capital_usd": 1000.0, "position_usd": 1000.0, "max_positions": 1}
    )
    first = Signal("AMD", "baseline", DAYS[0], DAYS[0], DAYS[1], 0.02)
    other = Signal("AAPL", "baseline", DAYS[0], DAYS[0], DAYS[1], 0.01)
    closing_day = replace(other, entry_day=DAYS[3])
    result = simulate(
        data(),
        (other, first, closing_day),
        "baseline",
        Window(name="test", start=DAYS[0], end=DAYS[-1]),
        1,
        5.0,
        cfg,
    )
    # For a one-session hold, the first entry still owns the slot until its close.
    assert result.trades[0].ticker == "AMD" and result.capacity_skips == 1
    result = simulate(
        data(),
        (first, replace(other, entry_day=DAYS[3])),
        "baseline",
        Window(name="test", start=DAYS[0], end=DAYS[-1]),
        3,
        5.0,
        cfg,
    )
    # The later candidate would cross the boundary, so extend this independent window.
    assert result.boundary_excluded == 1


def test_missing_exact_exit_stays_unresolved_instead_of_using_a_later_close() -> None:
    original = data()
    broken = prepare(
        tuple(b for b in original.bars if not (b.ticker == "AAPL" and b.day == DAYS[2]))
    )
    f = Signal("AAPL", "baseline", DAYS[0], DAYS[0], DAYS[1], 0.01)
    result = simulate(
        broken, (f,), "baseline", Window(name="test", start=DAYS[0], end=DAYS[-1]), 2, 5.0
    )
    assert result.unresolved == 1 and result.closed == 0 and result.trades[0].exit_day is None
    assert result.return_pct is None and result.spy_excess_usd is None
    assert result.equity[-1].cash == pytest.approx(settings().capital_usd - result.trades[0].debit)


def test_no_pre_split_signal_can_enter_the_review_window() -> None:
    f = Signal("AAPL", "baseline", DAYS[0], DAYS[0], DAYS[1], 0.01)
    result = simulate(
        data(), (f,), "baseline", Window(name="review", start=DAYS[1], end=DAYS[-1]), 1, 5.0
    )
    assert result.signals == 0 and result.trades == ()


def test_duplicate_and_reserved_sources_are_rejected_before_reading_prices(tmp_path: Path) -> None:
    original = data()
    with pytest.raises(ValueError, match="duplicate"):
        prepare((*original.bars, original.bars[0]))
    with pytest.raises(ValueError, match="reserved"):
        load(tmp_path / "study_g" / "never-read.csv", expected_sha="not-used")
    invalid = tmp_path / "changed.csv"
    invalid.write_text("not the registered source")
    with pytest.raises(ValueError, match="checksum"):
        load(invalid)


def test_bootstrap_is_reproducible_and_keeps_missing_data_unknown() -> None:
    from webapp.call_research.offline import interval

    values = tuple(float(i) / 100 for i in range(40))
    assert interval(values, 1) == interval(values, 1)
    assert interval((*values, None), 1) is None
    assert interval((0.0,) * 20, 1) == (0.0, 0.0)


def test_real_144_ledgers_reconcile_to_source_prices_costs_cash_and_references() -> None:
    import gzip
    import hashlib
    import json
    import math

    from webapp.call_research import report

    summary = report.load()
    raw = (report.DIRECTORY / "replays.jsonl.gz").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == summary["ledger_sha256"]
    records = [json.loads(line) for line in gzip.decompress(raw).splitlines()]
    assert len(records) == summary["replays"] == 144
    prices = {(b.ticker, b.day.isoformat()): b for b in load().bars}
    for row in records:
        slip = row["cost_bps"] / 10000
        debit = credit = spy = qqq = 0.0
        for trade in row["trades"]:
            first = prices[trade["ticker"], trade["entry_day"]]
            assert trade["quantity"] > 0 and isinstance(trade["quantity"], int)
            expected_debit = trade["quantity"] * first.open * (1 + slip)
            assert trade["debit"] == pytest.approx(expected_debit)
            assert expected_debit <= settings().position_usd + 1e-8
            debit += expected_debit
            if trade["exit_day"] is None:
                continue
            last = prices[trade["ticker"], trade["exit_day"]]
            expected_credit = trade["quantity"] * last.close * (1 - slip)
            credit += expected_credit
            own = expected_credit - expected_debit
            assert trade["pnl"] == pytest.approx(own)
            for benchmark, key in [("SPY", "spy_excess"), ("QQQ", "qqq_excess")]:
                start = prices[benchmark, trade["entry_day"]].open
                end = prices[benchmark, trade["exit_day"]].close
                expected = own - expected_debit * (end * (1 - slip) / (start * (1 + slip)) - 1)
                assert trade[key] == pytest.approx(expected)
                if key == "spy_excess":
                    spy += expected
                else:
                    qqq += expected
        assert row["equity"][-1]["cash"] == pytest.approx(settings().capital_usd - debit + credit)
        if not row["unresolved"]:
            assert row["net_usd"] == pytest.approx(credit - debit)
            assert row["spy_excess_usd"] == pytest.approx(spy)
            assert row["qqq_excess_usd"] == pytest.approx(qqq)
            assert math.fsum(
                p["spy_daily_excess_pp"] for p in row["equity"]
            ) / 100 * settings().capital_usd == pytest.approx(spy)
        cash = settings().capital_usd
        for point in row["equity"]:
            entering = [t for t in row["trades"] if t["entry_day"] == point["day"]]
            exiting = [t for t in row["trades"] if t["exit_day"] == point["day"]]
            cash -= sum(t["debit"] for t in entering)
            assert cash >= -1e-8  # Closing proceeds have not yet become available.
            active = [
                t
                for t in row["trades"]
                if t["entry_day"] <= point["day"]
                and (t["exit_day"] is None or t["exit_day"] >= point["day"])
            ]
            assert len(active) <= settings().max_positions
            assert len({t["ticker"] for t in active}) == len(active)
            cash += sum(t["credit"] for t in exiting)
            assert point["cash"] == pytest.approx(cash)
    assert summary["live_promotion"] is False
