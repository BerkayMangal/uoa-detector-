"""Turn a session's option chain into priced, sized candidate structures.

The pipeline this module owns is the middle of the task's section 8 chain:

    chain rows -> eligibility gates -> structure construction -> cost -> risk

Two properties matter more than anything clever here.

**Every rejection is counted.** ``Funnel`` records how many contracts died at each
gate and why. A screen that only ever says "no signal today" is useless — the task
requires showing how many records survived scanned -> candidate -> priced -> cost
gate -> risk gate, and which gate is doing the killing. A funnel that is built as
a side effect of filtering cannot drift away from the filtering itself.

**Nothing is invented to fill the screen.** A contract with a missing quote, a
stale session, a non-standard multiplier or a delta outside the frozen band is
dropped and counted, never substituted, widened or rounded into eligibility.

Pure: no I/O beyond reading a snapshot the caller hands it, no clock, no network.
The session date is always passed in, so a replay of 2026-09-22 prices exactly
what was knowable on 2026-09-22.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from uoa_detector.options_alpha.settings import OptionsAlphaSettings
from uoa_detector.options_alpha.structures import (
    ContractQuote,
    Leg,
    Right,
    Sizing,
    StructureKind,
    StructurePrice,
    UnpriceableError,
    price_structure,
    size_position,
)


def _decimal(value: object) -> Decimal | None:
    """UW quotes numbers as strings. A value that will not parse is absent, not zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    parsed = _float(value)
    return None if parsed is None else int(parsed)


def parse_chain_row(row: dict[str, Any], session: date, underlying: str) -> ContractQuote | None:
    """One UW ``option-chains`` row as a quote, or ``None`` if it is unusable.

    ``last_tape_time`` is deliberately not read as a quote timestamp: it is the
    last TRADE time, which says nothing about when the NBBO was valid. The
    session the chain was requested for is the only time this quote can honestly
    claim, and that is what ``as_of`` carries.
    """
    symbol = row.get("option_symbol")
    raw_type = row.get("option_type")
    strike = _decimal(row.get("strike"))
    expires = row.get("expires")
    if not isinstance(symbol, str) or not isinstance(expires, str) or strike is None:
        return None
    if raw_type not in ("call", "put"):
        return None
    try:
        expiry = date.fromisoformat(expires)
    except ValueError:
        return None

    return ContractQuote(
        option_symbol=symbol,
        underlying=underlying,
        right=Right(raw_type),
        strike=strike,
        expiry=expiry,
        bid=_decimal(row.get("nbbo_bid")),
        ask=_decimal(row.get("nbbo_ask")),
        as_of=session,
        open_interest=_int(row.get("open_interest")),
        volume=_int(row.get("volume")),
        delta=_float(row.get("delta")),
        implied_volatility=_float(row.get("implied_volatility")),
    )


def load_chain_snapshot(path: pathlib.Path | str) -> tuple[list[ContractQuote], date, str]:
    """Read a frozen replay snapshot written by the capture step."""
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    session = date.fromisoformat(str(payload["date"]))
    underlying = str(payload["ticker"])
    quotes = [
        quote
        for quote in (parse_chain_row(row, session, underlying) for row in payload["rows"])
        if quote is not None
    ]
    return quotes, session, underlying


@dataclass
class Funnel:
    """How many contracts survived each gate, and what killed the rest.

    Built while filtering rather than recomputed afterwards, so the number on the
    screen is the number the filter actually produced.
    """

    scanned: int = 0
    eligible: int = 0
    structures_built: int = 0
    priced: int = 0
    passed_cost_gate: int = 0
    passed_risk_gate: int = 0
    dropped: Counter[str] = field(default_factory=Counter)

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "eligible": self.eligible,
            "structures_built": self.structures_built,
            "priced": self.priced,
            "passed_cost_gate": self.passed_cost_gate,
            "passed_risk_gate": self.passed_risk_gate,
            "top_drop_reasons": self.dropped.most_common(8),
        }


def eligibility_failure(
    quote: ContractQuote, session: date, settings: OptionsAlphaSettings
) -> str | None:
    """The first frozen gate this contract fails, or ``None`` if it passes all."""
    gate = settings.contract

    if quote.as_of != session:
        return "farkli seans"
    if gate.require_standard_multiplier and quote.multiplier != gate.standard_multiplier:
        # Adjusted deliverables and non-100 multipliers are excluded rather than
        # priced with a blanket 100, which would misstate every dollar figure.
        return "standart disi carpan"

    dte = quote.dte(session)
    if dte < gate.min_dte:
        return "DTE cok kisa"
    if dte > gate.max_dte:
        return "DTE cok uzun"

    if quote.bid is None or quote.ask is None:
        return "kotasyon eksik"
    if quote.is_crossed:
        return "capraz kotasyon"
    if quote.bid < Decimal(str(gate.min_nbbo_bid)):
        return "bid cok dusuk"

    spread = quote.spread_pct_of_mid
    if spread is None:
        return "makas hesaplanamadi"
    if spread > gate.max_spread_pct_of_mid:
        return "makas cok genis"

    if quote.open_interest is None or quote.open_interest < gate.min_open_interest:
        return "acik pozisyon yetersiz"
    if quote.volume is None or quote.volume < gate.min_volume:
        return "hacim yetersiz"

    if quote.delta is None:
        # A missing delta is missing information, not a neutral one. Guessing it
        # from moneyness here would put a contract through a gate it never met.
        return "delta yok"
    if not (gate.min_abs_delta <= abs(quote.delta) <= gate.max_abs_delta):
        return "delta bandi disinda"

    return None


