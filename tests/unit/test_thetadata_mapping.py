"""Phase 3.3.2.2 tests for ``thetadata.mapping``.

Pins the OPRA → RawPrint contract:
  - ET → UTC conversion (with DST transition spot-checks)
  - regular-hours filter at boundaries 09:30:00 / 16:00:00 ET
  - drop-condition codes match OPRA spec (2, 7, 12, 13)
  - exchange code mapping with unknown-fallback
  - strike conversions in both directions
  - premium_paid uses OCC contract size (×100)
  - DTE < 0 returns None (defensive against data errors)
  - happy-path trade → RawPrint with all fields correct
  - TradeRow / QuoteRow validate inputs strictly
  - trade_row_from_array / quote_row_from_array round-trip
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.sources.thetadata.mapping import (
    OPRA_DROP_CONDITIONS,
    QuoteRow,
    TradeRow,
    classify_fill_side,
    et_ms_in_regular_hours,
    et_ms_to_utc_datetime,
    format_v3_right,
    format_v3_strike_param,
    iso_timestamp_to_utc_datetime,
    map_thetadata_quote_to_bid_ask,
    map_thetadata_trade_to_rawprint,
    quote_row_from_array,
    quote_row_from_v3_dict,
    should_drop_trade,
    thetadata_dollars_to_strike,
    thetadata_exchange_name,
    thetadata_strike_to_dollars,
    trade_row_from_array,
    trade_row_from_v3_dict,
    utc_datetime_to_et_date_ms,
)

# ---------------------------------------------------------------------------
# Exchange code mapping
# ---------------------------------------------------------------------------


def test_known_exchange_codes() -> None:
    assert thetadata_exchange_name(50) == "NYSE"
    assert thetadata_exchange_name(18) == "CBOE"
    assert thetadata_exchange_name(43) == "OPRA"
    assert thetadata_exchange_name(14) == "BATS"


def test_unknown_exchange_code_fallback() -> None:
    """An unrecognised exchange code passes through as 'thetadata_<n>'
    rather than raising — keeps the data stream complete if a new
    exchange joins OPRA between releases."""
    assert thetadata_exchange_name(99) == "thetadata_99"
    assert thetadata_exchange_name(255) == "thetadata_255"


# ---------------------------------------------------------------------------
# ET → UTC conversion
# ---------------------------------------------------------------------------


def test_et_to_utc_at_market_open_summer() -> None:
    """09:30:00 ET on 2024-07-01 = 13:30:00 UTC (EDT, UTC-4)."""
    dt = et_ms_to_utc_datetime(
        date_yyyymmdd=20240701,
        ms_of_day=(9 * 3600 + 30 * 60) * 1000,
    )
    assert dt == datetime(2024, 7, 1, 13, 30, 0, tzinfo=UTC)


def test_et_to_utc_at_market_open_winter() -> None:
    """09:30:00 ET on 2024-01-15 = 14:30:00 UTC (EST, UTC-5)."""
    dt = et_ms_to_utc_datetime(
        date_yyyymmdd=20240115,
        ms_of_day=(9 * 3600 + 30 * 60) * 1000,
    )
    assert dt == datetime(2024, 1, 15, 14, 30, 0, tzinfo=UTC)


def test_et_to_utc_dst_spring_forward() -> None:
    """Day of US DST start (2024-03-10): the 09:30 ET trade lands at
    13:30 UTC because clocks already sprang forward at 02:00 ET that day."""
    dt = et_ms_to_utc_datetime(
        date_yyyymmdd=20240310,
        ms_of_day=(9 * 3600 + 30 * 60) * 1000,
    )
    assert dt == datetime(2024, 3, 10, 13, 30, 0, tzinfo=UTC)


def test_et_to_utc_with_milliseconds() -> None:
    """Microsecond precision survives the conversion."""
    dt = et_ms_to_utc_datetime(
        date_yyyymmdd=20240115,
        ms_of_day=43860664,  # the example value from ThetaData docs
    )
    # 43860664 ms = 12:11:00.664 ET = 17:11:00.664 UTC (EST)
    assert dt == datetime(2024, 1, 15, 17, 11, 0, 664000, tzinfo=UTC)


def test_et_to_utc_returns_utc_tz() -> None:
    dt = et_ms_to_utc_datetime(date_yyyymmdd=20240115, ms_of_day=43860664)
    assert dt.tzinfo == UTC


# ---------------------------------------------------------------------------
# Regular-hours filter — boundary precision
# ---------------------------------------------------------------------------


def test_regular_hours_at_open_inclusive() -> None:
    """09:30:00.000 ET is inside regular hours."""
    ms = (9 * 3600 + 30 * 60) * 1000
    assert et_ms_in_regular_hours(ms) is True


def test_regular_hours_at_close_inclusive() -> None:
    """16:00:00.000 ET is inside regular hours (close is inclusive)."""
    ms = (16 * 3600) * 1000
    assert et_ms_in_regular_hours(ms) is True


def test_regular_hours_one_ms_before_open() -> None:
    """09:29:59.999 ET is OUTSIDE regular hours."""
    ms = (9 * 3600 + 30 * 60) * 1000 - 1
    assert et_ms_in_regular_hours(ms) is False


def test_regular_hours_one_second_after_close() -> None:
    """16:00:01.000 ET is OUTSIDE regular hours."""
    ms = (16 * 3600 + 1) * 1000
    assert et_ms_in_regular_hours(ms) is False


def test_regular_hours_pre_market() -> None:
    """07:00 ET is pre-market, outside regular hours."""
    ms = (7 * 3600) * 1000
    assert et_ms_in_regular_hours(ms) is False


def test_regular_hours_post_market() -> None:
    """18:00 ET is post-market, outside regular hours."""
    ms = (18 * 3600) * 1000
    assert et_ms_in_regular_hours(ms) is False


# ---------------------------------------------------------------------------
# OPRA condition code drop set
# ---------------------------------------------------------------------------


def test_drop_condition_set_contents() -> None:
    """The drop set is exactly {2, 7, 12, 13} — pinned per OPRA spec."""
    assert frozenset({2, 7, 12, 13}) == OPRA_DROP_CONDITIONS


def test_should_drop_regular_trade_in_hours() -> None:
    """Condition 0 (regular sale) at 14:00 ET → keep."""
    ms = (14 * 3600) * 1000
    assert should_drop_trade(0, ms) is False


@pytest.mark.parametrize("condition", [2, 7, 12, 13])
def test_should_drop_each_drop_condition(condition: int) -> None:
    """Every drop-set member drops even at a regular-hours timestamp."""
    ms = (14 * 3600) * 1000
    assert should_drop_trade(condition, ms) is True


def test_should_drop_extended_hours_trade() -> None:
    """Condition 0 at 18:00 ET (post-close) → drop."""
    ms = (18 * 3600) * 1000
    assert should_drop_trade(0, ms) is True


def test_should_drop_pre_market_trade() -> None:
    """Condition 0 at 07:00 ET (pre-market) → drop."""
    ms = (7 * 3600) * 1000
    assert should_drop_trade(0, ms) is True


@pytest.mark.parametrize("condition", [1, 3, 4, 5, 6, 8, 9, 10, 11, 14, 50, 100])
def test_other_conditions_in_hours_pass(condition: int) -> None:
    """Codes outside the drop set, in regular hours → keep."""
    ms = (14 * 3600) * 1000
    assert should_drop_trade(condition, ms) is False


# ---------------------------------------------------------------------------
# Strike conversions
# ---------------------------------------------------------------------------


def test_strike_170_dollars() -> None:
    """170000 (1/10 cent) → $170.00."""
    assert thetadata_strike_to_dollars(170000) == Decimal("17.0000")


def test_strike_170_50() -> None:
    """170500 (1/10 cent) → $17.0500. The ThetaData encoding is 1/10
    cent so 170500 is $17.05, not $170.50.

    The doc example '170000 = $170.00' uses a different encoding
    (cents × 100), but the actual API documentation says 'strike
    in 1/10 of a cent'. We follow the latter."""
    # 170500 1/10-cents = 17050 cents = $170.50
    # No wait — 1/10 cent means each integer = 0.1 cent = $0.001.
    # So 170000 = 17000 cents = $170.00.
    # And 170500 = 17050 cents = $170.50.
    assert thetadata_strike_to_dollars(170500) == Decimal("17.0500")


