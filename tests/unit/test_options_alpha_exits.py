"""Exit rules, checked against arithmetic worked out by hand.

The position under test is the real card this engine produced on 2026-09-22: a
SPY 775/776 call debit spread entered at 0.58 per share, one structure, 2.60 of
commission, whose structural maximum profit is 0.42 per share (1.00 width minus
the 0.58 debit).

With the frozen profile (stop 50% of debit, target +100% of debit, five trading
days, close at 7 DTE) that gives:

    stop level   = 0.58 * 0.50            = 0.29
    target level = 0.58 * 2.00 = 1.16, capped by the structure ceiling
                 = 0.58 + 0.42            = 1.00

Every expectation below is derived from those two numbers.
"""

from __future__ import annotations

import pathlib
from datetime import date
from decimal import Decimal

import pytest

from uoa_detector.options_alpha.exits import (
    DailyObservation,
    ExitReason,
    ExitVariantId,
    evaluate_exit,
)
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings

ENTRY = Decimal("0.58")
MAX_PROFIT = Decimal("0.42")
COMMISSION = Decimal("2.60")


@pytest.fixture(scope="module")
def settings() -> OptionsAlphaSettings:
    return load_settings(pathlib.Path("profiles/options_alpha_v1.yaml"))


def obs(
    day: int,
    close: str | None,
    *,
    high: str | None = None,
    low: str | None = None,
    dte: int = 17,
    complete: bool = True,
) -> DailyObservation:
    return DailyObservation(
        day=date(2026, 9, day),
        exit_value=None if close is None else Decimal(close),
        dte=dte,
        high_value=None if high is None else Decimal(high),
        low_value=None if low is None else Decimal(low),
        is_complete=complete,
    )


def run(
    variant: ExitVariantId, observations: tuple[DailyObservation, ...], settings: OptionsAlphaSettings
):
    return evaluate_exit(
        variant=variant,
        entry_debit=ENTRY,
        observations=observations,
        settings=settings,
        max_profit_per_share=MAX_PROFIT,
        commission_usd=COMMISSION,
    )


def test_time_only_holds_to_the_frozen_horizon(settings: OptionsAlphaSettings) -> None:
    """Five sessions, no thresholds, exit on the fifth close of 0.75.

        pnl/share = 0.75 - 0.58 = 0.17
        pnl usd   = 0.17 * 100 - 2.60 = 14.40
    """
    days = tuple(
        obs(d, c) for d, c in ((23, "0.60"), (24, "0.62"), (25, "0.55"), (28, "0.70"), (29, "0.75"))
    )
    out = run(ExitVariantId.TIME_ONLY, days, settings)

    assert out.reason is ExitReason.TIME
    assert out.exit_value == Decimal("0.75")
    assert out.pnl_per_share == Decimal("0.17")
    assert out.pnl_usd == Decimal("14.40")
    assert out.held_trading_days == 5


def test_the_stop_is_taken_at_the_stop_level_not_at_the_worse_close(
    settings: OptionsAlphaSettings,
) -> None:
    """Day two trades down to 0.25, through the 0.29 stop.

    The exit is booked AT the stop, not at the lower close: a stop that fills at
    whatever the day happened to close at would report a loss the rule never
    accepted.

        pnl/share = 0.29 - 0.58 = -0.29
        pnl usd   = -29.00 - 2.60 = -31.60
    """
    days = (obs(23, "0.60", high="0.64", low="0.58"), obs(24, "0.25", high="0.55", low="0.20"))
    out = run(ExitVariantId.TIME_AND_STOP, days, settings)

    assert out.reason is ExitReason.STOP
    assert out.exit_value == Decimal("0.29")
    assert out.pnl_usd == Decimal("-31.60")
    assert out.held_trading_days == 2


def test_the_target_is_capped_by_the_structures_ceiling(settings: OptionsAlphaSettings) -> None:
    """+100% of the debit would be 1.16, but the spread cannot exceed 1.00.

    Exiting at the capped target returns exactly the card's stated maximum:
        pnl usd = (1.00 - 0.58) * 100 - 2.60 = 39.40
    """
    days = (obs(23, "0.70", high="0.72", low="0.66"), obs(24, "0.95", high="1.02", low="0.88"))
    out = run(ExitVariantId.TIME_TARGET_STOP, days, settings)

    assert out.reason is ExitReason.TARGET
    assert out.exit_value == Decimal("1.00")
    assert out.pnl_usd == Decimal("39.40")


def test_both_levels_inside_one_day_is_ambiguous_and_takes_the_stop(
    settings: OptionsAlphaSettings,
) -> None:
    """Day two ranges 0.20 to 1.05: it touched the stop AND the target.

    Daily data cannot say which came first. Booking the target would be inventing
    the order in our own favour, so the conservative branch is applied and the
    ambiguity is reported rather than hidden.
    """
    days = (obs(23, "0.60", high="0.64", low="0.58"), obs(24, "0.90", high="1.05", low="0.20"))
    out = run(ExitVariantId.TIME_TARGET_STOP, days, settings)

    assert out.ambiguous_same_day_touch is True
    assert out.reason is ExitReason.STOP
    assert out.pnl_usd == Decimal("-31.60")
    assert any("sira bilinmiyor" in note for note in out.notes)


def test_a_close_only_day_says_intraday_touches_are_invisible(
    settings: OptionsAlphaSettings,
) -> None:
    """Without a range the close stands in — and the limitation is stated."""
    days = (obs(23, "0.20"),)
    out = run(ExitVariantId.TIME_AND_STOP, days, settings)

    assert out.reason is ExitReason.STOP
    assert any("gun ici dokunuslar gorunmez" in note for note in out.notes)


def test_an_unpriceable_day_is_counted_never_treated_as_zero(
    settings: OptionsAlphaSettings,
) -> None:
    """A missing quote is missing information, not a worthless position."""
    days = (obs(23, None, complete=False), obs(24, "0.70", high="0.72", low="0.68"))
    out = run(ExitVariantId.TIME_ONLY, days, settings)

    assert out.unpriced_days == 1
    assert out.exit_value == Decimal("0.70")
    assert out.reason is ExitReason.TIME


def test_no_priceable_day_is_not_a_successful_trade(settings: OptionsAlphaSettings) -> None:
    """A position we could never price did not break even — it has no outcome."""
    days = (obs(23, None, complete=False), obs(24, None, complete=False))
    out = run(ExitVariantId.TIME_ONLY, days, settings)

    assert out.reason is ExitReason.NO_EXIT_DATA
    assert out.pnl_usd is None
    assert out.return_on_risk is None


def test_the_dte_floor_closes_before_expiry(settings: OptionsAlphaSettings) -> None:
    """Never hold into the last days, where the spread widens and assignment starts."""
    days = (obs(23, "0.60", high="0.62", low="0.58", dte=9), obs(24, "0.64", high="0.66", low="0.62", dte=7))
    out = run(ExitVariantId.TIME_ONLY, days, settings)

    assert out.reason is ExitReason.DTE_FLOOR
    assert out.exit_value == Decimal("0.64")


def test_mfe_and_mae_use_the_range_so_a_round_trip_stays_visible(
    settings: OptionsAlphaSettings,
) -> None:
    """"It was up 40% before it came back" must not collapse into the close."""
    days = (obs(23, "0.60", high="0.95", low="0.40"), obs(24, "0.62", high="0.70", low="0.35"))
    out = run(ExitVariantId.TIME_ONLY, days, settings)

    assert out.mfe_per_share == Decimal("0.95")
    assert out.mae_per_share == Decimal("0.35")
