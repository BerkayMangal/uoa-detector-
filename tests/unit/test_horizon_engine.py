"""Synthetic fixed-roster timing, candle, cost and missingness contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from webapp.horizon_research.engine import Tape, _bounds, run
from webapp.horizon_research.model import Candle, Signal, Source

DAYS = tuple(date(2026, 8, day) for day in (3, 4, 5, 6, 7, 10, 11, 12))
AT = datetime(2026, 8, 3, 14, 15, tzinfo=UTC)


def signal(key: str = "base", arm: str = "baseline_proxy", at: datetime = AT) -> Signal:
    return Signal(key, arm, "NVDA", at.date(), at)


def candle(
    ticker: str,
    at: datetime,
    price: float,
    *,
    close: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1,
) -> Candle:
    final = price + 0.005 if close is None else close
    return Candle(
        ticker,
        at.date(),
        at,
        price,
        max(price, final) + 0.02 if high is None else high,
        min(price, final) - 0.02 if low is None else low,
        final,
        volume,
    )


def source(*, full: bool = True, signals: tuple[Signal, ...] | None = None) -> Source:
    points = []
    if full:
        for i, day in enumerate(DAYS):
            opening, closing = _bounds(day)
            at = opening
            minute = 0
            while at < closing:
                points.extend(
                    (
                        candle("NVDA", at, 100 + i * 2 + minute * 0.01),
                        candle("SPY", at, 500 + i + minute * 0.02),
                    )
                )
                at += timedelta(minutes=1)
                minute += 1
    else:
        entry, exit_at = AT + timedelta(minutes=2), AT + timedelta(minutes=62)
        points = [
            candle("NVDA", entry, 100),
            candle("NVDA", exit_at, 110),
            candle("SPY", entry, 500),
            candle("SPY", exit_at, 505),
        ]
    return Source(
        DAYS,
        DAYS,
        (signal(),) if signals is None else signals,
        tuple(points),
        "synthetic-source",
        "synthetic-report",
        (),
    )


def unit(report, *, horizon="m60", delay=1, cost=5, key="base"):
    return next(
        u
        for u in report["units"]
        if u["signal_key"] == key
        and u["horizon"] == horizon
        and u["delay_minutes"] == delay
        and u["cost_bps_per_side"] == cost
    )


def cell(report, *, horizon="m60", delay=1, cost=5, minutes=0, arm="baseline_proxy"):
    return next(
        row
        for row in report["rows"]
        if row["arm"] == arm
        and row["horizon"] == horizon
        and row["delay_minutes"] == delay
        and row["cost_bps_per_side"] == cost
        and row["filter_minutes"] == minutes
    )


@pytest.fixture(scope="module")
def full_source() -> Source:
    return source(
        signals=(signal(), signal("normalized", "normalized"), signal("price", "normalized_price"))
    )


@pytest.fixture(scope="module")
def full_report(full_source: Source):
    return run(full_source)


def test_registered_tensor_and_deduplicated_references(full_report) -> None:
    assert full_report["registered_cells"] == full_report["assessed_cells"] == 1440
    assert full_report["repeated_control_cells"] == 12
    assert full_report["deduplicated_outcome_units"] == 360
    assert full_report["winner"] is None and full_report["validated_alpha"] is False
    assert len(full_report["units"]) == 360 and len(full_report["signal_gates"]) == 12
    assert cell(full_report)["unit_keys"] == cell(full_report, minutes=5)["unit_keys"]
    assert (
        full_report["complete_cells"] + full_report["partial_cells"] + full_report["blocked_cells"]
        == 1440
    )


def test_costs_match_cash_reference_without_double_charging() -> None:
    report = run(source(full=False))
    observed = unit(report)
    assert observed["entry_at"] == (AT + timedelta(minutes=2)).isoformat()
    assert observed["exit_at"] == (AT + timedelta(minutes=62)).isoformat()
    assert observed["quantity"] == 24
    assert observed["debit"] == pytest.approx(2401.2)
    assert observed["net_usd"] == pytest.approx(237.48)
    assert observed["cost_usd"] == pytest.approx(2.52)
    assert observed["spy_matched_usd"] == pytest.approx(21.588)
    assert observed["excess_usd"] == pytest.approx(215.892)
    assert observed["gross_underlying_mfe_pct"] is None
    assert observed["path_missing"] == ("regular_minute_path_gap",)
    assert cell(report)["strict_net_usd"] == pytest.approx(237.48)


def test_bar_uses_last_completed_anchor_with_availability_lag(full_source: Source) -> None:
    tape = Tape(full_source)
    gate = tape.gate(signal(), 5)
    assert gate.state is True
    assert gate.start == datetime(2026, 8, 3, 14, 5, tzinfo=UTC)
    assert gate.end == datetime(2026, 8, 3, 14, 10, tzinfo=UTC)
    assert gate.available_at == gate.end + timedelta(minutes=1)
    assert gate.available_at <= AT
    # The currently forming 14:10–14:15 candle cannot change this decision.
    later = tuple(
        replace(p, close=p.open - 0.01, low=p.open - 0.02)
        if p.ticker == "NVDA" and datetime(2026, 8, 3, 14, 10, tzinfo=UTC) <= p.at < AT
        else p
        for p in full_source.candles
    )
    assert Tape(replace(full_source, candles=later)).gate(signal(), 5) == gate


def test_initial_bar_unknown_and_rejected_bar_do_not_become_new_signals(
    full_source: Source,
) -> None:
    early = signal(at=datetime(2026, 8, 3, 13, 45, tzinfo=UTC))
    report = run(replace(full_source, signals=(early,)))
    assert cell(report, minutes=30)["gate_unknown"] == 1
    assert cell(report, minutes=30)["strict_net_usd"] is None
    rejected = tuple(
        replace(p, close=p.open - 1, low=p.open - 2)
        if p.ticker == "NVDA" and p.at == datetime(2026, 8, 3, 14, 9, tzinfo=UTC)
        else p
        for p in full_source.candles
    )
    rejected_report = run(replace(full_source, signals=(signal(),), candles=rejected))
    filtered = cell(rejected_report, minutes=5)
    assert filtered["gate_false"] == 1 and filtered["selected"] == 0
    assert filtered["strict_net_usd"] == 0 and filtered["unit_keys"] == []
    assert filtered["false_signal_keys"] == ["base"]


def test_missing_ohlc_bar_is_unknown_not_a_false_zero(full_source: Source) -> None:
    points = tuple(
        p
        for p in full_source.candles
        if not (p.ticker == "NVDA" and p.at == AT - timedelta(minutes=8))
    )
    report = run(replace(full_source, signals=(signal(),), candles=points))
    assert cell(report, minutes=5)["gate_unknown"] == 1
    assert cell(report, minutes=5)["strict_net_usd"] is None


def test_exit_is_exact_intraday_point_and_close_is_last_minute_close(
    full_report, full_source: Source
) -> None:
    c0 = unit(full_report, horizon="c0")
    assert c0["exit_at"] == datetime(2026, 8, 3, 20, tzinfo=UTC).isoformat()
    assert c0["exit_source_at"] == datetime(2026, 8, 3, 19, 59, tzinfo=UTC).isoformat()
    last = next(
        p
        for p in full_source.candles
        if p.ticker == "NVDA" and p.at == datetime(2026, 8, 3, 19, 59, tzinfo=UTC)
    )
    assert c0["exit_price"] == last.close and c0["exit_price"] != last.open
    c1 = unit(full_report, horizon="c1")
    assert c1["exit_at"] == datetime(2026, 8, 4, 20, tzinfo=UTC).isoformat()
    assert c1["exit_kind"] == "final_rth_minute_close"


def test_missing_exact_spy_mark_keeps_strict_total_unknown(full_source: Source) -> None:
    points = tuple(
        p
        for p in full_source.candles
        if not (p.ticker == "SPY" and p.at == AT + timedelta(minutes=62))
    )
    report = run(replace(full_source, signals=(signal(),), candles=points))
    assert unit(report)["missing"] == ("exact_exit_spy_missing",)
    assert cell(report)["strict_excess_usd"] is None
    assert report["primary_family"]["bootstrap"]["status"] == "UNAVAILABLE"


def test_late_entry_does_not_exit_after_regular_close(full_source: Source) -> None:
    late = signal(at=datetime(2026, 8, 3, 19, 45, tzinfo=UTC))
    report = run(replace(full_source, signals=(late,)))
    assert unit(report, horizon="m15")["missing"] == ("intraday_endpoint_outside_rth",)
    assert cell(report, horizon="m15")["strict_net_usd"] is None
    assert report["primary_family"]["administrative_roster"] == 0


def test_right_censoring_never_looks_past_source_end(full_source: Source) -> None:
    final = signal(at=datetime(2026, 8, 12, 14, 15, tzinfo=UTC))
    report = run(replace(full_source, signals=(final,)))
    assert unit(report, horizon="c1")["missing"] == ("right_censored_source_end",)
    assert unit(report, horizon="c5")["missing"] == ("right_censored_source_end",)
    assert len(report["primary_family"]["right_censored_days_excluded"]) == 5


def test_mae_mfe_do_not_use_intraday_exit_bars_future_high_low(full_source: Source) -> None:
    exit_at = AT + timedelta(minutes=62)
    points = tuple(
        replace(p, high=1000, low=1) if p.ticker == "NVDA" and p.at == exit_at else p
        for p in full_source.candles
    )
    report = run(replace(full_source, signals=(signal(),), candles=points))
    outcome = unit(report)
    assert outcome["gross_underlying_mfe_pct"] < 2
    assert outcome["gross_underlying_mae_pct"] > -1


def test_negative_volume_is_disclosed_without_deleting_price(full_source: Source) -> None:
    points = tuple(
        replace(p, volume=-1) if p.ticker == "NVDA" and p.at == AT + timedelta(minutes=3) else p
        for p in full_source.candles
    )
    report = run(replace(full_source, signals=(signal(),), candles=points))
    original = run(replace(full_source, signals=(signal(),)))
    assert unit(report)["net_usd"] == unit(original)["net_usd"]
    assert unit(report)["negative_volume_path_bars"] == 1


def test_missing_whole_intermediate_session_does_not_invent_a_complete_path(
    full_source: Source,
) -> None:
    missing_day = DAYS[2]
    changed = replace(
        full_source,
        signals=(signal(),),
        sessions=tuple(d for d in DAYS if d != missing_day),
        evaluation_sessions=tuple(d for d in DAYS if d != missing_day),
        candles=tuple(p for p in full_source.candles if p.day != missing_day),
    )
    outcome = unit(run(changed), horizon="c5")
    assert outcome["net_usd"] is not None
    assert outcome["gross_underlying_mfe_pct"] is None
    assert outcome["path_missing"] == ("regular_session_path_gap",)


def test_calendar_boundaries_and_invalid_minute_source(full_source: Source) -> None:
    assert _bounds(date(2026, 11, 4))[0].hour == 14
    assert _bounds(DAYS[0])[0].hour == 13
    assert _bounds(date(2026, 11, 27))[1].hour == 18
    with pytest.raises(ValueError):
        _bounds(date(2028, 1, 4))
    extra = candle("NVDA", datetime(2026, 8, 3, 20, tzinfo=UTC), 100)
    with pytest.raises(ValueError):
        Tape(replace(full_source, candles=(*full_source.candles, extra)))


def test_primary_common_panel_uses_same_dates_and_net_units(full_report) -> None:
    primary = full_report["primary_family"]
    assert len(primary["administrative_days"]) == 3
    assert len(primary["complete_common_days"]) == 3
    assert len(primary["right_censored_days_excluded"]) == 5
    anchor = next(row for row in primary["contrasts"] if row["horizon"] == "m60")
    assert anchor["mean_daily_difference_vs_m60_usd"] == 0
    assert anchor["total_net_usd"] == pytest.approx(unit(full_report)["net_usd"])
    assert anchor["complete_days"] == 3
    assert primary["bootstrap"]["reason"] == "insufficient_calendar_blocks"