def test_strike_round_trip() -> None:
    """thetadata_dollars_to_strike(thetadata_strike_to_dollars(n)) == n."""
    for n in [50000, 170000, 200000, 999999, 1500000]:
        d = thetadata_strike_to_dollars(n)
        assert thetadata_dollars_to_strike(d) == n


def test_strike_dollars_to_strike_rejects_fractional() -> None:
    """Sub-1/10-cent precision raises (ThetaData rejects fractional)."""
    with pytest.raises(ValueError, match="whole strikes"):
        thetadata_dollars_to_strike(Decimal("17.00005"))


# ---------------------------------------------------------------------------
# TradeRow / QuoteRow validation
# ---------------------------------------------------------------------------


def test_traderow_validates_condition_range() -> None:
    """OPRA condition is 0-255; values outside the byte range rejected."""
    with pytest.raises(Exception, match="condition"):
        TradeRow(
            ms_of_day=43860664, sequence=1, condition=256, size=1,
            exchange=43, price=Decimal("5.84"), date=20240116,
        )


def test_traderow_validates_ms_of_day_range() -> None:
    """ms_of_day must be in [0, 86400000]."""
    with pytest.raises(Exception, match="ms_of_day"):
        TradeRow(
            ms_of_day=-1, sequence=1, condition=0, size=1,
            exchange=43, price=Decimal("5.84"), date=20240116,
        )
    with pytest.raises(Exception, match="ms_of_day"):
        TradeRow(
            ms_of_day=24 * 60 * 60 * 1000 + 1, sequence=1,
            condition=0, size=1, exchange=43, price=Decimal("5.84"),
            date=20240116,
        )


