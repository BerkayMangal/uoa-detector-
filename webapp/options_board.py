"""Read the committed options_alpha_v1 artifacts for the ``/opsiyon`` screen.

Phase 5.24 M7. The options scope writes its PAPER cards, their scored outcomes and
its research verdicts to ``artifacts/options-alpha-v1/`` as JSON. Until now nothing
in the webapp could read any of it, so the main product -- the option signal
terminal -- had no screen at all and the only way to see a card was to open a file.

This module is the reader. It is pure: it takes a repository root, touches the
filesystem, and returns frozen dataclasses. No network, no database, no clock.

Three things it deliberately does NOT do.

It does not decide anything. A card's status is whatever the card says; this module
never promotes a card, never re-ranks one and never hides one.

It never returns a clean-looking empty board. If the scope directory cannot be read
the board says so, the same rule the Alfa Board follows: a blank screen that looks
calm is the failure mode that costs money, because it reads as "nothing today"
when it actually means "I could not look".

It surfaces contradictions instead of rendering them. A replay card marked anything
other than WATCH, or a target above the structure's own ceiling, means an artifact
disagrees with the engine that was supposed to have produced it. Those are shown as
contradictions on the card rather than quietly displayed as fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCOPE_DIR = Path("artifacts/options-alpha-v1")

# The variant frozen as primary in every pre-registration of this scope. The screen
# reports it as the headline and keeps the others visible underneath, so a reader
# cannot mistake a better-looking secondary for the result.
PRIMARY_VARIANT = "time_only"


# --- adapter boundary -------------------------------------------------------
# Vendor-shaped JSON arrives as Any. It is narrowed here and nowhere else, so no
# untyped value escapes into the rest of the module (D12).


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _whole(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _money(value: Any) -> Decimal | None:
    if isinstance(value, str | int):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Leg:
    occ_symbol: str
    right: str
    strike: str
    expiry: str
    side: str
    bid: str
    ask: str
    quote_as_of: str


@dataclass(frozen=True)
class Outcome:
    variant: str
    reason: str
    exit_day: str
    pnl_usd: str
    held_trading_days: int
    unpriced_days: int


@dataclass(frozen=True)
class CardRow:
    source_path: str
    session: str
    underlying: str
    direction: str
    structure: str
    research_status: str
    opportunity_status: str
    data_origin: str
    quality_tier: str
    quantity: int
    entry_cost_usd: str
    max_loss_usd: str
    max_profit_usd: str
    target_usd: str
    planned_stop_usd: str
    invalidation: str
    trigger: str
    counter_argument: str
    missing_data: tuple[str, ...]
    legs: tuple[Leg, ...]
    primary_outcome: Outcome | None
    other_outcomes: tuple[Outcome, ...]
    contradictions: tuple[str, ...]

    @property
    def is_scored(self) -> bool:
        return self.primary_outcome is not None


@dataclass(frozen=True)
class ResearchRow:
    milestone: str
    name: str
    verdict: str
    headline: str
    failed_clauses: tuple[str, ...]
    # Set when the verdict was rerun with a corrected engine (``correction_v2`` in
    # the manifest). ``verdict`` then IS the corrected verdict and ``v1_verdict``
    # is what the screen said before, shown next to it rather than silently replaced.
    v1_verdict: str = ""
    correction_doc: str = ""

    @property
    def corrected(self) -> bool:
        return bool(self.v1_verdict)


@dataclass(frozen=True)
class OptionsBoard:
    cards: tuple[CardRow, ...]
    research: tuple[ResearchRow, ...]
    problems: tuple[str, ...]
    scope_readable: bool
    closed_pnl_usd: Decimal | None
    closed_count: int

    @property
    def state(self) -> str:
        """What the screen must say about itself, never a silent blank.

        ``load-failed`` means the artifacts could not be read, which is a different
        statement from ``no-cards`` and must never be rendered as the latter.
        """
        if not self.scope_readable:
            return "load-failed"
        if not self.cards:
            return "no-cards"
        return "ok"


def _contradictions(card: dict[str, Any]) -> tuple[str, ...]:
    """Ways the artifact disagrees with the engine that claims to have written it."""
    found: list[str] = []

    origin = _text(card.get("data_origin"))
    opportunity = _text(card.get("opportunity_status"))
    if origin == "replay" and opportunity != "WATCH":
        found.append(
            f"replay kartı '{opportunity}' olarak işaretlenmiş; "
            "geçmiş veriden üretilen kart yalnız WATCH olabilir"
        )

    research = _text(card.get("research_status"))
    if research != "RESEARCH_ONLY":
        found.append(
            f"kart '{research}' taşıyor; bu kapsamda üretilen her kart RESEARCH_ONLY'dir"
        )

    # The card engine caps the target at the structure's own ceiling. A target above
    # it means an unreachable number is being shown as a plan.
    target = _money(card.get("target_usd"))
    ceiling = _money(card.get("max_profit_usd"))
    quantity = _whole(card.get("quantity"), 1)
    if target is not None and ceiling is not None and quantity > 0:
        total_ceiling = ceiling * Decimal(quantity)
        if target > total_ceiling:
            found.append(
                f"hedef {target} $ yapının tavanı {total_ceiling} $'ın üstünde — ulaşılamaz"
            )

    return tuple(found)


def _legs(card: dict[str, Any]) -> tuple[Leg, ...]:
    raw = card.get("legs")
    if not isinstance(raw, list):
        return ()
    legs: list[Leg] = []
    for item in raw:
        leg = _mapping(item)
        if not leg:
            continue
        legs.append(
            Leg(
                occ_symbol=_text(leg.get("occ_symbol")),
                right=_text(leg.get("right")),
                strike=_text(leg.get("strike")),
                expiry=_text(leg.get("expiry")),
                side=_text(leg.get("side")),
                bid=_text(leg.get("bid")),
                ask=_text(leg.get("ask")),
                quote_as_of=_text(leg.get("quote_as_of")),
            )
        )
    return tuple(legs)


def _outcomes(payload: dict[str, Any]) -> tuple[Outcome | None, tuple[Outcome, ...]]:
    exits = _mapping(payload.get("exits"))
    primary: Outcome | None = None
    others: list[Outcome] = []
    for variant, raw in exits.items():
        body = _mapping(raw)
        if not body:
            continue
        outcome = Outcome(
            variant=variant,
            reason=_text(body.get("reason")),
            exit_day=_text(body.get("exit_day")),
            pnl_usd=_text(body.get("pnl_usd")),
            held_trading_days=_whole(body.get("held_trading_days")),
            unpriced_days=_whole(body.get("unpriced_days")),
        )
        if variant == PRIMARY_VARIANT:
            primary = outcome
        else:
            others.append(outcome)
    return primary, tuple(sorted(others, key=lambda o: o.variant))


def _read_json(path: Path, problems: list[str]) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        problems.append(f"{path.name} okunamadı: {error}")
        return None
    if not isinstance(payload, dict):
        problems.append(f"{path.name} beklenen nesne değil")
        return None
    return payload


def _research_rows(root: Path, problems: list[str]) -> tuple[ResearchRow, ...]:
    manifest_path = root / SCOPE_DIR / "run_manifest.json"
    if not manifest_path.exists():
        problems.append("run_manifest.json yok; araştırma hükümleri gösterilemiyor")
        return ()
    payload = _read_json(manifest_path, problems)
    if payload is None:
        return ()
    milestones = payload.get("milestones")
    if not isinstance(milestones, list):
        problems.append("run_manifest.json içinde milestones listesi yok")
        return ()

    rows: list[ResearchRow] = []
    for item in milestones:
        milestone = _mapping(item)
        verdict = _text(milestone.get("verdict"))
        if not verdict:
            continue  # not a research family; M0/M1/M3/M8 are engineering milestones
        correction = _mapping(milestone.get("correction_v2"))
        corrected_verdict = _text(correction.get("verdict"))
        if corrected_verdict:
            rows.append(
                ResearchRow(
                    milestone=_text(milestone.get("id")),
                    name=_text(milestone.get("name")),
                    verdict=corrected_verdict,
                    headline=_text(correction.get("why")),
                    failed_clauses=_strings(correction.get("verdict_failed_clauses")),
                    v1_verdict=verdict,
                    correction_doc=_text(correction.get("doc")),
                )
            )
            continue
        rows.append(
            ResearchRow(
                milestone=_text(milestone.get("id")),
                name=_text(milestone.get("name")),
                verdict=verdict,
                headline=_text(milestone.get("headline")),
                failed_clauses=_strings(milestone.get("verdict_failed_clauses")),
            )
        )
    return tuple(rows)


def unreadable(reason: str) -> OptionsBoard:
    """A board that could not be built at all.

    Exists so an unexpected exception in the route degrades into a screen that
    SAYS it could not read, rather than one that renders zero cards and reads as
    a quiet market.
    """
    return OptionsBoard(
        cards=(),
        research=(),
        problems=(reason,),
        scope_readable=False,
        closed_pnl_usd=None,
        closed_count=0,
    )


def load_board(root: Path) -> OptionsBoard:
    """Read every committed PAPER card, its outcome if scored, and the verdicts."""
    problems: list[str] = []
    replay_dir = root / SCOPE_DIR / "replay"

    research = _research_rows(root, problems)

    if not replay_dir.is_dir():
        problems.append(f"{replay_dir} yok; kart okunamadı")
        return OptionsBoard(
            cards=(),
            research=research,
            problems=tuple(problems),
            scope_readable=False,
            closed_pnl_usd=None,
            closed_count=0,
        )

    cards: list[CardRow] = []
    for card_path in sorted(replay_dir.glob("*/paper_card_*.json")):
        if card_path.name.endswith("_outcome.json"):
            continue
        payload = _read_json(card_path, problems)
        if payload is None:
            continue
        card = _mapping(payload.get("card"))
        if not card:
            problems.append(f"{card_path.name} içinde card nesnesi yok")
            continue

        outcome_path = card_path.with_name(f"{card_path.stem}_outcome.json")
        primary: Outcome | None = None
        others: tuple[Outcome, ...] = ()
        if outcome_path.exists():
            scored = _read_json(outcome_path, problems)
            if scored is not None:
                primary, others = _outcomes(scored)

        cards.append(
            CardRow(
                source_path=str(card_path.relative_to(root)),
                session=_text(card.get("session")),
                underlying=_text(card.get("underlying")),
                direction=_text(card.get("direction")),
                structure=_text(card.get("structure")),
                research_status=_text(card.get("research_status")),
                opportunity_status=_text(card.get("opportunity_status")),
                data_origin=_text(card.get("data_origin")),
                quality_tier=_text(card.get("quality_tier")),
                quantity=_whole(card.get("quantity")),
                entry_cost_usd=_text(card.get("entry_cost_usd")),
                max_loss_usd=_text(card.get("structural_max_loss_usd")),
                max_profit_usd=_text(card.get("max_profit_usd")),
                target_usd=_text(card.get("target_usd")),
                planned_stop_usd=_text(card.get("planned_stop_usd")),
                invalidation=_text(card.get("invalidation")),
                trigger=_text(card.get("trigger")),
                counter_argument=_text(card.get("counter_argument")),
                missing_data=_strings(card.get("missing_data")),
                legs=_legs(card),
                primary_outcome=primary,
                other_outcomes=others,
                contradictions=_contradictions(card),
            )
        )

    # Newest session first; a stable secondary key so the order never flickers.
    cards.sort(key=lambda row: (row.session, row.underlying), reverse=True)

    closed = [row.primary_outcome for row in cards if row.primary_outcome is not None]
    total: Decimal | None = None
    for outcome in closed:
        value = _money(outcome.pnl_usd)
        if value is None:
            problems.append(f"{outcome.variant} sonucunda okunamayan P&L: {outcome.pnl_usd!r}")
            continue
        total = value if total is None else total + value

    return OptionsBoard(
        cards=tuple(cards),
        research=research,
        problems=tuple(problems),
        scope_readable=True,
        closed_pnl_usd=total,
        closed_count=len(closed),
    )
