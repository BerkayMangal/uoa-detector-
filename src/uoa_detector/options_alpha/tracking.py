"""Open PAPER position tracking: from per-leg daily quotes to an exit decision.

The live tracker (``webapp/board/options_paper.py``) fetches, stores and
schedules; this module only decides. It is pure: no I/O, no clock, no network.

Three rules carry the honesty of the whole loop:

* **The session calendar comes from outside the legs.** A day is a session
  because the market traded, not because the long leg happened to have a row.
  Deriving the days from the long leg's own history (what
  ``scripts/options_alpha_score_card.py`` did) makes a day on which that leg
  had no quote simply disappear, so it is never counted as unpriced and the
  hold horizon silently stretches. Here the caller hands in the calendar and a
  leg with no row on a calendar day makes that day unpriced.
* **Closable value is leg by leg:** long leg at the bid, short leg at the ask,
  the same frozen convention the research engine uses. It can be negative; that
  is recorded as it is, not floored.
* **An unknown exit stays unknown.** The exit decision is the fixed
  ``evaluate_exit``: short of the horizon the position is ``STILL_OPEN``; a
  horizon whose last day cannot be priced is ``NO_EXIT_DATA``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from uoa_detector.options_alpha.exits import (
    DailyObservation,
    ExitOutcome,
    ExitReason,
    ExitVariantId,
    evaluate_exit,
)
from uoa_detector.options_alpha.settings import OptionsAlphaSettings

CENTS = Decimal("0.01")

# The variant a PAPER card's own plan describes: a time limit, a stop and a
# target on the structure's value. The research families' PRIMARY metric is
# time_only; all three are recorded, this one decides the card's status.
CARD_PLAN_VARIANT = ExitVariantId.TIME_TARGET_STOP


@dataclass(frozen=True)
class LegQuote:
    """One leg's end-of-day NBBO on one session. Either side may be absent."""

    bid: Decimal | None
    ask: Decimal | None
    volume: int | None = None


@dataclass(frozen=True)
class TrackedLeg:
    occ_symbol: str
    is_long: bool
    strike: Decimal
    expiry: date


@dataclass(frozen=True)
class TrackedPosition:
    """What the tracker needs from a frozen PAPER card."""

    position_id: str
    underlying: str
    entry_session: date
    entry_debit: Decimal
    quantity: int
    commission_usd: Decimal
    legs: tuple[TrackedLeg, ...]

    @property
    def expiry(self) -> date:
        return min(leg.expiry for leg in self.legs)

    @property
    def max_profit_per_share(self) -> Decimal | None:
        """GROSS per-share ceiling of a two-leg vertical; ``None`` for a bare long."""
        if len(self.legs) != 2:
            return None
        width = abs(self.legs[0].strike - self.legs[1].strike)
        return (width - self.entry_debit).quantize(CENTS)


@dataclass(frozen=True)
class Mark:
    """One session's closable value, with the quotes it came from."""

    session: date
    closable_value: Decimal | None
    missing_legs: tuple[str, ...]
    quotes: Mapping[str, LegQuote]

    @property
    def is_complete(self) -> bool:
        return self.closable_value is not None


@dataclass(frozen=True)
class TrackResult:
    marks: tuple[Mark, ...]
    outcomes: Mapping[ExitVariantId, ExitOutcome]

    @property
    def plan(self) -> ExitOutcome:
        return self.outcomes[CARD_PLAN_VARIANT]

    @property
    def resolved(self) -> bool:
        """True once the card's own plan reached a final state (closed or unknown)."""
        return self.plan.reason is not ExitReason.STILL_OPEN


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def parse_historic_quotes(payload: Mapping[str, Any]) -> dict[date, LegQuote]:
    """``{session: LegQuote}`` from a ``/api/option-contract/{occ}/historic`` payload.

    Rows arrive under ``chains`` (or ``data``), newest first, prices as strings.
    A missing or unparseable side stays ``None``: never a zero.
    """
    raw = payload.get("chains")
    if not isinstance(raw, list):
        raw = payload.get("data")
    if not isinstance(raw, list):
        return {}
    out: dict[date, LegQuote] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = item.get("date")
        if not isinstance(day, str):
            continue
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            continue
        out.setdefault(
            parsed,
            LegQuote(bid=_dec(item.get("nbbo_bid")), ask=_dec(item.get("nbbo_ask")),
                     volume=_int(item.get("volume"))),
        )
    return out


