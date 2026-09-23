"""The PAPER signal card: everything a decision needs, frozen at decision time.

A card answers, in one object, the questions the task's section 15 puts to the
screen: what happened, which structure, on what condition, how much is at risk,
until when, why you might pass, and how validated any of it is.

Three rules shape the design.

**Two independent status axes.** How much EVIDENCE a strategy has
(:class:`ResearchStatus`) and whether an opportunity is actionable right now
(:class:`OpportunityStatus`) are different questions. Collapsing them is how an
unvalidated idea ends up looking like a validated one: a strategy can be
``RESEARCH_ONLY`` and still produce a ``PAPER_ENTRY_READY`` card, as long as the
card says so in both places. That is the honest way to ship a working product
while the research is still open.

**A card is evidence, not a view.** It stores what was known at ``decision_time``
and is never rewritten when prices move. A later price makes a NEW card or an
event on the existing one; overwriting would destroy the only record of what the
decision actually saw. Cards carry an ``opportunity_id`` so that several
hypotheses firing on one economic event are visibly one opportunity rather than
several independent trades.

**Missing data is a field, not a silence.** ``missing_data`` travels with the
card. A blank on the screen must be readable as "not known", never as "clean".

Nothing here sends an order. PAPER only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from uoa_detector.options_alpha.selection import Candidate
from uoa_detector.options_alpha.settings import OptionsAlphaSettings
from uoa_detector.options_alpha.structures import Leg, StructureKind


class ResearchStatus(StrEnum):
    """How much evidence stands behind the STRATEGY that produced this card."""

    RESEARCH_ONLY = "RESEARCH_ONLY"
    EXPLORATORY_PASS = "EXPLORATORY_PASS"
    OOS_PASS = "OOS_PASS"
    FORWARD_PASS = "FORWARD_PASS"
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class OpportunityStatus(StrEnum):
    """Whether THIS card is actionable, independent of how validated it is."""

    WATCH = "WATCH"
    PAPER_ENTRY_READY = "PAPER_ENTRY_READY"
    PAPER_OPEN = "PAPER_OPEN"
    EXIT_DUE = "EXIT_DUE"
    EXPIRED = "EXPIRED"
    NO_TRADE = "NO_TRADE"


class DataOrigin(StrEnum):
    """Where the prices came from. A replay is never shown as a live entry."""

    LIVE = "live"
    REPLAY = "replay"


@dataclass(frozen=True)
class CardLeg:
    """One leg, with the identity a broker ticket would need."""

    occ_symbol: str
    right: str
    strike: Decimal
    expiry: date
    multiplier: int
    is_long: bool
    bid: Decimal | None
    ask: Decimal | None
    quote_as_of: date

    @classmethod
    def of(cls, leg: Leg) -> CardLeg:
        quote = leg.quote
        return cls(
            occ_symbol=quote.option_symbol,
            right=quote.right.value,
            strike=quote.strike,
            expiry=quote.expiry,
            multiplier=quote.multiplier,
            is_long=leg.is_long,
            bid=quote.bid,
            ask=quote.ask,
            quote_as_of=quote.as_of,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "occ_symbol": self.occ_symbol,
            "right": self.right,
            "strike": str(self.strike),
            "expiry": self.expiry.isoformat(),
            "multiplier": self.multiplier,
            "side": "long" if self.is_long else "short",
            "bid": None if self.bid is None else str(self.bid),
            "ask": None if self.ask is None else str(self.ask),
            "quote_as_of": self.quote_as_of.isoformat(),
        }


@dataclass(frozen=True)
class SignalCard:
    """One frozen decision. Append-only by intent: build a new card, never edit."""

    signal_id: str
    opportunity_id: str
    hypothesis_id: str
    research_status: ResearchStatus
    opportunity_status: OpportunityStatus
    data_origin: DataOrigin

    decision_time: datetime
    session: date
    underlying: str
    direction: str
    structure: StructureKind
    legs: tuple[CardLeg, ...]

    net_debit_per_share: Decimal
    limit_price: Decimal
    quantity: int
    entry_cost_usd: Decimal
    commission_usd: Decimal
    structural_max_loss_usd: Decimal
    max_profit_usd: Decimal | None
    breakeven_underlying: Decimal | None

    planned_stop_usd: Decimal
    target_usd: Decimal
    target_meaning: str
    max_hold_trading_days: int
    force_close_at_dte: int
    invalidation: str

    trigger: str
    eligibility_reason: str
    counter_argument: str
    missing_data: tuple[str, ...]

    quality_tier: str
    profile_sha256: str
    code_version: str
    data_manifest: dict[str, Any]
    sources: tuple[str, ...]

    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "opportunity_id": self.opportunity_id,
            "hypothesis_id": self.hypothesis_id,
            "research_status": self.research_status.value,
            "opportunity_status": self.opportunity_status.value,
            "data_origin": self.data_origin.value,
            "decision_time": self.decision_time.isoformat(),
            "session": self.session.isoformat(),
            "underlying": self.underlying,
            "direction": self.direction,
            "structure": self.structure.value,
            "legs": [leg.as_dict() for leg in self.legs],
            "net_debit_per_share": str(self.net_debit_per_share),
            "limit_price": str(self.limit_price),
            "quantity": self.quantity,
            "entry_cost_usd": str(self.entry_cost_usd),
            "commission_usd": str(self.commission_usd),
            "structural_max_loss_usd": str(self.structural_max_loss_usd),
            "max_profit_usd": None if self.max_profit_usd is None else str(self.max_profit_usd),
            "breakeven_underlying": (
                None if self.breakeven_underlying is None else str(self.breakeven_underlying)
            ),
            "planned_stop_usd": str(self.planned_stop_usd),
            "target_usd": str(self.target_usd),
            "target_meaning": self.target_meaning,
            "max_hold_trading_days": self.max_hold_trading_days,
            "force_close_at_dte": self.force_close_at_dte,
            "invalidation": self.invalidation,
            "trigger": self.trigger,
            "eligibility_reason": self.eligibility_reason,
            "counter_argument": self.counter_argument,
            "missing_data": list(self.missing_data),
            "quality_tier": self.quality_tier,
            "profile_sha256": self.profile_sha256,
            "code_version": self.code_version,
            "data_manifest": self.data_manifest,
            "sources": list(self.sources),
            "events": list(self.events),
        }


def _deterministic_id(prefix: str, parts: dict[str, Any]) -> str:
    """Content-addressed id: the same decision on the same data is the same card.

    Replays must be stable, and a random id would make every re-run look like a
    new opportunity. It also makes double-counting visible: two hypotheses that
    produce an identical structure on one session collide on ``opportunity_id``
    instead of quietly becoming two trades.
    """
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(blob).hexdigest()[:16]}"


def build_card(
    candidate: Candidate,
    *,
    hypothesis_id: str,
    research_status: ResearchStatus,
    settings: OptionsAlphaSettings,
    session: date,
    underlying: str,
    trigger: str,
    counter_argument: str,
    missing_data: tuple[str, ...] = (),
    data_origin: DataOrigin = DataOrigin.REPLAY,
    code_version: str = "unknown",
    data_manifest: dict[str, Any] | None = None,
    sources: tuple[str, ...] = (),
    decision_time: datetime | None = None,
) -> SignalCard:
    """Freeze one candidate into a card.

    ``opportunity_status`` is decided here and only here. A replay card can never
    be ``PAPER_ENTRY_READY``: those prices are an end-of-day snapshot of a session
    that has already closed, and presenting them as an entry would be exactly the
    "old card shown as buy now" the task forbids. It is ``WATCH`` instead, and the
    origin says why.
    """
    price, sizing = candidate.price, candidate.sizing
    legs = tuple(CardLeg.of(leg) for leg in candidate.legs)

    opportunity_id = _deterministic_id(
        "opp",
        {
            "session": session,
            "underlying": underlying,
            "direction": candidate.direction,
            "legs": sorted(leg.occ_symbol for leg in legs),
        },
    )
    signal_id = _deterministic_id(
        "sig", {"opportunity": opportunity_id, "hypothesis": hypothesis_id}
    )

    status = (
        OpportunityStatus.WATCH
        if data_origin is DataOrigin.REPLAY
        else OpportunityStatus.PAPER_ENTRY_READY
    )

    stop_fraction = Decimal(str(settings.exit.stop_pct_of_entry_debit)) / Decimal(100)
    target_fraction = Decimal(str(settings.exit.target_pct_of_entry_debit)) / Decimal(100)

    # A defined-risk structure has a payoff ceiling, and a target above it can
    # never be reached. The first card produced by this engine asked for +100% of
    # a 60.60 debit on a spread whose maximum profit was 39.40 — a target the
    # position could not hit even if it went perfectly. Cap it at the structure's
    # own ceiling and say so, rather than printing an unreachable number.
    raw_target = (price.max_loss_usd * target_fraction).quantize(Decimal("0.01"))
    structure_ceiling = (
        None if price.max_profit_usd is None else price.max_profit_usd * Decimal(sizing.structures)
    )
    target_capped = structure_ceiling is not None and raw_target > structure_ceiling
    target_usd = structure_ceiling if target_capped and structure_ceiling is not None else raw_target
    target_note = (
        f" — yapinin tavaniyla sinirlandi (istenen {raw_target} $ ulasilamaz)"
        if target_capped
        else ""
    )

    return SignalCard(
        signal_id=signal_id,
        opportunity_id=opportunity_id,
        hypothesis_id=hypothesis_id,
        research_status=research_status,
        opportunity_status=status,
        data_origin=data_origin,
        decision_time=decision_time or datetime.now(UTC),
        session=session,
        underlying=underlying,
        direction=candidate.direction,
        structure=candidate.kind,
        legs=legs,
        net_debit_per_share=price.entry_debit,
        # The limit IS the modelled entry debit. It is not a promise of a fill:
        # section 12 allows a hypothetical fill only when a later quote actually
        # reaches it, and that check belongs to the monitor, not to this card.
        limit_price=price.entry_debit,
        quantity=sizing.structures,
        entry_cost_usd=(price.entry_cost_usd * Decimal(sizing.structures)),
        commission_usd=(price.commission_usd * Decimal(sizing.structures)),
        structural_max_loss_usd=sizing.total_risk_usd,
        max_profit_usd=(
            None
            if price.max_profit_usd is None
            else price.max_profit_usd * Decimal(sizing.structures)
        ),
        breakeven_underlying=price.breakeven_underlying,
        planned_stop_usd=(price.max_loss_usd * stop_fraction).quantize(Decimal("0.01")),
        target_usd=target_usd,
        target_meaning=(
            "yapinin cikis degeri uzerinden, dayanak fiyati uzerinden degil "
            f"(+%{settings.exit.target_pct_of_entry_debit:.0f} / "
            f"-%{settings.exit.stop_pct_of_entry_debit:.0f}){target_note}"
        ),
        max_hold_trading_days=settings.exit.primary_hold_trading_days,
        force_close_at_dte=settings.exit.close_at_dte,
        invalidation=(
            f"{settings.exit.primary_hold_trading_days} islem gunu doldu, "
            f"vadeye {settings.exit.close_at_dte} gun kaldi, "
            "ya da yapinin cikis degeri stop seviyesine dustu"
        ),
        trigger=trigger,
        eligibility_reason=(
            candidate.chose_spread_because
            or "bare long secildi: spread debit'i esik kadar dusurmedi"
        ),
        counter_argument=counter_argument,
        missing_data=missing_data,
        quality_tier=settings.quality.tier,
        profile_sha256=settings.profile_sha256,
        code_version=code_version,
        data_manifest=data_manifest or {},
        sources=sources,
    )