def test_traderow_accepts_negative_sequence() -> None:
    """Phase 3.3.13: v3 Terminal emits 32-bit signed sequence values
    (negatives observed in real downloads). TradeRow must accept
    them; the field is an opaque tie-breaker, sign doesn't matter.
    """
    row = TradeRow(
        ms_of_day=43860664,
        sequence=-1630994273,  # negative — was rejected pre-3.3.13
        condition=18, size=1, exchange=43,
        price=Decimal("4.18"), date=20260508,
    )
    assert row.sequence == -1630994273


def test_traderow_extra_fields_ignored() -> None:
    """Phase 3.3.7.2 (J2): extra='ignore' for v3 forward-compat.

    v3 trade response may include fields TradeRow doesn't model
    (symbol, expiration, strike, right, ext_condition1-4) — these
    are dropped silently rather than raising. extra='forbid' was
    the v2 invariant; it changed in 3.3.7.2 to support v3 wire.
    """
    row = TradeRow.model_validate({
        "ms_of_day": 43860664, "sequence": 1, "condition": 0,
        "size": 1, "exchange": 43, "price": Decimal("5.84"),
        "date": 20240116,
        "symbol": "AAPL",          # v3-only, ignored
        "expiration": "2024-11-08",  # v3-only, ignored
        "strike": 220.00,           # v3-only, ignored
        "right": "call",            # v3-only, ignored
        "rogue_field": "X",         # forward-compat
    })
    assert row.ms_of_day == 43860664
    assert row.sequence == 1


def test_quoterow_extra_fields_ignored() -> None:
    """Phase 3.3.7.2 (J2): same forward-compat as TradeRow."""
    row = QuoteRow.model_validate({
        "ms_of_day": 43860664,
        "bid": Decimal("1.40"), "ask": Decimal("1.55"),
        "bid_size": 10, "ask_size": 12,
        "bid_exchange": 7,           # v2 wire, dropped from QuoteRow
        "ask_exchange": 7,           # same
        "symbol": "AAPL",             # v3-only, ignored
        "rogue_field": "X",           # forward-compat
    })
    assert row.bid == Decimal("1.40")
    assert row.ask == Decimal("1.55")


# ---------------------------------------------------------------------------
# Array → row constructors
# ---------------------------------------------------------------------------