def sessions_after(calendar: Iterable[date], entry: date, through: date) -> tuple[date, ...]:
    """Calendar sessions strictly after ``entry`` and on or before ``through``."""
    return tuple(sorted(d for d in set(calendar) if entry < d <= through))


def mark_session(
    position: TrackedPosition, session: date, quotes: Mapping[str, Mapping[date, LegQuote]]
) -> Mark:
    """The package's closable value on ``session``: long at the bid, short at the ask."""
    value = Decimal(0)
    missing: list[str] = []
    seen: dict[str, LegQuote] = {}
    for leg in position.legs:
        quote = quotes.get(leg.occ_symbol, {}).get(session)
        if quote is not None:
            seen[leg.occ_symbol] = quote
        price = None if quote is None else (quote.bid if leg.is_long else quote.ask)
        # A zero bid on the long leg is a real quote (nobody pays for it); a zero
        # or absent ask on the short leg is not a closable price.
        if price is None or (not leg.is_long and price <= 0):
            missing.append(leg.occ_symbol)
            continue
        value += price if leg.is_long else -price
    return Mark(
        session=session,
        closable_value=None if missing else value.quantize(CENTS),
        missing_legs=tuple(missing),
        quotes=seen,
    )


def track(
    position: TrackedPosition,
    sessions: Sequence[date],
    quotes: Mapping[str, Mapping[date, LegQuote]],
    settings: OptionsAlphaSettings,
) -> TrackResult:
    """Mark every session after entry and apply every frozen exit variant."""
    marks = tuple(mark_session(position, day, quotes) for day in sessions)
    observations = tuple(
        DailyObservation(
            day=m.session,
            exit_value=m.closable_value,
            dte=(position.expiry - m.session).days,
            is_complete=m.is_complete,
        )
        for m in marks
    )
    outcomes = {
        variant: evaluate_exit(
            variant=variant,
            entry_debit=position.entry_debit,
            observations=observations,
            settings=settings,
            structures=position.quantity,
            max_profit_per_share=position.max_profit_per_share,
            commission_usd=position.commission_usd,
        )
        for variant in ExitVariantId
    }
    return TrackResult(marks=marks, outcomes=outcomes)


def position_from_card(card: Mapping[str, Any]) -> TrackedPosition:
    """A frozen PAPER card (``paper_card_*.json`` ``card`` block) as a tracked position.

    ``commission_usd`` on the card is already the TOTAL for all structures, round
    trip (``card.py``: ``price.commission_usd * sizing.structures``). It is taken
    as is; multiplying it by the quantity again double-charged the fee. The first
    real run caught that: the 2026-09-08 QQQ card (2 structures) came out 5.20 $
    worse than ``scripts/options_alpha_score_card.py`` with identical exit values.
    """
    legs = tuple(
        TrackedLeg(
            occ_symbol=str(leg["occ_symbol"]),
            is_long=leg["side"] == "long",
            strike=Decimal(str(leg["strike"])),
            expiry=date.fromisoformat(str(leg["expiry"])),
        )
        for leg in card["legs"]
    )
    quantity = int(card["quantity"])
    return TrackedPosition(
        position_id=str(card["signal_id"]),
        underlying=str(card["underlying"]),
        entry_session=date.fromisoformat(str(card["session"])),
        entry_debit=Decimal(str(card["net_debit_per_share"])),
        quantity=quantity,
        commission_usd=Decimal(str(card["commission_usd"])),
        legs=legs,
    )
