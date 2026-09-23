"""Cost and sizing arithmetic, checked against numbers worked out by hand.

The task forbids verifying a calculator with its own output, so every expected
value below is computed in the docstring from the quoted prices and the frozen
profile, by hand, and then asserted. If the implementation changes what a
structure costs, these fail — which is the point.

Each refusal test names the specific way a result could be flattered if the
refusal were missing.
"""

from __future__ import annotations

import pathlib
from datetime import date
from decimal import Decimal

import pytest

from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings
from uoa_detector.options_alpha.structures import (
    ContractQuote,
    Leg,
    Right,
    StructureKind,
    UnpriceableError,
    price_structure,
    size_position,
    structure_kind,
)

SESSION = date(2026, 9, 22)
EXPIRY = date(2026, 10, 16)
PROFILE = pathlib.Path("profiles/options_alpha_v1.yaml")


@pytest.fixture(scope="module")
def settings() -> OptionsAlphaSettings:
    return load_settings(PROFILE)


def quote(
    symbol: str,
    right: Right,
    strike: str,
    bid: str | None,
    ask: str | None,
    *,
    as_of: date = SESSION,
    expiry: date = EXPIRY,
    multiplier: int = 100,
) -> ContractQuote:
    return ContractQuote(
        option_symbol=symbol,
        underlying="SPY",
        right=right,
        strike=Decimal(strike),
        expiry=expiry,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        as_of=as_of,
        multiplier=multiplier,
    )


# --------------------------------------------------------------------- pricing


def test_long_call_costs_what_the_ask_plus_slippage_and_two_commissions_say(
    settings: OptionsAlphaSettings,
) -> None:
    """Long 770 call, bid 11.50 / ask 12.00, worked out by hand.

    A long leg opens at the ask and closes at the bid, so before fees the package
    is 12.00 out and 11.50 back. The latency haircut is 2% of the package mid:

        entry     = 12.00 * 1.02 = 12.2400 -> 12.24
        exit      = 11.50 * 0.98 = 11.2700 -> 11.27

    One leg, opened and closed, at 0.65 a side: 0.65 * 1 * 2 = 1.30.

        entry cost = 12.24 * 100 = 1224.00
        max loss   = 1224.00 + 1.30 = 1225.30
        breakeven  = 770 + 12.24 = 782.24

    A long call's upside is not bounded, so max profit must be None rather than
    a large number that would quietly cap the payoff in a report.
    """
    legs = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=True),)
    kind, price = price_structure(legs, settings)

    assert kind is StructureKind.LONG_CALL
    assert price.entry_debit == Decimal("12.24")
    assert price.exit_credit == Decimal("11.27")
    assert price.commission_usd == Decimal("1.30")
    assert price.entry_cost_usd == Decimal("1224.00")
    assert price.max_loss_usd == Decimal("1225.30")
    assert price.max_profit_usd is None
    assert price.breakeven_underlying == Decimal("782.24")


def test_bull_call_debit_prices_the_package_not_the_two_legs_separately(
    settings: OptionsAlphaSettings,
) -> None:
    """Long 770 call (11.50/12.00), short 775 call (8.80/9.20), by hand.

    The package opens at the long ask minus the short bid and closes at the long
    bid minus the short ask:

        entry raw = 12.00 - 8.80 = 3.20  ->  3.20 * 1.02 = 3.2640 -> 3.26
        exit  raw = 11.50 - 9.20 = 2.30  ->  2.30 * 0.98 = 2.2540 -> 2.25

    Two legs, each opened and closed: 0.65 * 2 * 2 = 2.60.

        entry cost = 3.26 * 100 = 326.00
        max loss   = 326.00 + 2.60 = 328.60
        width      = 775 - 770 = 5
        max profit = 5 * 100 - 326.00 - 2.60 = 171.40
        breakeven  = 770 + 3.26 = 773.26

    The spread costs a quarter of the bare long above, which is the whole reason
    the structure exists, and the fees are visible rather than folded into it.
    """
    legs = (
        Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=True),
        Leg(quote("SPY261016C00775000", Right.CALL, "775", "8.80", "9.20"), is_long=False),
    )
    kind, price = price_structure(legs, settings)

    assert kind is StructureKind.BULL_CALL_DEBIT
    assert price.entry_debit == Decimal("3.26")
    assert price.exit_credit == Decimal("2.25")
    assert price.commission_usd == Decimal("2.60")
    assert price.entry_cost_usd == Decimal("326.00")
    assert price.max_loss_usd == Decimal("328.60")
    assert price.max_profit_usd == Decimal("171.40")
    assert price.breakeven_underlying == Decimal("773.26")