def eligible_contracts(
    quotes: list[ContractQuote], session: date, settings: OptionsAlphaSettings
) -> tuple[list[ContractQuote], Funnel]:
    funnel = Funnel(scanned=len(quotes))
    kept: list[ContractQuote] = []
    for quote in quotes:
        failure = eligibility_failure(quote, session, settings)
        if failure is None:
            kept.append(quote)
        else:
            funnel.drop(failure)
    funnel.eligible = len(kept)
    return kept, funnel


@dataclass(frozen=True)
class Candidate:
    """One priced, sized structure, with the reasoning that produced it."""

    kind: StructureKind
    legs: tuple[Leg, ...]
    price: StructurePrice
    sizing: Sizing
    direction: str
    chose_spread_because: str | None


def _same_expiry_ladder(
    chain: list[ContractQuote], anchor: ContractQuote, right: Right
) -> list[ContractQuote]:
    """Every listed strike of the anchor's expiry and right, in strike order.

    Built from the FULL chain, not from the contracts that passed eligibility.
    The short leg of a debit spread is a hedge that caps cost and payoff — it is
    not a directional expression — so requiring it to clear the delta band and
    volume floors meant for the long leg is wrong, and it silently widens the
    spread: on SPY the eligible ladder skipped 776 and produced a 2-wide spread
    where `width_strikes: 1` asked for the next listed strike.

    The short leg is still not unchecked. ``price_structure`` refuses a missing,
    zero or crossed quote, and enforces the same underlying, expiry, right,
    multiplier and session for both legs.
    """
    return sorted(
        (q for q in chain if q.expiry == anchor.expiry and q.right is right),
        key=lambda q: q.strike,
    )


def build_candidate(
    anchor: ContractQuote,
    chain: list[ContractQuote],
    settings: OptionsAlphaSettings,
    funnel: Funnel,
) -> Candidate | None:
    # ``chain`` must be the FULL session chain, not the eligible subset: the
    # short leg is picked from every listed strike. See _same_expiry_ladder.
    """Price the bare long and its debit spread, and apply the frozen choice rule.

    The spread is preferred only when it cuts the debit by at least the frozen
    percentage. Otherwise the bare long keeps its unbounded upside, which is the
    whole reason to pay for convexity. Choosing by which one *performed* better
    would be selection on the outcome.
    """
    bullish = anchor.right is Right.CALL
    direction = "up" if bullish else "down"

    long_legs = (Leg(anchor, is_long=True),)
    try:
        long_kind, long_price = price_structure(long_legs, settings)
    except UnpriceableError as error:
        funnel.drop(f"uzun bacak fiyatlanamadi: {error}")
        return None
    funnel.priced += 1

    spread_settings = settings.structures.debit_spread
    ladder = _same_expiry_ladder(chain, anchor, anchor.right)
    index = next((i for i, q in enumerate(ladder) if q.option_symbol == anchor.option_symbol), None)

    spread: tuple[StructureKind, StructurePrice, tuple[Leg, ...]] | None = None
    if index is not None:
        offset = spread_settings.width_strikes
        short_index = index + offset if bullish else index - offset
        if 0 <= short_index < len(ladder):
            legs = (Leg(anchor, is_long=True), Leg(ladder[short_index], is_long=False))
            try:
                kind, priced = price_structure(legs, settings)
            except UnpriceableError as error:
                funnel.drop(f"spread fiyatlanamadi: {error}")
            else:
                spread = (kind, priced, legs)

    chosen_kind: StructureKind = long_kind
    chosen_price: StructurePrice = long_price
    chosen_legs: tuple[Leg, ...] = long_legs
    reason: str | None = None
    if spread is not None:
        # Distinct names: rebinding `kind`/`priced`/`legs` from the block above
        # narrows them to that block's concrete tuple shape and the types stop
        # agreeing.
        spread_kind, spread_price, spread_legs = spread
        funnel.priced += 1
        cut_pct = float(
            (long_price.entry_debit - spread_price.entry_debit)
            / long_price.entry_debit
            * Decimal(100)
        )
        if cut_pct >= spread_settings.min_debit_reduction_pct:
            chosen_kind, chosen_price, chosen_legs = spread_kind, spread_price, spread_legs
            reason = f"debit %{cut_pct:.1f} dustu (esik %{spread_settings.min_debit_reduction_pct})"

    funnel.structures_built += 1
    funnel.passed_cost_gate += 1

    sizing = size_position(chosen_price, settings)
    if sizing.structures < 1:
        # The minimum budget travels with the rejection. Section 13 allows showing
        # what WOULD be needed, and forbids quietly reaching for a cheaper, riskier
        # contract to make a position fit.
        funnel.drop(
            f"risk butcesine sigmadi (tek yapi {sizing.risk_per_structure_usd} $, "
            f"butce {sizing.budget_usd} $)"
        )
        return None
    funnel.passed_risk_gate += 1

    return Candidate(
        kind=chosen_kind,
        legs=chosen_legs,
        price=chosen_price,
        sizing=sizing,
        direction=direction,
        chose_spread_because=reason,
    )
