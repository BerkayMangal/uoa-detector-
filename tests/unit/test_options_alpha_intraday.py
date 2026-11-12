"""No future/stale quotes, substituted contracts, or missing-as-zero markouts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.options_alpha.intraday import evaluate_intraday_long_option

_SESSION = date(2026, 11, 9)
_TARGET = datetime(2026, 11, 9, 15, tzinfo=UTC)
_SYMBOL = "AAPL261120C00300000"


def _row(seconds: int, *, bid: str = "1.00", ask: str = "1.10", **changes: Any) -> dict[str, Any]:
    stamp = _TARGET + timedelta(seconds=seconds)
    row: dict[str, Any] = {
        "id": str(seconds), "option_chain_id": _SYMBOL, "expiry": "2026-11-20",
        "executed_at": stamp.isoformat(), "nbbo_bid_time": stamp.isoformat(),
        "nbbo_ask_time": stamp.isoformat(), "nbbo_bid": bid, "nbbo_ask": ask,
        "nbbo_bid_size": 1, "nbbo_ask_size": 2, "canceled": False,
        "option_type": "call", "trading_hours": "rth",
    }
    row.update(changes)
    return row


def _evaluate(rows: list[dict[str, Any]], **changes: Any) -> Any:
    inputs: dict[str, Any] = {
        "rows": rows, "option_symbol": _SYMBOL, "session": _SESSION,
        "planned_entry_at": _TARGET, "received_at": _TARGET + timedelta(days=3),
        "packet_date": _SESSION, "coverage_complete": True,
        "calendar": load_live_alpha_settings().calendar,
    }
    inputs.update(changes)
    return evaluate_intraday_long_option(**inputs)


def test_real_option_economics_use_bid_ask_slippage_and_both_fees() -> None:
    result = _evaluate([
        _row(3600, bid="0.90", ask="1.00"),
        _row(900, bid="1.20", ask="1.30"), _row(0),
    ])
    assert result.status == "EVALUATED"
    first, second = result.markouts
    assert first.quote_gross_pnl_usd == Decimal("10.00")
    assert first.modeled_gross_pnl_usd == Decimal("8.00")
    assert first.modeled_net_pnl_usd == Decimal("6.70")
    assert second.modeled_net_pnl_usd == Decimal("-23.30")
    serialized = result.as_dict()
    assert serialized["entry_same_quote_roundtrip_friction_usd"] == "13.30"
    assert serialized["executable_fill_proven"] is False
    assert serialized["data_use"] == "retrospective_quote_markout"
    assert serialized["entry_quote"]["ask_age_seconds"] == 0


def test_first_valid_event_after_target_sets_horizons_not_planned_time() -> None:
    result = _evaluate([
        _row(-1, ask="0.50", bid="0.40"),
        _row(0, nbbo_ask_time=(_TARGET - timedelta(seconds=6)).isoformat()),
        _row(10), _row(900, bid="5.00", ask="5.10"),
        _row(910, bid="1.20", ask="1.30"), _row(3610),
    ])
    assert result.entry_quote is not None
    assert result.entry_quote.executed_at == _TARGET + timedelta(seconds=10)
    assert result.markouts[0].exit_quote is not None
    assert result.markouts[0].exit_quote.executed_at == _TARGET + timedelta(seconds=910)
    assert result.markouts[0].modeled_net_pnl_usd == Decimal("6.70")
    assert result.rejected_rows == {"stale_quote": 1}


@pytest.mark.parametrize(("changes", "reason"), [
    ({"nbbo_bid": "NaN"}, "price_missing_or_invalid"),
    ({"nbbo_ask": "Infinity"}, "price_missing_or_invalid"),
    ({"nbbo_bid": True}, "price_missing_or_invalid"),
    ({"nbbo_bid": "0"}, "price_missing_or_invalid"),
    ({"nbbo_bid": "2"}, "crossed_quote"),
    ({"nbbo_bid_size": True}, "size_missing_or_invalid"),
    ({"nbbo_ask_size": "1.5"}, "size_missing_or_invalid"),
    ({"nbbo_bid_size": "Infinity"}, "size_missing_or_invalid"),
    ({"nbbo_bid_size": 0}, "size_missing_or_invalid"),
    ({"nbbo_bid_size": None}, "size_missing_or_invalid"),
    ({"nbbo_ask_time": (_TARGET + timedelta(microseconds=1)).isoformat()}, "future_quote"),
    ({"nbbo_bid_time": (_TARGET - timedelta(seconds=5, microseconds=1)).isoformat()}, "stale_quote"),
    ({"nbbo_ask_time": None}, "timestamp_missing_or_invalid"),
    ({"nbbo_bid_time": "2026-11-09T15:00:00"}, "timestamp_missing_or_invalid"),
    ({"option_chain_id": "AAPL261120P00300000"}, "contract_mismatch"),
    ({"expiry": "2026-11-21"}, "expiry_missing_or_mismatch"),
    ({"canceled": True}, "canceled_or_cancel_state_missing"),
    ({"trading_hours": "post"}, "provider_not_regular_session"),
])
def test_invalid_quotes_are_unknown_and_each_reason_is_retained(
    changes: dict[str, object], reason: str,
) -> None:
    result = _evaluate([_row(0, **changes)])
    assert result.status == "UNKNOWN"
    assert result.reason == "missing_eligible_entry_quote"
    assert result.rejected_rows == {reason: 1}
    assert all(markout.modeled_net_pnl_usd is None for markout in result.markouts)


def test_missing_exit_is_unknown_not_zero_and_does_not_use_earlier_quote() -> None:
    result = _evaluate([_row(0), _row(899), _row(1021), _row(3600)])
    assert result.status == "PARTIAL"
    assert result.markouts[0].status == "UNKNOWN"
    assert result.markouts[0].modeled_net_pnl_usd is None
    assert result.markouts[1].status == "EVALUATED"


def test_gate_boundaries_are_inclusive_and_next_microsecond_is_excluded() -> None:
    old_side = _TARGET + timedelta(seconds=115)
    result = _evaluate([_row(120, nbbo_bid_time=old_side.isoformat()), _row(1140), _row(3840)])
    assert result.entry_quote is not None
    assert result.entry_quote.executed_at == _TARGET + timedelta(seconds=120)
    assert result.status == "EVALUATED"
    late = _row(120, executed_at=(_TARGET + timedelta(seconds=120, microseconds=1)).isoformat())
    assert _evaluate([late]).entry_quote is None


def test_incomplete_coverage_cannot_produce_a_positive_result() -> None:
    result = _evaluate([_row(0), _row(900, bid="20", ask="21"), _row(3600)], coverage_complete=False)
    assert result.status == "UNKNOWN"
    assert result.reason == "incomplete_source_coverage"
    assert result.entry_quote is None


@pytest.mark.parametrize(("changes", "reason"), [
    ({"packet_date": date(2026, 11, 10)}, "packet_session_mismatch"),
    ({"planned_entry_at": _TARGET.replace(tzinfo=None)}, "invalid_input_naive_or_invalid_timestamp"),
    ({"received_at": _TARGET - timedelta(seconds=1)}, "retrieval_before_entry_target"),
    ({"multiplier": 10}, "invalid_input_nonstandard_multiplier"),
    ({"multiplier": True}, "invalid_input_nonstandard_multiplier"),
    ({"option_symbol": "AAPL1261120C00300000"}, "invalid_input_nonstandard_contract_symbol"),
    ({"option_symbol": "AAPL261106C00300000"}, "expired_contract"),
    ({"planned_entry_at": _TARGET.replace(hour=14, minute=29)}, "entry_target_outside_regular_session"),
])
def test_metadata_mismatch_and_invalid_inputs_remain_distinguishable(
    changes: dict[str, object], reason: str,
) -> None:
    result = _evaluate([_row(0)], **changes)
    assert result.status == "UNKNOWN"
    assert result.reason == reason


def test_exit_cannot_cross_session_or_expiry_into_next_day() -> None:
    entry = _TARGET.replace(hour=20, minute=50)
    quote = _row(0, executed_at=entry.isoformat(), nbbo_bid_time=entry.isoformat(),
                 nbbo_ask_time=entry.isoformat())
    result = _evaluate([quote], planned_entry_at=entry)
    assert result.entry_quote is not None
    assert all(markout.reason == "exit_target_outside_regular_session" for markout in result.markouts)


def test_missing_provider_market_tag_uses_actual_new_york_time() -> None:
    row = _row(0)
    del row["trading_hours"]
    assert _evaluate([row]).entry_quote is not None
    row["executed_at"] = "2026-11-09T21:14:00Z"
    assert _evaluate([row]).rejected_rows == {"outside_regular_session": 1}


def test_exit_price_floors_at_zero_without_inventing_a_rebate() -> None:
    result = _evaluate([_row(0), _row(900, bid="0.005", ask="0.02"), _row(3600)])
    assert result.markouts[0].modeled_net_pnl_usd == Decimal("-112.30")
    assert result.markouts[0].as_dict()["modeled_exit_price"] == "0"


def test_conflicting_duplicate_event_cannot_choose_better_price() -> None:
    result = _evaluate([_row(0), _row(0, bid="0.40", ask="0.50"), _row(900)])
    assert result.status == "UNKNOWN"
    assert result.rejected_rows == {"conflicting_duplicate_event": 2}


@pytest.mark.parametrize("correction", [
    {"canceled": True},
    {"expiry": "2026-11-21"},
    {"nbbo_bid": "NaN"},
    {"nbbo_bid_size": True},
])
def test_invalid_or_canceled_revision_removes_previously_valid_event(
    correction: dict[str, object],
) -> None:
    original = _row(0)
    corrected = _row(0, **correction)
    for rows in ([original, corrected, _row(900)], [corrected, original, _row(900)]):
        result = _evaluate(rows)
        assert result.status == "UNKNOWN"
        assert result.entry_quote is None
        assert result.rejected_rows == {"conflicting_duplicate_event": 2}


def test_identical_duplicates_do_not_change_quote_economics() -> None:
    rows = [_row(0), _row(900), _row(3600)]
    assert _evaluate(rows).as_dict() == _evaluate(rows + rows).as_dict()


def test_holiday_and_early_close_are_not_regular_session() -> None:
    holiday = datetime(2026, 11, 26, 15, tzinfo=UTC)
    result = _evaluate([], session=holiday.date(), packet_date=holiday.date(),
                       option_symbol="AAPL261218C00300000", planned_entry_at=holiday,
                       received_at=holiday + timedelta(days=1))
    assert result.reason == "entry_target_outside_regular_session"
    after_close = datetime(2026, 11, 27, 18, 1, tzinfo=UTC)
    result = _evaluate([], session=after_close.date(), packet_date=after_close.date(),
                       option_symbol="AAPL261218C00300000", planned_entry_at=after_close,
                       received_at=after_close + timedelta(days=1))
    assert result.reason == "entry_target_outside_regular_session"


def test_calendar_must_be_explicit_and_cover_the_session_year() -> None:
    assert _evaluate([], calendar=None).reason == "missing_session_calendar"
    calendar = load_live_alpha_settings().calendar.model_copy(update={"years": (2025,)})
    assert _evaluate([], calendar=calendar).reason == "session_calendar_year_unknown"