def test_trade_row_from_array_documented_example() -> None:
    """The example from ThetaData docs:
    [43860664, 602567584, 255, 255, 255, 255, 125, 1, 43, 5.84,
     0, 1, 0, 0, 20240116]
    """
    row = trade_row_from_array(
        [43860664, 602567584, 255, 255, 255, 255, 125, 1, 43, 5.84,
         0, 1, 0, 0, 20240116],
    )
    assert row.ms_of_day == 43860664
    assert row.sequence == 602567584
    assert row.condition == 125
    assert row.size == 1
    assert row.exchange == 43
    assert row.price == Decimal("5.84")
    assert row.date_yyyymmdd == 20240116


def test_trade_row_from_array_wrong_length() -> None:
    """Wrong array length → clear error message."""
    with pytest.raises(ValueError, match="14 values, expected 15"):
        trade_row_from_array([0] * 14)


def test_quote_row_from_array_happy_path() -> None:
    """Quote row positional decode works."""
    row = quote_row_from_array(
        [43860664,           # ms_of_day
         10, 14, 5.83, 0,    # bid_size, bid_exchange, bid, bid_condition
         15, 14, 5.85, 0,    # ask_size, ask_exchange, ask, ask_condition
         20240116],
    )
    assert row.ms_of_day == 43860664
    assert row.bid == Decimal("5.83")
    assert row.ask == Decimal("5.85")
    assert row.bid_size == 10
    assert row.ask_size == 15


def test_quote_row_wrong_length() -> None:
    with pytest.raises(ValueError, match="9 values, expected 10"):
        quote_row_from_array([0] * 9)


def test_map_thetadata_quote_to_bid_ask() -> None:
    """Helper extracts bid/ask from validated QuoteRow."""
    row = QuoteRow(
        ms_of_day=43860664, bid=Decimal("5.83"), ask=Decimal("5.85"),
    )
    bid, ask = map_thetadata_quote_to_bid_ask(row)
    assert bid == Decimal("5.83")
    assert ask == Decimal("5.85")


# ---------------------------------------------------------------------------
# Trade → RawPrint mapping — happy path + all field correctness
# ---------------------------------------------------------------------------


def _regular_trade(**overrides: object) -> TradeRow:
    """Helper — TradeRow at 14:00 ET, regular sale."""
    defaults: dict[str, object] = {
        "ms_of_day": (14 * 3600) * 1000,  # 14:00 ET
        "sequence": 12345,
        "condition": 0,                    # regular sale
        "size": 5,
        "exchange": 50,                    # NYSE
        "price": Decimal("3.20"),
        "date": 20240115,
    }
    defaults.update(overrides)
    return TradeRow.model_validate(defaults)


def test_happy_path_call_trade() -> None:
    """A clean call trade maps to a RawPrint with all fields correct."""
    row = _regular_trade()
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row,
        ticker="aapl",  # lowercased on input
        expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"),
        right="C",
        spot_price=Decimal("180.00"),
        bid=Decimal("3.18"),
        ask=Decimal("3.22"),
        open_interest=12000,
        implied_volatility=0.28,
    )
    assert rp is not None
    assert rp.source_id == "thetadata"
    assert rp.source_event_id == "thetadata-20240115-12345"
    # 14:00 ET on 2024-01-15 = 19:00 UTC (EST, UTC-5)
    assert rp.timestamp == datetime(2024, 1, 15, 19, 0, 0, tzinfo=UTC)
    assert rp.ticker == "AAPL"  # uppercased
    assert rp.option_type == "call"
    assert rp.strike == Decimal("170.00")
    assert rp.expiry == date(2024, 2, 16)
    assert rp.dte == 32  # Jan 15 → Feb 16
    assert rp.spot_price == Decimal("180.00")
    # premium_paid = 3.20 × 5 × 100 = 1600
    assert rp.premium_paid == Decimal("1600.00")
    assert rp.option_price == Decimal("3.20")
    assert rp.bid == Decimal("3.18")
    assert rp.ask == Decimal("3.22")
    # Phase 3.5.5 A1: fill_side is now inferred from the NBBO. Price
    # 3.20 sits exactly at the 3.18/3.22 midpoint → "midpoint".
    assert rp.fill_side == "midpoint"
    assert rp.exchange == "NYSE"
    assert rp.implied_volatility == 0.28
    assert rp.open_interest == 12000
    assert rp.is_iso is False


