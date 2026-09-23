"""Exit rules, evaluated against what the data can actually support.

The backtest engine this repo already had raises ``NotImplementedError`` for
``take_profit_or_stop``, so no stop-or-target rule has ever been tested here. This
module supplies that as working code for the options-alpha scope, **beside** the
legacy engine rather than replacing it: the old fixed-window path keeps its
behaviour and its recorded verdicts.

What a stop and a target track is stated, not implied. Both track the
**structure's own exit value** — what the package could be closed for — never the
underlying price. On a debit spread the underlying moving the right way is not the
same as the position being worth more, and a rule that watches spot while the
money lives in the package is measuring the wrong thing.

The hard part is honesty about daily data. A day gives one exit value; it does not
say what happened inside the day. So:

* If a day's value would trigger BOTH the target and the stop, the order is
  **unknowable**. This module refuses to guess, marks the outcome ambiguous, and
  applies the **stop** — the conservative branch — rather than booking the profit.
* A threshold seen on day D is exited at day D's value, and the delay between
  seeing and transacting is recorded as an explicit assumption, not hidden.
* MFE and MAE are the best and worst the position was ever worth inside the hold,
  which is what makes "it was up 40% before it stopped out" visible instead of
  collapsing into a single closing number.

Pure: no I/O, no clock, no network. Observations are handed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from uoa_detector.options_alpha.settings import OptionsAlphaSettings

CENTS = Decimal("0.01")


class ExitReason(StrEnum):
    TARGET = "target"
    STOP = "stop"
    TIME = "time"
    DTE_FLOOR = "dte_floor"
    NO_EXIT_DATA = "no_exit_data"
    STILL_OPEN = "still_open"


class ExitVariantId(StrEnum):
    """Frozen before results. Each one is a separate trial in the search budget."""

    TIME_ONLY = "time_only"
    TIME_AND_STOP = "time_and_stop"
    TIME_TARGET_STOP = "time_target_stop"


@dataclass(frozen=True)
class DailyObservation:
    """One session's closable value for the whole structure, per share.

    ``exit_value`` is what the package could be sold for at the close using the
    frozen exit sides (long leg at the bid, short leg at the ask).

    ``high_value`` and ``low_value`` are the best and worst the package was worth
    inside the day. They exist because a threshold test on the CLOSE alone cannot
    see a level that was touched and given back — and, more importantly, because
    the "both levels hit in one interval" case is undetectable without them: the
    target always sits above the stop, so a single closing value can never be on
    both sides at once. Without a range, that ambiguity branch is unreachable
    code pretending to be a safeguard.

    Both are optional. When absent the close stands in for them and a note says
    so, because an intraday touch is then invisible rather than absent.

    ``is_complete`` is False when a leg had no usable quote that day — a day we
    cannot price is not a day the position was worth zero.
    """

    day: date
    exit_value: Decimal | None
    dte: int
    high_value: Decimal | None = None
    low_value: Decimal | None = None
    is_complete: bool = True


@dataclass(frozen=True)
class ExitOutcome:
    """What happened, including what could not be known."""

    variant: ExitVariantId
    reason: ExitReason
    exit_day: date | None
    exit_value: Decimal | None
    entry_debit: Decimal
    pnl_per_share: Decimal | None
    pnl_usd: Decimal | None
    return_on_risk: float | None
    held_trading_days: int
    mfe_per_share: Decimal | None
    mae_per_share: Decimal | None
    ambiguous_same_day_touch: bool
    unpriced_days: int
    notes: tuple[str, ...]


def _levels(
    entry_debit: Decimal, settings: OptionsAlphaSettings, max_profit_per_share: Decimal | None
) -> tuple[Decimal, Decimal]:
    """(stop level, target level) as package values per share.

    The target is capped by the structure's own ceiling. A defined-risk spread
    cannot be worth more than its width, so a target above that is an instruction
    to wait for something that cannot happen.
    """
    stop = (entry_debit * Decimal(str(settings.exit.stop_pct_of_entry_debit)) / Decimal(100)).quantize(CENTS)
    target = (
        entry_debit * (Decimal(1) + Decimal(str(settings.exit.target_pct_of_entry_debit)) / Decimal(100))
    ).quantize(CENTS)
    if max_profit_per_share is not None:
        ceiling = (entry_debit + max_profit_per_share).quantize(CENTS)
        target = min(target, ceiling)
    return stop, target


def evaluate_exit(
    *,
    variant: ExitVariantId,
    entry_debit: Decimal,
    observations: tuple[DailyObservation, ...],
    settings: OptionsAlphaSettings,
    multiplier: int = 100,
    structures: int = 1,
    max_profit_per_share: Decimal | None = None,
    commission_usd: Decimal = Decimal(0),
) -> ExitOutcome:
    """Walk the hold day by day and apply one frozen exit variant.

    ``observations`` must be ordered oldest first and must NOT include the entry
    day: a position cannot exit on the value that priced its own entry.
    """
    stop_level, target_level = _levels(entry_debit, settings, max_profit_per_share)
    max_days = settings.exit.primary_hold_trading_days
    dte_floor = settings.exit.close_at_dte

    notes: list[str] = [
        f"stop {stop_level}/hisse, hedef {target_level}/hisse — ikisi de yapinin cikis degerini izler",
        "gunluk veri: gun ici sira bilinmiyor",
    ]
    if max_profit_per_share is not None and target_level < (
        entry_debit * (Decimal(1) + Decimal(str(settings.exit.target_pct_of_entry_debit)) / Decimal(100))
    ).quantize(CENTS):
        notes.append("hedef yapinin tavaniyla sinirlandi")

    mfe: Decimal | None = None
    mae: Decimal | None = None
    unpriced = 0
    ambiguous = False
    held = 0

    exit_day: date | None = None
    exit_value: Decimal | None = None
    reason = ExitReason.STILL_OPEN

    for observation in observations:
        if held >= max_days:
            break
        held += 1

        if observation.exit_value is None or not observation.is_complete:
            # A day we could not price is not a day the position was worthless.
            # It is counted and disclosed; it never becomes a zero or a fill.
            unpriced += 1
            continue

        value = observation.exit_value
        # Thresholds are tested against the day's RANGE, not its close. A level
        # touched and given back still happened.
        high = observation.high_value if observation.high_value is not None else value
        low = observation.low_value if observation.low_value is not None else value
        ranged = observation.high_value is not None and observation.low_value is not None

        mfe = high if mfe is None else max(mfe, high)
        mae = low if mae is None else min(mae, low)

        hits_target = variant is ExitVariantId.TIME_TARGET_STOP and high >= target_level
        hits_stop = (
            variant in (ExitVariantId.TIME_AND_STOP, ExitVariantId.TIME_TARGET_STOP)
            and low <= stop_level
        )
        if (hits_target or hits_stop) and not ranged:
            notes.append(
                f"{observation.day}: yalnizca kapanis vardi, gun ici dokunuslar gorunmez"
            )

        if hits_target and hits_stop:
            # Both levels inside one daily bar. Which came first is genuinely
            # unknown, so the profitable branch is not assumed.
            ambiguous = True
            exit_day, exit_value, reason = observation.day, stop_level, ExitReason.STOP
            notes.append("ayni gun hem hedef hem stop dokunuldu — sira bilinmiyor, muhafazakar senaryo")
            break
        if hits_stop:
            exit_day, exit_value, reason = observation.day, stop_level, ExitReason.STOP
            break
        if hits_target:
            exit_day, exit_value, reason = observation.day, target_level, ExitReason.TARGET
            break
        if observation.dte <= dte_floor:
            exit_day, exit_value, reason = observation.day, value, ExitReason.DTE_FLOOR
            notes.append(f"vadeye {observation.dte} gun kaldi — zorunlu kapanis")
            break
        if held >= max_days:
            exit_day, exit_value, reason = observation.day, value, ExitReason.TIME
            break

    if reason is ExitReason.STILL_OPEN and observations:
        # The loop only falls through here when no rule fired. Two cases, and
        # neither may borrow an earlier day's value as the exit:
        #   * fewer observations than the horizon -> the position is still open;
        #   * the horizon was reached but its last day could not be priced -> the
        #     exit is UNKNOWN. Booking the last priced day as a "time" exit turned
        #     unknowns into realised P&L (H10 trade audit, 21 of 73 records).
        priced = [o for o in observations[:max_days] if o.exit_value is not None and o.is_complete]
        if held >= max_days:
            reason = ExitReason.NO_EXIT_DATA
            if priced:
                notes.append(
                    f"ufkun son gunu fiyatlanamadi — son fiyatli gun {priced[-1].day} "
                    f"({priced[-1].exit_value}) cikis olarak KULLANILMADI"
                )
            else:
                notes.append("elde tutma boyunca fiyatlanabilir gun yok — cikis kaydedilemedi")

    pnl_share: Decimal | None = None
    pnl_usd: Decimal | None = None
    ror: float | None = None
    if exit_value is not None:
        pnl_share = (exit_value - entry_debit).quantize(CENTS)
        gross = pnl_share * Decimal(multiplier) * Decimal(structures)
        pnl_usd = (gross - commission_usd).quantize(CENTS)
        risk = (entry_debit * Decimal(multiplier) * Decimal(structures)) + commission_usd
        if risk > 0:
            ror = float(pnl_usd / risk)

    return ExitOutcome(
        variant=variant,
        reason=reason,
        exit_day=exit_day,
        exit_value=exit_value,
        entry_debit=entry_debit,
        pnl_per_share=pnl_share,
        pnl_usd=pnl_usd,
        return_on_risk=ror,
        held_trading_days=held,
        mfe_per_share=mfe,
        mae_per_share=mae,
        ambiguous_same_day_touch=ambiguous,
        unpriced_days=unpriced,
        notes=tuple(notes),
    )
