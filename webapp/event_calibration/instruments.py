"""Observed packet comparisons, quality B; no theoretical option P&L."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from webapp.event_calibration.model import Dataset, Decision, Structure, settings
from webapp.event_calibration.replay import Unit


@dataclass(frozen=True)
class Comparison:
    decision: str
    kind: str
    status: str
    units: int = 0
    debit: float | None = None
    planned_risk: float | None = None
    net_usd: float | None = None
    risk_r: float | None = None
    entry_packet: str | None = None
    exit_packet: str | None = None
    quality: str = "B / receipt-dated quote references, not exchange-timed fills"
    entry_observed_at: datetime | None = None
    exit_observed_at: datetime | None = None


def _valid(d: Decision, structure: Structure) -> bool:
    return all(
        leg.expiry >= d.at.date()
        and 0 <= (d.at - leg.fetched_at).total_seconds() <= settings().quote_max_age_seconds
        for leg in structure.legs
    )


def _value(structure: Structure, entry: bool) -> float:
    return structure.multiplier * sum(
        (leg.ask if entry else leg.bid) if leg.is_long else -(leg.bid if entry else leg.ask)
        for leg in structure.legs
    )


def compare(data: Dataset, unit: Unit) -> tuple[Comparison, ...]:
    c = settings()
    original = next(d for d in data.decisions if d.key == unit.key)
    result = []
    if original.risk_fraction is not None:
        risk = unit.entry * original.risk_fraction
        quantity = min(
            math.floor(c.risk_budget_usd / risk),
            math.floor(c.outlay_cap_usd / (unit.entry * (1 + c.slippage_bps[0] / 10000))),
        )
        if quantity > 0:
            # Stock slip has already been measured in Unit.net_pct; apply once.
            debit = quantity * unit.entry * (1 + c.slippage_bps[0] / 10000)
            net = debit * unit.net_pct / 100
            result.append(
                Comparison(
                    unit.key,
                    "stock",
                    "observed_stock_reference",
                    quantity,
                    debit,
                    quantity * risk,
                    net,
                    net / (quantity * risk),
                    quality="sampled stock references; stop risk is not a guaranteed loss cap",
                )
            )
        else:
            result.append(Comparison(unit.key, "stock", "unaffordable"))
    else:
        result.append(Comparison(unit.key, "stock", "missing_recorded_stop_risk"))
    tolerance = timedelta(minutes=c.mark_tolerance_minutes)
    for kind in ("long_call", "bull_call_debit"):
        proposed = next((o for o in original.options if o.kind == kind), None)
        if proposed is None:
            result.append(Comparison(unit.key, kind, "missing_recorded_structure"))
            continue
        proposed_symbols = tuple(leg.symbol for leg in proposed.legs)
        chosen: tuple[Decision, Structure] | None = None
        for packet in sorted(data.decisions, key=lambda d: (d.at, d.key)):
            if (
                packet.ticker != unit.ticker
                or not unit.entry_at <= packet.at <= unit.entry_at + tolerance
            ):
                continue
            for structure in packet.options:
                if (
                    structure.kind == kind
                    and tuple(leg.symbol for leg in structure.legs) == proposed_symbols
                    and _valid(packet, structure)
                    and all(leg.fetched_at >= unit.entry_at for leg in structure.legs)
                ):
                    chosen = packet, structure
                    break
            if chosen:
                break
        if chosen is None:
            result.append(Comparison(unit.key, kind, "missing_fresh_entry_packet"))
            continue
        packet, structure = chosen
        # Never select a later structure after inspecting profit/affordability.
        fee = len(structure.legs) * c.commission_per_contract_side
        debit = _value(structure, True) + fee
        width = (
            (structure.legs[-1].strike - structure.legs[0].strike) * structure.multiplier
            if kind == "bull_call_debit"
            else None
        )
        if debit <= 0 or (width is not None and debit >= width):
            result.append(Comparison(unit.key, kind, "invalid_debit"))
            continue
        units = min(math.floor(c.risk_budget_usd / debit), math.floor(c.outlay_cap_usd / debit))
        if units <= 0:
            result.append(Comparison(unit.key, kind, "unaffordable"))
            continue
        symbols = tuple(leg.symbol for leg in structure.legs)
        exit_choice = None
        for later in sorted(data.decisions, key=lambda d: (d.at, d.key)):
            if (
                later.ticker != unit.ticker
                or not unit.exit_at <= later.at <= unit.exit_at + tolerance
            ):
                continue
            for other in later.options:
                if (
                    other.kind == kind
                    and other.multiplier == structure.multiplier
                    and tuple(leg.symbol for leg in other.legs) == symbols
                    and tuple(leg.is_long for leg in other.legs)
                    == tuple(leg.is_long for leg in structure.legs)
                    and _valid(later, other)
                    and all(leg.fetched_at >= unit.exit_at for leg in other.legs)
                ):
                    exit_choice = later, other
                    break
            if exit_choice:
                break
        if exit_choice is None:
            result.append(
                Comparison(
                    unit.key,
                    kind,
                    "missing_fixed_contract_exit",
                    units,
                    units * debit,
                    units * debit,
                    entry_packet=packet.key,
                )
            )
            continue
        later, other = exit_choice
        credit = _value(other, False) - fee
        net = units * (credit - debit)
        result.append(
            Comparison(
                unit.key,
                kind,
                "observed_quote_reference",
                units,
                units * debit,
                units * debit,
                net,
                net / (units * debit),
                packet.key,
                later.key,
                entry_observed_at=packet.at,
                exit_observed_at=later.at,
            )
        )
    return tuple(result)