def test_bear_put_debit_breakeven_sits_below_the_long_strike(
    settings: OptionsAlphaSettings,
) -> None:
    """Long 770 put (11.50/12.00), short 765 put (8.80/9.20).

    Same package arithmetic as the call spread — entry 3.26, exit 2.24 — but a
    put spread profits downward, so the breakeven is the long strike MINUS the
    debit: 770 - 3.26 = 766.74. Reusing the call's plus sign here would report a
    breakeven the trade can never reach.
    """
    legs = (
        Leg(quote("SPY261016P00770000", Right.PUT, "770", "11.50", "12.00"), is_long=True),
        Leg(quote("SPY261016P00765000", Right.PUT, "765", "8.80", "9.20"), is_long=False),
    )
    kind, price = price_structure(legs, settings)

    assert kind is StructureKind.BEAR_PUT_DEBIT
    assert price.entry_debit == Decimal("3.26")
    assert price.breakeven_underlying == Decimal("766.74")
    assert price.max_profit_usd == Decimal("171.40")


def test_a_wider_market_is_never_cheaper_to_trade(settings: OptionsAlphaSettings) -> None:
    """Both quotes share an ask of 12.00, so opening costs the same. The exit differs.

    This started as a wrong assertion and caught a real modelling defect. The
    first cost model took its latency haircut from the package MID, so the wide
    quote below — with its lower mid — drew a smaller haircut and came out
    CHEAPER to open than the tight one. A worse market must never be cheaper.

    What genuinely separates these two is the round trip: the wide quote returns
    9.80 on exit against 11.66, so its cost to get in and out is far higher. The
    entry is identical because a long leg opens at the ask, and both asks are
    12.00 — asserting otherwise would have been asserting a bug.
    """
    tight = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.90", "12.00"), is_long=True),)
    wide = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "10.00", "12.00"), is_long=True),)

    _, tight_price = price_structure(tight, settings)
    _, wide_price = price_structure(wide, settings)

    assert wide_price.entry_debit == tight_price.entry_debit
    assert wide_price.exit_credit < tight_price.exit_credit

    tight_round_trip = tight_price.entry_debit - tight_price.exit_credit
    wide_round_trip = wide_price.entry_debit - wide_price.exit_credit
    assert wide_round_trip > tight_round_trip


def test_entry_reads_the_ask_and_never_the_bid(settings: OptionsAlphaSettings) -> None:
    """The sides guard: a bid-side entry on this quote would cost ~10.20, not 12.24."""
    legs = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "10.00", "12.00"), is_long=True),)
    _, price = price_structure(legs, settings)

    assert price.entry_debit >= Decimal("12.00")
    assert price.exit_credit <= Decimal("10.00")


# -------------------------------------------------------------------- refusals


def test_a_crossed_quote_is_refused_rather_than_priced(settings: OptionsAlphaSettings) -> None:
    """Ask below bid is a data condition. Pricing it would book a free option."""
    legs = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "12.00", "11.50"), is_long=True),)
    with pytest.raises(UnpriceableError, match="capraz"):
        price_structure(legs, settings)


def test_a_missing_quote_is_refused_rather_than_filled_from_the_other_side(
    settings: OptionsAlphaSettings,
) -> None:
    """No ask means no entry price. Falling back to the bid or the mid invents one."""
    legs = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", None), is_long=True),)
    with pytest.raises(UnpriceableError):
        price_structure(legs, settings)


def test_a_zero_bid_leg_is_refused(settings: OptionsAlphaSettings) -> None:
    """A zero bid cannot be sold. Treating it as 0.00 credit would flatter the debit."""
    legs = (
        Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=True),
        Leg(quote("SPY261016C00775000", Right.CALL, "775", "0", "0.05"), is_long=False),
    )
    with pytest.raises(UnpriceableError):
        price_structure(legs, settings)


def test_legs_from_different_sessions_are_refused(settings: OptionsAlphaSettings) -> None:
    """Two sessions is the cheapest way to build a package that never existed."""
    legs = (
        Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=True),
        Leg(
            quote(
                "SPY261016C00775000",
                Right.CALL,
                "775",
                "8.80",
                "9.20",
                as_of=date(2026, 9, 15),
            ),
            is_long=False,
        ),
    )
    with pytest.raises(UnpriceableError, match="farkli seans"):
        structure_kind(legs)