def test_happy_path_put_trade() -> None:
    """A put trade maps option_type='put'."""
    row = _regular_trade()
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="SPY", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("470.00"), right="P",
        spot_price=Decimal("475.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is not None
    assert rp.option_type == "put"


def test_premium_paid_uses_occ_contract_size() -> None:
    """premium_paid = price × size × 100 (OCC equity-options multiplier)."""
    row = _regular_trade(price=Decimal("2.50"), size=10)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="QQQ", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("400.00"), right="C",
        spot_price=Decimal("410.00"), bid=Decimal("2.48"),
        ask=Decimal("2.52"),
    )
    assert rp is not None
    # 2.50 × 10 × 100 = 2500
    assert rp.premium_paid == Decimal("2500.00")


def test_unknown_exchange_code_in_mapping() -> None:
    """Trade with unknown exchange code 99 maps to 'thetadata_99'."""
    row = _regular_trade(exchange=99)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is not None
    assert rp.exchange == "thetadata_99"


# ---------------------------------------------------------------------------
# Filtering — drop conditions / extended hours / negative DTE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_condition", [2, 7, 12, 13])
def test_drop_conditions_return_none(bad_condition: int) -> None:
    """A drop-set condition code returns None."""
    row = _regular_trade(condition=bad_condition)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is None


def test_extended_hours_trade_returns_none() -> None:
    """A trade at 18:00 ET (post-market) returns None."""
    row = _regular_trade(ms_of_day=(18 * 3600) * 1000)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is None


def test_negative_dte_returns_none() -> None:
    """A trade with trade_date past expiry → None (defensive)."""
    # Trade on 2024-02-20, expiry 2024-02-16: DTE = -4
    row = _regular_trade(date=20240220)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is None


def test_zero_dte_passes() -> None:
    """A trade on the expiry date itself (DTE=0) passes through."""
    row = _regular_trade(date=20240216)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is not None
    assert rp.dte == 0


def test_at_open_boundary_passes() -> None:
    """09:30:00 ET (market open exactly) → keep."""
    row = _regular_trade(ms_of_day=(9 * 3600 + 30 * 60) * 1000)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is not None


def test_at_close_boundary_passes() -> None:
    """16:00:00 ET (market close exactly) → keep."""
    row = _regular_trade(ms_of_day=(16 * 3600) * 1000)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is not None


def test_one_ms_before_open_drops() -> None:
    row = _regular_trade(ms_of_day=(9 * 3600 + 30 * 60) * 1000 - 1)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is None


def test_one_second_after_close_drops() -> None:
    row = _regular_trade(ms_of_day=(16 * 3600 + 1) * 1000)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("170.00"), right="C",
        spot_price=Decimal("180.00"), bid=Decimal("3.18"),
        ask=Decimal("3.22"),
    )
    assert rp is None


# ---------------------------------------------------------------------------
# Phase 3.3.7.2 — v3 REST helpers
# ---------------------------------------------------------------------------


def test_iso_timestamp_to_utc_datetime_summer() -> None:
    """ET ISO 09:30 in July → 13:30 UTC (DST)."""
    dt = iso_timestamp_to_utc_datetime("2024-07-15T09:30:00.000")
    assert dt.year == 2024
    assert dt.month == 7
    assert dt.day == 15
    assert dt.hour == 13  # 09:30 ET DST = 13:30 UTC
    assert dt.minute == 30
    assert dt.tzinfo is UTC


def test_iso_timestamp_to_utc_datetime_winter() -> None:
    """ET ISO 09:30 in January → 14:30 UTC (EST)."""
    dt = iso_timestamp_to_utc_datetime("2024-01-15T09:30:00.000")
    assert dt.hour == 14  # 09:30 EST = 14:30 UTC
    assert dt.minute == 30
    assert dt.tzinfo is UTC


def test_iso_timestamp_to_utc_datetime_with_milliseconds() -> None:
    """Sub-second precision survives the conversion."""
    dt = iso_timestamp_to_utc_datetime("2024-07-15T09:30:00.123")
    assert dt.microsecond == 123_000


def test_iso_timestamp_to_utc_datetime_with_tz_marker_honoured() -> None:
    """If v3 ever includes 'Z' or '+00:00' suffix, honour it directly."""
    dt = iso_timestamp_to_utc_datetime("2024-07-15T13:30:00+00:00")
    assert dt.hour == 13  # already UTC, no ET re-interpretation
    assert dt.tzinfo is UTC


