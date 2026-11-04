"""Shared slot allocation, three-valued gates and causal reference outcomes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import product

import pytest
from webapp.news_research.experiment import (
    ARMS,
    Dataset,
    Opportunity,
    Price,
    eligibility,
    evaluate,
    roster,
)

NOW = datetime(2026, 11, 4, 15, tzinfo=UTC)
DAY = NOW.date()
DONE = datetime(2026, 11, 4, 21, 10, tzinfo=UTC)


def opportunity(
    key: str = "one",
    ticker: str = "NVDA",
    at: datetime = NOW,
    base: bool | None = True,
    rs: bool | None = False,
    news: bool | None = True,
) -> Opportunity:
    return Opportunity(key, ticker, at.date(), at, rs, news, base)


def prices(ticker: str = "NVDA", at: datetime = NOW) -> tuple[Price, ...]:
    entry, exit_at = at + timedelta(minutes=2), at + timedelta(minutes=63)
    return (
        Price(ticker, entry, entry, 100),
        Price(ticker, exit_at, exit_at, 110),
        Price("SPY", entry, entry, 500),
        Price("SPY", exit_at, exit_at, 505),
    )


def dataset(
    opportunities: tuple[Opportunity, ...] | None = None,
    points: tuple[Price, ...] | None = None,
    asof: datetime = DONE,
) -> Dataset:
    return Dataset(
        (opportunity(),) if opportunities is None else opportunities,
        prices() if points is None else points,
        (DAY,),
        "synthetic-fixture",
        asof,
    )


@pytest.mark.parametrize(("base", "rs", "news"), product((True, False, None), repeat=3))
def test_three_valued_gates_match_all_boolean_completions(
    base: bool | None,
    rs: bool | None,
    news: bool | None,
) -> None:
    item = opportunity(base=base, rs=rs, news=news)
    outputs: dict[str, set[bool]] = {arm: set() for arm in ARMS}
    for g, r, n in product(
        *[(True, False) if value is None else (value,) for value in (base, rs, news)]
    ):
        for arm, outcome in zip(
            ARMS, (g, g and r, g and n, g and r and n, g and (r or n)), strict=True
        ):
            outputs[arm].add(outcome)
    for arm in ARMS:
        expected = next(iter(outputs[arm])) if len(outputs[arm]) == 1 else None
        assert eligibility(item, arm) is expected


def test_roster_keeps_first_unknown_without_backfill_or_gate_reallocation() -> None:
    first = opportunity("first", base=None, news=None)
    later = opportunity("later", at=NOW + timedelta(minutes=10))
    others = tuple(
        opportunity(ticker=t, key=t, at=NOW + timedelta(minutes=i + 1), news=False)
        for i, t in enumerate(("AAPL", "MSFT", "COHR", "NFLX", "BE"))
    )
    impossible = opportunity("false", "NONE", NOW - timedelta(minutes=1), base=False)
    frozen = roster(dataset((first, later, impossible, *others), ()))
    assert [o.key for o in frozen] == ["first", "AAPL", "MSFT", "COHR"]
    assert frozen[0].base is None
    assert roster(dataset(tuple(reversed((first, later, impossible, *others))), ())) == frozen


def test_future_opportunities_and_prices_cannot_change_asof_roster_or_label() -> None:
    data = dataset()
    future = opportunity("future", "AAA", DONE + timedelta(minutes=1))
    unseen = Price("NVDA", NOW + timedelta(minutes=1, seconds=1), DONE + timedelta(minutes=1), 1)
    extended = replace(
        data, opportunities=(*data.opportunities, future), prices=(*data.prices, unseen)
    )
    assert roster(extended) == roster(data)
    assert evaluate(extended).base.units == evaluate(data).base.units


def test_costs_whole_share_budget_and_same_debit_exact_spy_pair() -> None:
    report = evaluate(dataset())
    unit = report.base.units[0]
    assert unit.quantity == 24  # 25 shares at the adverse entry price exceed USD2,500.
    assert unit.entry_debit == pytest.approx(2401.2)
    assert unit.gross == 240
    assert unit.cost == pytest.approx(2.52)
    assert unit.net == pytest.approx(237.48)
    assert unit.spy_net == pytest.approx(21.588)
    assert unit.excess == pytest.approx(215.892)
    pair = report.base.paired_days[0]
    assert pair.b_net == 0 and pair.b_excess == 0
    assert pair.difference_net == pytest.approx(237.48)
    assert pair.difference_excess == pytest.approx(215.892)
    assert report.news_only_count == 1
    assert report.base.complete_common_sessions == 1
    assert report.status == "INSUFFICIENT_EVIDENCE" and report.winner is None
    assert [r.slippage_bps_per_side for r in report.stresses] == [10, 20, 50]
    assert all(
        r.units[0].entry_debit <= 2500
        for r in report.stresses
        if r.units[0].entry_debit is not None
    )
    assert report.stresses[0].units[0].net == pytest.approx(234.96)
    assert all(
        r.units[0].net < unit.net
        for r in report.stresses
        if r.units[0].net is not None and unit.net is not None
    )


def test_first_observed_knowledge_selects_price_and_anchors_holding_clock() -> None:
    late_entry = Price("NVDA", NOW + timedelta(minutes=2), NOW + timedelta(minutes=5), 99)
    entry_at = NOW + timedelta(minutes=3)
    exit_at = NOW + timedelta(minutes=64)
    points = (
        late_entry,
        Price("NVDA", entry_at, entry_at, 100),
        Price("NVDA", exit_at, exit_at, 110),
        Price("SPY", entry_at, entry_at, 500),
        Price("SPY", exit_at, exit_at, 505),
    )
    unit = evaluate(dataset(points=points)).base.units[0]
    assert unit.entry_at == entry_at and unit.entry_known_at == entry_at
    assert unit.exit_at == exit_at


def test_exact_delay_is_not_a_strictly_later_price() -> None:
    at = NOW + timedelta(minutes=1)
    point = Price("NVDA", at, at, 1)
    unit = evaluate(dataset(points=(point, *prices()))).base.units[0]
    assert unit.entry_at == NOW + timedelta(minutes=2)


@pytest.mark.parametrize(
    "damage", ("missing_exit", "late_entry_receipt", "spy_wrong_time", "conflict")
)
def test_bad_labels_stay_unknown_and_are_not_replaced(damage: str) -> None:
    points = list(prices())
    if damage == "missing_exit":
        points.pop(1)
    elif damage == "late_entry_receipt":
        points[0] = replace(points[0], known_at=NOW + timedelta(minutes=7))
    elif damage == "spy_wrong_time":
        points[3] = replace(
            points[3],
            at=points[3].at + timedelta(seconds=1),
            known_at=points[3].known_at + timedelta(seconds=1),
        )
    else:
        points.append(replace(points[0], value=101))
    report = evaluate(dataset(points=tuple(points)))
    unit = report.base.units[0]
    assert unit.net is None and unit.missing
    assert next(d for d in report.base.days if d.arm == "A").net is None
    assert next(d for d in report.base.days if d.arm == "B").net == 0
    assert not report.base.paired_days
    assert report.base.complete_common_sessions == 0


def test_one_unpriced_selected_slot_invalidates_entire_arm_day() -> None:
    data = dataset(
        (opportunity(), opportunity("missing", "MSFT", NOW + timedelta(minutes=1))), prices()
    )
    report = evaluate(data)
    e_day = next(d for d in report.base.days if d.arm == "E")
    assert e_day.selected == 2 and e_day.unpriced == 1 and e_day.net is None
    assert report.base.arms[-1].net_complete_days is None
    assert not report.base.paired_days


def test_unknown_news_can_short_circuit_for_primary_but_not_full_support() -> None:
    report = evaluate(dataset((opportunity(rs=True, news=None),)))
    assert len(report.base.paired_days) == 1
    assert report.base.paired_days[0].difference_excess == 0
    assert report.base.complete_common_sessions == 0
    assert next(d for d in report.base.days if d.arm == "C").net is None


def test_unfinished_session_is_not_a_complete_zero_or_final_daily_total() -> None:
    report = evaluate(dataset(asof=NOW + timedelta(hours=2)))
    assert report.base.units[0].net is not None
    assert all(
        day.net is None and "session_not_finished" in day.missing for day in report.base.days
    )
    assert not report.base.paired_days and report.base.complete_common_sessions == 0


def test_80_common_sessions_only_enable_descriptive_readiness() -> None:
    from uoa_detector.live_alpha.calendar import is_session_day
    from uoa_detector.live_alpha.settings import load_live_alpha_settings

    days = []
    day = date(2026, 1, 5)
    while len(days) < 80:
        if is_session_day(day, load_live_alpha_settings().calendar):
            days.append(day)
        day += timedelta(days=1)
    opportunities, points = [], []
    for day in days:
        at = datetime.combine(day, NOW.timetz())
        opportunities.append(opportunity(day.isoformat(), at=at))
        points.extend(prices(at=at))
    data = Dataset(tuple(opportunities), tuple(points), tuple(days), "synthetic-80-days", DONE)
    report = evaluate(data)
    assert report.base.complete_common_sessions == 80
    assert report.status == "DESCRIPTIVE_READY_NOT_VALIDATED"
    assert report.inference == "NOT_PERFORMED" and report.winner is None
    assert evaluate(replace(data, sessions=tuple(days[:-1]))).status == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("bad", (0.0, -1.0, float("nan"), float("inf")))
def test_invalid_price_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        Price("NVDA", NOW, NOW, bad)


def test_bad_clocks_and_ambiguous_identities_are_rejected() -> None:
    with pytest.raises(ValueError):
        Price("NVDA", NOW, NOW - timedelta(seconds=1), 100)
    with pytest.raises(ValueError):
        replace(opportunity(), at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        replace(dataset(), opportunities=(opportunity(), opportunity()))
    with pytest.raises(ValueError):
        replace(dataset(), sessions=(DAY, DAY))


def test_opening_delay_and_early_close_are_exchange_calendar_boundaries() -> None:
    pre = opportunity("too_early", at=datetime(2026, 11, 4, 14, 44, tzinfo=UTC))
    assert roster(dataset((pre,), ())) == ()
    at = datetime(2026, 11, 27, 17, tzinfo=UTC)  # 12:00 ET, early close at 13:00.
    data = Dataset(
        (opportunity(at=at),), prices(at=at), (at.date(),), "early-close", at + timedelta(hours=5)
    )
    assert evaluate(data).base.units[0].net is None
    assert evaluate(data).base.units[0].missing == ("exit_missing_observed_price",)


def test_unknown_calendar_and_unaffordable_share_remain_unresolved() -> None:
    unknown_at = datetime(2028, 1, 4, 15, tzinfo=UTC)
    unknown = Dataset((), (), (unknown_at.date(),), "calendar-unknown", unknown_at)
    assert evaluate(unknown).base.complete_common_sessions == 0
    assert all("unknown_calendar" in row.missing for row in evaluate(unknown).base.days)
    points = tuple(replace(p, value=3000) if p.ticker == "NVDA" else p for p in prices())
    unit = evaluate(dataset(points=points)).base.units[0]
    assert unit.net is None and unit.missing == ("no_affordable_whole_share",)


def test_inconsistent_exact_time_spy_and_nonfinite_math_are_unresolved() -> None:
    points = (*prices(), replace(prices()[2], value=501))
    assert evaluate(dataset(points=points)).base.units[0].missing == (
        "entry_conflicting_spy_versions",
    )
    huge = tuple(
        replace(p, value=1e-300 if p == prices()[0] else 1e300) if p.ticker == "NVDA" else p
        for p in prices()
    )
    unit = evaluate(dataset(points=huge)).base.units[0]
    assert unit.net is None and unit.missing == ("nonfinite_reference_arithmetic",)


def test_paired_daily_contrast_keeps_shared_slots_and_is_not_difference_of_trade_means() -> None:
    items = (
        opportunity("rs", "NVDA", rs=True, news=False),
        opportunity("news", "MSFT", rs=False, news=True),
        opportunity("neither", "BE", rs=False, news=False),
    )
    report = evaluate(dataset(items, (*prices(), *prices("MSFT"))))
    pair = report.base.paired_days[0]
    assert pair.e_net == pytest.approx(474.96) and pair.b_net == pytest.approx(237.48)
    # Both arms have the same conditional average trade return: their exposure differs.
    assert pair.e_net / 2 == pair.b_net
    assert pair.difference_net == pytest.approx(237.48)
    assert pair.difference_excess == pytest.approx(215.892)
    # A selects the unpriced third slot, so the five-arm support remains incomplete.
    assert report.base.complete_common_sessions == 0