def test_legs_from_different_expiries_are_refused(settings: OptionsAlphaSettings) -> None:
    """A calendar is a different risk. v1 prices verticals only."""
    legs = (
        Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=True),
        Leg(
            quote(
                "SPY261023C00775000",
                Right.CALL,
                "775",
                "8.80",
                "9.20",
                expiry=date(2026, 10, 23),
            ),
            is_long=False,
        ),
    )
    with pytest.raises(UnpriceableError, match="vadesi ayni"):
        structure_kind(legs)


def test_a_credit_package_is_refused(settings: OptionsAlphaSettings) -> None:
    """Short 770 / long 775 calls quote as a credit. v1 is debit-only."""
    legs = (
        Leg(quote("SPY261016C00775000", Right.CALL, "775", "11.50", "12.00"), is_long=True),
        Leg(quote("SPY261016C00770000", Right.CALL, "770", "14.00", "14.40"), is_long=False),
    )
    with pytest.raises(UnpriceableError):
        price_structure(legs, settings)


def test_a_naked_short_leg_is_out_of_scope(settings: OptionsAlphaSettings) -> None:
    legs = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=False),)
    with pytest.raises(UnpriceableError, match="kisa"):
        structure_kind(legs)


# ---------------------------------------------------------------------- sizing


def test_one_structure_over_budget_sizes_to_zero_and_says_what_it_needs(
    settings: OptionsAlphaSettings,
) -> None:
    """R is 100 and this long call risks 1225.30, so the honest answer is zero.

    The number it WOULD need is reported as information. Reaching for a cheaper,
    further out-of-the-money strike to make a position fit is the failure this
    asserts against.
    """
    legs = (Leg(quote("SPY261016C00770000", Right.CALL, "770", "11.50", "12.00"), is_long=True),)
    _, price = price_structure(legs, settings)
    sizing = size_position(price, settings)

    assert sizing.structures == 0
    assert sizing.total_risk_usd == Decimal("0")
    assert sizing.minimum_budget_needed_usd == Decimal("1225.30")
    assert sizing.budget_usd == Decimal("100")


def test_a_cheap_spread_sizes_to_whole_structures_and_floors(
    settings: OptionsAlphaSettings,
) -> None:
    """Long 800 call (0.40/0.45), short 805 call (0.20/0.25), by hand.

        entry raw = 0.45 - 0.20 = 0.25  ->  0.25 * 1.02 = 0.2550 -> 0.26
        exit  raw = 0.40 - 0.25 = 0.15  ->  0.15 * 0.98 = 0.1470 -> 0.15
        entry cost= 0.26 * 100 = 26.00  commission = 2.60  max loss = 28.60

    100 / 28.60 = 3.49, so three structures, not three-and-a-bit. The floor is
    what keeps a fractional option from being invented.
    """
    legs = (
        Leg(quote("SPY261016C00800000", Right.CALL, "800", "0.40", "0.45"), is_long=True),
        Leg(quote("SPY261016C00805000", Right.CALL, "805", "0.20", "0.25"), is_long=False),
    )
    _, price = price_structure(legs, settings)
    sizing = size_position(price, settings)

    assert price.max_loss_usd == Decimal("28.60")
    assert sizing.structures == 3
    assert sizing.total_risk_usd == Decimal("85.80")
    assert sizing.minimum_budget_needed_usd is None


def test_sizing_never_exceeds_the_per_signal_cap(settings: OptionsAlphaSettings) -> None:
    """Even a nearly free structure stops at max_structures_per_signal."""
    legs = (
        Leg(quote("SPY261016C00800000", Right.CALL, "800", "0.06", "0.07"), is_long=True),
        Leg(quote("SPY261016C00805000", Right.CALL, "805", "0.01", "0.02"), is_long=False),
    )
    _, price = price_structure(legs, settings)
    sizing = size_position(price, settings)

    assert sizing.structures == settings.risk.max_structures_per_signal


# --------------------------------------------------------------------- profile


def test_the_profile_carries_its_own_hash_and_the_frozen_horizon(
    settings: OptionsAlphaSettings,
) -> None:
    """Every result must be attributable to the exact numbers that produced it."""
    assert len(settings.profile_sha256) == 64
    assert settings.exit.primary_hold_trading_days == 5
    assert settings.quality.tier == "B"
    assert settings.quality.research_window_start == "2026-05-13"