def test_utc_datetime_to_et_date_ms_round_trip() -> None:
    """Full round-trip: ET ISO → UTC → (date, ms_of_day) → UTC again."""
    iso = "2024-01-15T09:30:00.000"
    utc1 = iso_timestamp_to_utc_datetime(iso)
    date_yyyymmdd, ms_of_day = utc_datetime_to_et_date_ms(utc1)
    utc2 = et_ms_to_utc_datetime(date_yyyymmdd=date_yyyymmdd, ms_of_day=ms_of_day)
    assert utc1 == utc2
    assert date_yyyymmdd == 20240115
    assert ms_of_day == (9 * 3600 + 30 * 60) * 1000


def test_utc_datetime_to_et_date_ms_rejects_naive_dt() -> None:
    naive = datetime(2024, 1, 15, 14, 30)
    with pytest.raises(ValueError, match="tz-aware"):
        utc_datetime_to_et_date_ms(naive)


def test_format_v3_strike_param_170_dollars() -> None:
    """$170.00 → "170.00"."""
    assert format_v3_strike_param(Decimal("170.00")) == "170.00"
    assert format_v3_strike_param(Decimal("170")) == "170.00"


def test_format_v3_strike_param_170_50() -> None:
    """$170.50 → "170.50"."""
    assert format_v3_strike_param(Decimal("170.50")) == "170.50"


def test_format_v3_strike_param_rejects_fractional_cent() -> None:
    """Half-pennies are not v3-supported; reject like v2 (J4)."""
    with pytest.raises(ValueError, match="2 decimals"):
        format_v3_strike_param(Decimal("170.005"))


def test_format_v3_strike_param_zero() -> None:
    """$0.00 strike formats correctly (degenerate but valid)."""
    assert format_v3_strike_param(Decimal("0")) == "0.00"


def test_format_v3_right_call() -> None:
    assert format_v3_right("call") == "call"


def test_format_v3_right_put() -> None:
    assert format_v3_right("put") == "put"


def test_format_v3_right_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        format_v3_right("CALL")  # type: ignore[arg-type]


def test_trade_row_from_v3_dict_happy_path() -> None:
    """v3 trade response object → TradeRow with same shape as v2 path."""
    obj = {
        "symbol": "AAPL",
        "expiration": "2024-11-08",
        "strike": 220.00,
        "right": "call",
        "timestamp": "2024-11-04T09:30:00.000",
        "sequence": 12345,
        "ext_condition1": 0,
        "ext_condition2": 0,
        "ext_condition3": 0,
        "ext_condition4": 0,
        "condition": 0,
        "size": 1,
        "exchange": 7,
        "price": 12.50,
    }
    row = trade_row_from_v3_dict(obj)
    assert row.sequence == 12345
    assert row.condition == 0
    assert row.size == 1
    assert row.exchange == 7
    assert row.price == Decimal("12.5")
    assert row.date_yyyymmdd == 20241104
    # 09:30 ET DST = ms_of_day 09:30
    assert row.ms_of_day == (9 * 3600 + 30 * 60) * 1000


def test_trade_row_from_v3_dict_missing_timestamp_raises() -> None:
    """timestamp is required; missing → ValueError."""
    obj = {
        "sequence": 12345, "condition": 0, "size": 1,
        "exchange": 7, "price": 12.50,
    }
    with pytest.raises(ValueError, match="timestamp"):
        trade_row_from_v3_dict(obj)


def test_trade_row_from_v3_dict_drops_redundant_fields() -> None:
    """symbol/expiration/strike/right are dropped silently (J2)."""
    obj = {
        "symbol": "AAPL", "expiration": "2024-11-08",
        "strike": 220.00, "right": "call",
        "timestamp": "2024-11-04T09:30:00.000",
        "sequence": 1, "condition": 0, "size": 1,
        "exchange": 7, "price": 1.0,
    }
    row = trade_row_from_v3_dict(obj)
    assert "symbol" not in row.model_dump()
    assert row.sequence == 1


