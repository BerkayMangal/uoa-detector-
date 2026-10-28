"""Actual recorded reference coverage, no fake zero returns or inflated trade n."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from webapp import main
from webapp.opening import measurement, outcomes, storage, view
from webapp.opening.model import settings

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_opening_desk import T0, add_observation, persist, rows, source
from tests.unit.test_opening_desk import engine as engine  # shared real SQLite fixture


def result(
    key: str = "one",
    ticker: str = "NVDA",
    at: datetime = T0,
    marks: tuple[tuple[str, dict[str, Any] | None], ...] = (),
) -> view.Result:
    return view.Result(
        ticker,
        at,
        "long_call",
        marks,
        key,
        at.astimezone(measurement.ZoneInfo("America/New_York")).date(),
        settings().profile_hash,
    )


def mark(
    at: datetime = T0 + timedelta(minutes=15),
    value: float | None = 2,
    excess: float | None = 1,
    status: str = "complete",
) -> dict[str, Any]:
    return dict(
        status=status,
        observed_at=at.isoformat(),
        target_at=(T0 + timedelta(minutes=15)).isoformat(),
        directional_underlying_pct=value,
        directional_excess_pp=excess,
        spy_pct=1 if excess is not None else None,
    )


def test_zero_coverage_is_unknown_not_zero_success() -> None:
    summary = measurement.build((), T0)
    assert summary.entries == summary.sessions == summary.tickers == 0
    assert len(summary.horizons) == len(settings().intraday_minutes) + len(
        settings().close_sessions
    )
    assert all(
        h.mean_pct is None and h.mean_excess_pp is None and h.observed == 0
        for h in summary.horizons
    )


def test_awaiting_record_is_not_assumed_missing_price_after_window() -> None:
    early = measurement.build((result(),), T0).horizons[0]
    later = measurement.build((result(),), T0 + timedelta(hours=2)).horizons[0]
    assert early.pending == 1 and early.awaiting == early.missing == 0
    assert later.awaiting == 1 and later.missing == later.pending == 0
    assert later.mean_pct is None


def test_first_entry_keys_deduplicate_updates_with_separate_session_count() -> None:
    own = result(marks=(("+15 dk", mark()),))
    updated = replace(own, marks=(("+15 dk", mark(value=100)),))
    summary = measurement.build(
        (own, updated, result("two", "AMD", marks=(("+15 dk", mark(value=-2, excess=-3)),))),
        T0 + timedelta(hours=1),
    )
    h = summary.horizons[0]
    assert (
        summary.entries == 2
        and summary.duplicates == 1
        and summary.sessions == 1
        and summary.tickers == 2
    )
    assert h.observed == 2 and h.positive == 1 and h.sessions == 1 and h.mean_pct == 0
    assert h.mean_excess_pp == -1
    assert summary.horizons[1].observed == 0  # never concatenate horizons as trades


@pytest.mark.parametrize("value", [0, -2, 2])
def test_real_zero_negative_and_positive_references(value: float) -> None:
    summary = measurement.build(
        (result(marks=(("+15 dk", mark(value=value, excess=value - 1)),)),),
        T0 + timedelta(minutes=16),
    )
    h = summary.horizons[0]
    assert h.mean_pct == value and h.positive == int(value > 0) and h.observed == 1


def test_spy_denominator_and_partial_coverage_are_separate() -> None:
    partial = result(
        "partial", marks=(("+15 dk", mark(value=10, excess=None, status="missing_reference")),)
    )
    complete = result("complete", "AMD", marks=(("+15 dk", mark(value=2, excess=1)),))
    summary = measurement.build((partial, complete), T0 + timedelta(minutes=16))
    h = summary.horizons[0]
    assert h.mean_pct == 6 and h.mean_excess_pp == 1
    assert h.observed == 2 and h.matched == 1 and h.partial == 1


def test_recorded_missing_does_not_enter_return_denominator() -> None:
    missing = result(marks=(("+15 dk", mark(value=None, excess=None, status="missing_reference")),))
    h = measurement.build((missing,), T0 + timedelta(minutes=16)).horizons[0]
    assert h.missing == 1 and h.observed == 0 and h.mean_pct is None


@pytest.mark.parametrize(
    "mutation", ["unrecorded", "different_profile", "future_entry", "unknown_day", "naive"]
)
def test_unrecorded_or_different_policy_and_future_data_excluded(mutation: str) -> None:
    r = result()
    if mutation == "unrecorded":
        r = replace(r, key="")
    elif mutation == "different_profile":
        r = replace(r, profile_hash="different")
    elif mutation == "future_entry":
        r = replace(r, at=T0 + timedelta(days=1))
    elif mutation == "unknown_day":
        r = replace(r, day=None)
    elif mutation == "naive":
        r = replace(r, at=T0.replace(tzinfo=None))
    summary = measurement.build((r,), T0)
    assert summary.entries == 0 and summary.excluded == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "nan",
        "infinite",
        "bool",
        "string_number",
        "wrong_target",
        "naive_observation",
        "early_observation",
        "inconsistent_complete",
        "invalid_status",
        "missing_timestamp",
    ],
)
def test_invalid_evidence_never_looks_like_measured_alpha(mutation: str) -> None:
    raw = mark()
    if mutation == "nan":
        raw["directional_underlying_pct"] = float("nan")
    elif mutation == "infinite":
        raw["directional_excess_pp"] = float("inf")
    elif mutation == "bool":
        raw["directional_underlying_pct"] = True
    elif mutation == "string_number":
        raw["directional_underlying_pct"] = "2.0"
    elif mutation == "wrong_target":
        raw["target_at"] = T0.isoformat()
    elif mutation == "naive_observation":
        raw["observed_at"] = (T0 + timedelta(minutes=15)).replace(tzinfo=None).isoformat()
    elif mutation == "early_observation":
        raw["observed_at"] = T0.isoformat()
    elif mutation == "inconsistent_complete":
        raw["spy_pct"] = None
    elif mutation == "invalid_status":
        raw["status"] = "profitable"
    elif mutation == "missing_timestamp":
        raw.pop("observed_at")
    h = measurement.build((result(marks=(("+15 dk", raw),)),), T0 + timedelta(minutes=16)).horizons[
        0
    ]
    assert h.observed == 0 and h.invalid == 1 and h.mean_pct is None


def test_future_result_not_consumed_until_actually_available() -> None:
    raw = mark(at=T0 + timedelta(minutes=17))
    h = measurement.build((result(marks=(("+15 dk", raw),)),), T0 + timedelta(minutes=16)).horizons[
        0
    ]
    assert h.observed == 0 and h.awaiting == 1 and h.invalid == 0


def test_scope_uses_latest_bounded_records_never_best_returns() -> None:
    entries = tuple(
        result(str(i), at=T0 - timedelta(seconds=i), marks=(("+15 dk", mark(value=float(i))),))
        for i in range(settings().history_limit + 2)
    )
    summary = measurement.build(entries, T0 + timedelta(hours=1))
    assert summary.entries == settings().history_limit
    assert summary.excluded == 0
    assert summary.first_day == summary.last_day == T0.date()
    older = replace(result("older"), day=date(2026, 10, 27), at=T0 - timedelta(days=1))
    selected = measurement.build((result(), older), T0, scope_day=date(2026, 10, 27))
    assert selected.entries == 1 and selected.scope_day == date(2026, 10, 27)


@pytest.mark.parametrize(
    ("day", "now", "count", "expected"),
    [
        (
            date(2026, 10, 30),
            datetime(2026, 11, 2, 20, 59, tzinfo=UTC),
            1,
            datetime(2026, 11, 2, 21, tzinfo=UTC),
        ),
        (
            date(2026, 11, 25),
            datetime(2026, 11, 27, 17, 59, tzinfo=UTC),
            1,
            datetime(2026, 11, 27, 18, tzinfo=UTC),
        ),
        (date(2026, 10, 28), T0, 3, datetime(2026, 11, 2, 21, tzinfo=UTC)),
    ],
)
def test_declared_calendar_dst_holiday_early_close(
    day: date, now: datetime, count: int, expected: datetime
) -> None:
    assert measurement._target(day, T0, "sessions", count) == expected
    at = min(datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=15), now)
    h = next(
        h
        for h in measurement.build((result(at=at),), now).horizons
        if h.label == f"+{count} seans kapanış"
    )
    assert h.pending == 1


def test_unknown_calendar_does_not_guess_target() -> None:
    at = T0.replace(year=2028)
    summary = measurement.build((result(at=at),), at + timedelta(days=2))
    assert all(h.unknown_time == 1 for h in summary.horizons if "kapanış" in h.label)
    with pytest.raises(ValueError, match="timezone-aware"):
        measurement.build((), T0.replace(tzinfo=None))


def test_actual_ledger_summary_get_is_read_only_and_no_paper_extrapolation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist(engine, source())
    at = T0 + timedelta(minutes=15)
    add_observation(engine, "NVDA", at, at, 102)
    add_observation(engine, "SPY", at, at, 505)
    outcomes.measure(engine, at)
    desk = view.build(engine, at)
    summary = measurement.build(desk.results, at)
    assert summary.entries == 1 and summary.horizons[0].mean_pct == pytest.approx(2)
    assert summary.horizons[0].mean_excess_pp == pytest.approx(1)
    before = [len(rows(engine, m)) for m in storage.TABLES]
    writes = []

    def record(
        connection: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool
    ) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, "_now", lambda: at)
    response = TestClient(main.app, headers=set_web_auth(monkeypatch)).get("/acilis")
    event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200 and "Gerçekte neyi ölçtük?" in response.text
    assert (
        "1 ilk öneri" in response.text and "1 hisse" in response.text and "+2.00%" in response.text
    )
    assert "opsiyon P&amp;L" in response.text and not writes
    assert before == [len(rows(engine, m)) for m in storage.TABLES]
