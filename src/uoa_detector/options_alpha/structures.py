"""Option structures, and what they actually cost to open and close.

This is the deterministic heart of phase 5.24: pure arithmetic over quoted
prices, with no I/O, no clock and no hidden defaults. Given the same quotes and
the same settings it returns the same numbers, which is what lets the research
path and the live path share one implementation instead of drifting into two.

The fill model is the conservative one the task fixes in section 12:

    a long leg  opens at the ASK and closes at the BID
    a short leg opens at the BID and closes at the ASK

so the bid/ask spread is already paid inside those prices. Charging a separate
"spread cost" on top would bill the same money twice — a mistake that quietly
flatters or punishes every result built on it.

Two things this module refuses to do, both of which would manufacture a better
number than the data supports:

* It never invents a price. A missing, zero, crossed or stale quote makes the
  structure unpriceable and it says so; it does not fall back to the mid, to the
  last trade, or to a model value.
* It never rounds a position up. If one structure does not fit the risk budget
  the answer is zero contracts, and the budget that WOULD be needed is reported
  as information rather than silently met by reaching for a cheaper, further
  out-of-the-money strike.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from uoa_detector.options_alpha.settings import OptionsAlphaSettings

# Money is Decimal end to end. Option premiums are quoted in cents and a float
# round-trip turns 0.65 into 0.6500000000000001, which then shows up in a P&L
# report as noise that looks like a result.
CENTS = Decimal("0.01")


class Right(StrEnum):
    CALL = "call"
    PUT = "put"


class StructureKind(StrEnum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_DEBIT = "bull_call_debit"
    BEAR_PUT_DEBIT = "bear_put_debit"


class UnpriceableError(Exception):
    """A structure that cannot be honestly priced from the quotes given."""


@dataclass(frozen=True)
class ContractQuote:
    """One contract's end-of-day NBBO snapshot, as the chain reports it.

    ``as_of`` is the session the chain was requested for. The chain's own time
    field is ``last_tape_time``, the last TRADE time, so there is no instant at
    which this quote is known to have been valid — which is exactly why this
    scope is tier B and why nothing here claims an intraday fill.
    """

    option_symbol: str
    underlying: str
    right: Right
    strike: Decimal
    expiry: date
    bid: Decimal | None
    ask: Decimal | None
    as_of: date
    multiplier: int = 100
    open_interest: int | None = None
    volume: int | None = None
    delta: float | None = None
    implied_volatility: float | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return ((self.bid + self.ask) / Decimal(2)).quantize(CENTS)

    @property
    def spread_pct_of_mid(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return float((self.ask - self.bid) / mid * Decimal(100))

    @property
    def is_crossed(self) -> bool:
        """Ask below bid. Real in end-of-day snapshots, and never tradeable."""
        return self.bid is not None and self.ask is not None and self.ask < self.bid

    def dte(self, on: date) -> int:
        return (self.expiry - on).days


@dataclass(frozen=True)
class Leg:
    """One leg of a structure. ``long`` is the direction WE take, not the quote."""

    quote: ContractQuote
    is_long: bool
    ratio: int = 1


@dataclass(frozen=True)
class StructurePrice:
    """What one structure costs to open and what closing it would return.

    All values are per ONE structure and already include commission, so
    ``max_loss_usd`` is the number that goes into position sizing. ``debit`` and
    ``credit`` are the raw per-share package prices before fees, kept separately
    so a report can show the fee share of the cost rather than burying it.
    """

    entry_debit: Decimal
    exit_credit: Decimal
    entry_cost_usd: Decimal
    commission_usd: Decimal
    max_loss_usd: Decimal
    max_profit_usd: Decimal | None
    breakeven_underlying: Decimal | None


def _require(value: Decimal | None, what: str) -> Decimal:
    if value is None:
        raise UnpriceableError(f"{what} yok")
    return value


def _side(quote: ContractQuote, side: str, what: str) -> Decimal:
    price = quote.ask if side == "ask" else quote.bid
    got = _require(price, f"{quote.option_symbol} {side}")
    if got <= 0:
        # A zero or negative quote is a data condition, not a free option. Clamping
        # it to something convenient is how a broken row becomes a fake winner.
        raise UnpriceableError(f"{quote.option_symbol} {side} {got} — {what} fiyatlanamaz")
    return got


def structure_kind(legs: tuple[Leg, ...]) -> StructureKind:
    """Name the structure, refusing anything outside the v1 scope."""
    if len(legs) == 1:
        leg = legs[0]
        if not leg.is_long:
            raise UnpriceableError("tek bacakli kisa pozisyon v1 kapsaminda degil")
        return StructureKind.LONG_CALL if leg.quote.right is Right.CALL else StructureKind.LONG_PUT
    if len(legs) != 2:
        raise UnpriceableError(f"{len(legs)} bacakli yapi v1 kapsaminda degil")

    longs = [x for x in legs if x.is_long]
    shorts = [x for x in legs if not x.is_long]
    if len(longs) != 1 or len(shorts) != 1:
        raise UnpriceableError("debit spread tam olarak bir uzun ve bir kisa bacak ister")
    long_leg, short_leg = longs[0], shorts[0]

    if long_leg.quote.right is not short_leg.quote.right:
        raise UnpriceableError("bacaklarin hakki ayni olmali")
    if long_leg.quote.underlying != short_leg.quote.underlying:
        raise UnpriceableError("bacaklarin dayanagi ayni olmali")
    if long_leg.quote.expiry != short_leg.quote.expiry:
        raise UnpriceableError("bacaklarin vadesi ayni olmali")
    if long_leg.quote.multiplier != short_leg.quote.multiplier:
        raise UnpriceableError("bacaklarin carpani ayni olmali")
    if long_leg.quote.as_of != short_leg.quote.as_of:
        # Two legs priced from different sessions is the cheapest way to invent a
        # package that never existed. The quality gate closes here, not later.
        raise UnpriceableError("bacaklar farkli seanslardan — paket fiyati gecersiz")

    if long_leg.quote.right is Right.CALL:
        if short_leg.quote.strike <= long_leg.quote.strike:
            raise UnpriceableError("bull call debit: kisa bacak daha yuksek strike olmali")
        return StructureKind.BULL_CALL_DEBIT
    if short_leg.quote.strike >= long_leg.quote.strike:
        raise UnpriceableError("bear put debit: kisa bacak daha dusuk strike olmali")
    return StructureKind.BEAR_PUT_DEBIT


def price_structure(
    legs: tuple[Leg, ...], settings: OptionsAlphaSettings
) -> tuple[StructureKind, StructurePrice]:
    """Open cost, close value and the risk numbers for one structure.

    Raises :class:`UnpriceableError` rather than returning a guess whenever the quotes
    cannot support an honest price.
    """
    kind = structure_kind(legs)
    costs = settings.costs
    multiplier = Decimal(legs[0].quote.multiplier)

    for leg in legs:
        if leg.quote.is_crossed:
            raise UnpriceableError(f"{leg.quote.option_symbol} capraz kotasyon")

    # Package price per share: what we pay to open, what we would receive to close.
    entry_debit = Decimal(0)
    exit_credit = Decimal(0)
    for leg in legs:
        ratio = Decimal(leg.ratio)
        if leg.is_long:
            entry_debit += _side(leg.quote, costs.entry_long_side, "giris") * ratio
            exit_credit += _side(leg.quote, costs.exit_long_side, "cikis") * ratio
        else:
            entry_debit -= _side(leg.quote, costs.entry_short_side, "giris") * ratio
            exit_credit -= _side(leg.quote, costs.exit_short_side, "cikis") * ratio

    if entry_debit <= 0:
        # A non-positive debit means the quotes imply a credit package. v1 is
        # debit-only; a credit structure has different risk and is out of scope.
        raise UnpriceableError(f"net debit {entry_debit} — v1 yalnizca debit yapilar")

    # Latency haircut, charged against us on both sides and applied to the price
    # actually transacted, not to the package mid.
    #
    # The mid version was tried first and is perverse: a wider market has a lower
    # mid, so a worse quote drew a SMALLER haircut and looked cheaper to open.
    # Widening a market must never reduce a cost. Scaling each side by its own
    # transacted price keeps it monotonic — and the bid/ask spread itself is
    # already paid inside those prices, so this is latency only, never a second
    # charge for the spread.
    slip_rate = Decimal(str(costs.extra_slippage_pct)) / Decimal(100)
    entry_debit = (entry_debit * (Decimal(1) + slip_rate)).quantize(CENTS)
    exit_credit = max(Decimal(0), (exit_credit * (Decimal(1) - slip_rate)).quantize(CENTS))

    # Commission is per contract, per leg, per direction: open and close both pay.
    fee_per_leg = Decimal(str(costs.commission_per_contract_per_leg_usd))
    commission = (fee_per_leg * Decimal(sum(x.ratio for x in legs)) * Decimal(2)).quantize(CENTS)

    entry_cost = (entry_debit * multiplier).quantize(CENTS)
    max_loss = (entry_cost + commission).quantize(CENTS)

    max_profit: Decimal | None = None
    breakeven: Decimal | None = None
    long_leg = next(x for x in legs if x.is_long)

    if kind in (StructureKind.BULL_CALL_DEBIT, StructureKind.BEAR_PUT_DEBIT):
        short_leg = next(x for x in legs if not x.is_long)
        width = abs(short_leg.quote.strike - long_leg.quote.strike)
        # The spread's payoff ceiling is the strike width; fees come out of it.
        max_profit = ((width * multiplier) - entry_cost - commission).quantize(CENTS)
    if kind in (StructureKind.LONG_CALL, StructureKind.BULL_CALL_DEBIT):
        breakeven = (long_leg.quote.strike + entry_debit).quantize(CENTS)
    else:
        breakeven = (long_leg.quote.strike - entry_debit).quantize(CENTS)

    return kind, StructurePrice(
        entry_debit=entry_debit,
        exit_credit=exit_credit,
        entry_cost_usd=entry_cost,
        commission_usd=commission,
        max_loss_usd=max_loss,
        max_profit_usd=max_profit,
        breakeven_underlying=breakeven,
    )


@dataclass(frozen=True)
class Sizing:
    """How many structures the risk budget allows, and why it might be none."""

    structures: int
    risk_per_structure_usd: Decimal
    total_risk_usd: Decimal
    budget_usd: Decimal
    minimum_budget_needed_usd: Decimal | None
    reason: str


def size_position(price: StructurePrice, settings: OptionsAlphaSettings) -> Sizing:
    """Whole structures only, floored, never rounded up to meet the budget.

    For both a long option and a debit spread the money genuinely at risk is the
    full cost of the package, so that — not a planned stop distance — is what the
    budget is divided by. Presenting a stop loss as the structural maximum is how
    a position ends up several times larger than the risk rule intended.
    """
    budget = Decimal(str(settings.risk.r_usd))
    risk_each = price.max_loss_usd

    if risk_each <= 0:
        raise UnpriceableError("yapisal risk sifir veya negatif — boyutlandirilamaz")

    count = int(budget // risk_each)
    if count < 1:
        return Sizing(
            structures=0,
            risk_per_structure_usd=risk_each,
            total_risk_usd=Decimal(0),
            budget_usd=budget,
            minimum_budget_needed_usd=risk_each,
            reason=(
                f"tek yapi {risk_each} $ riske ediyor, butce {budget} $ — adet sifir"
            ),
        )

    count = min(count, settings.risk.max_structures_per_signal)
    return Sizing(
        structures=count,
        risk_per_structure_usd=risk_each,
        total_risk_usd=(risk_each * Decimal(count)).quantize(CENTS),
        budget_usd=budget,
        minimum_budget_needed_usd=None,
        reason=f"{count} yapi x {risk_each} $",
    )