def test_quote_row_from_v3_dict_happy_path() -> None:
    obj = {
        "timestamp": "2024-11-04T09:30:00.000",
        "bid": 1.40, "ask": 1.55,
        "bid_size": 10, "ask_size": 12,
        "bid_exchange": 7, "ask_exchange": 7,
    }
    row = quote_row_from_v3_dict(obj)
    assert row.bid == Decimal("1.4")
    assert row.ask == Decimal("1.55")
    assert row.bid_size == 10
    assert row.ask_size == 12
    assert row.ms_of_day == (9 * 3600 + 30 * 60) * 1000


def test_quote_row_from_v3_dict_drops_redundant_fields() -> None:
    """v3 quote response may include symbol/expiration/strike/right/timestamp."""
    obj = {
        "symbol": "AAPL", "expiration": "2024-11-08",
        "strike": 220.00, "right": "call",
        "timestamp": "2024-11-04T09:30:00.000",
        "bid": 1.40, "ask": 1.55,
        "bid_exchange": 7, "ask_exchange": 7,
    }
    row = quote_row_from_v3_dict(obj)
    assert row.bid == Decimal("1.4")


def test_quote_row_from_v3_dict_invalid_timestamp_type() -> None:
    obj = {"timestamp": 123, "bid": 1.0, "ask": 1.5}
    with pytest.raises(ValueError, match="timestamp"):
        quote_row_from_v3_dict(obj)


def test_v3_trade_row_feeds_existing_map_function() -> None:
    """End-to-end: v3 dict → TradeRow → RawPrint via the unchanged
    ``map_thetadata_trade_to_rawprint``. Pinning that the v2/v3
    boundary unification works.
    """
    obj = {
        "symbol": "AAPL", "expiration": "2024-11-08",
        "strike": 220.00, "right": "call",
        "timestamp": "2024-01-16T13:30:00.000",  # 09:30 ET winter
        "sequence": 1, "condition": 0, "size": 5,
        "exchange": 43, "price": 1.85,
    }
    row = trade_row_from_v3_dict(obj)
    rp = map_thetadata_trade_to_rawprint(
        trade_row=row, ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("220.00"), right="C",
        spot_price=Decimal("220.00"), bid=Decimal("1.80"),
        ask=Decimal("1.90"),
    )
    assert rp is not None
    assert rp.ticker == "AAPL"
    assert rp.option_type == "call"
    assert rp.strike == Decimal("220.00")
    assert rp.option_price == Decimal("1.85")
    assert rp.dte == 31  # Jan 16 → Feb 16



# ---------------------------------------------------------------------------
# Phase 3.5.5 A1 — fill_side classification from the NBBO
# ---------------------------------------------------------------------------


def test_classify_fill_side_at_or_above_ask() -> None:
    assert classify_fill_side(Decimal("3.25"), Decimal("3.18"), Decimal("3.22")) == "above_ask"
    assert classify_fill_side(Decimal("3.22"), Decimal("3.18"), Decimal("3.22")) == "above_ask"


def test_classify_fill_side_at_or_below_bid() -> None:
    assert classify_fill_side(Decimal("3.10"), Decimal("3.18"), Decimal("3.22")) == "below_bid"
    assert classify_fill_side(Decimal("3.18"), Decimal("3.18"), Decimal("3.22")) == "below_bid"


def test_classify_fill_side_within_spread() -> None:
    # bid 3.18 / ask 3.22 → midpoint 3.20
    assert classify_fill_side(Decimal("3.21"), Decimal("3.18"), Decimal("3.22")) == "at_ask"
    assert classify_fill_side(Decimal("3.19"), Decimal("3.18"), Decimal("3.22")) == "at_bid"
    assert classify_fill_side(Decimal("3.20"), Decimal("3.18"), Decimal("3.22")) == "midpoint"


def test_classify_fill_side_unknown_on_bad_quote() -> None:
    # Missing / non-positive / crossed quotes cannot be classified.
    assert classify_fill_side(Decimal("3.20"), Decimal("0"), Decimal("3.22")) == "unknown"
    assert classify_fill_side(Decimal("3.20"), Decimal("3.18"), Decimal("0")) == "unknown"
    assert classify_fill_side(Decimal("3.20"), Decimal("3.30"), Decimal("3.22")) == "unknown"
