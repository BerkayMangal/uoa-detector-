"""Reason sentence and mandatory counter-argument per Alfa Board row (Phase 5.2.A5).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-CA1, R-CA2 and
R-WD1; §5 A5.

- **Frozen templates.** Every sentence comes from the dictionaries below.
  Placeholders take row values only: counts, family names from
  ``evidence.FAMILY_LABELS``, the direction, the position read, the
  tradability chip's frozen label and reason, and labels passed in by later
  items. There is no free text.
- **Reason** (bull column): the ``lehte`` families and the position read.
  A non-directional lehte family (dealer gamma, decision P9) is never listed
  under "{direction} gösteriyor". It gets its own clause saying it does not
  point a way and can amplify the move (review FA-03).
- **Counter-argument** (bear column) starts with ``copy_tr.COUNTER_LEAD``
  (``AMA``). It names the single most material negative, in the contract's
  priority order (``COUNTER_PRIORITY``):
  1. İŞLENMEZ or DAR cost (the chip's label and reason);
  2. ``aleyhte`` families;
  3. the chase verdict ``geç kaldın`` (B3; an optional input until then);
  4. a catalyst inside the window (B4; optional input);
  5. ``narrative.counter_min_unknown_families`` or more unknown families;
  6. penalty ledger items (A6; optional input).
- **Fallback.** If none applies, the counter is exactly
  ``copy_tr.NO_COUNTER_FOUND``, followed by ``Bakılanlar: ...`` listing the
  checks performed. An optional input that was not supplied was not checked,
  so it is not listed. ``kotasyon yok`` is not a cost counter-argument (it is
  unknown, not İŞLENMEZ), but the checked list shows it as the cost state. A
  check that ran against a source it could not read is named ``bilinmiyor``,
  never as a measured zero (``CatalystCheck.known``; R-UN1, review FB-H3).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from webapp.board.copy_tr import NO_COUNTER_FOUND, UNKNOWN_NOT_CLEAN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from webapp.board.aggregate import BoardRow
    from webapp.board.evidence import RowEvidence
    from webapp.board.settings import NarrativeSettings
    from webapp.board.tradability import TradabilityRead

CounterKey = Literal["cost", "against", "chase_late", "catalyst", "unknown", "penalties"]

# Contract §5 A5, in order: the first that applies is the row's counter-argument.
COUNTER_PRIORITY: Final[tuple[CounterKey, ...]] = (
    "cost", "against", "chase_late", "catalyst", "unknown", "penalties",
)

NARRATIVE_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bull_title": "Lehte okuma",
        "bear_title": "Karşı argüman",
        "list_separator": ", ",
    },
)

REASON_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "supporting": "Neden: {count} bağımsız kaynak {direction} gösteriyor ({families}); {position}.",
        "supporting_amplified": (
            "Neden: {count} bağımsız kaynak {direction} gösteriyor ({families}); "
            "{amplifiers} yön göstermez, hareketi büyütebilir; {position}."
        ),
        "amplified_only": (
            "Neden: yönü gösteren bağımsız kaynak yok; {amplifiers} yön göstermez, "
            "hareketi büyütebilir; {position}."
        ),
        "no_supporting": (
            "Neden: lehte bağımsız kaynak yok, satırı yalnızca akış baskıları oluşturuyor; {position}."
        ),
    },
)

DIRECTION_OBJECTS: Final[Mapping[str, str]] = MappingProxyType({"up": "yukarıyı", "down": "aşağıyı"})

POSITION_CLAUSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "intentional": "prim tek strike'ta toplanmış (kasıtlı pozisyon)",
        "scattered": "prim birçok strike'a dağılmış (dağınık envanter)",
        "mixed": "prim birkaç strike'a yayılmış (karışık)",
    },
)

COUNTER_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cost": "AMA maliyet: {reason} ({state}).",
        "cost_without_reason": "AMA maliyet: {state}.",
        "against": "AMA {count} aile aleyhte: {families}.",
        "chase_late": "AMA kovalama hükmü: {verdict}.",
        "catalyst": "AMA vade içinde katalizör var: {catalysts}.",
        "unknown": "AMA {count} aile bilinmiyor ({families}); {not_clean}.",
        "penalties": "AMA ceza defterinde uygulanan: {penalties}.",
    },
)

CHECK_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "lead": "Bakılanlar: {checks}.",
        "cost": "maliyet ({state})",
        "against": "aleyhte aile ({count})",
        "chase": "kovalama ({verdict})",
        "catalyst": "vade içi katalizör ({count})",
        # Phase 5.2.B-fix3: the chip could not be read, so its count is not a zero.
        "catalyst_unknown": "vade içi katalizör (bilinmiyor)",
        "unknown": "bilinmeyen aile ({count})",
        "penalties": "ceza defteri ({count} uygulanan)",
    },
)

_COST_STATES: Final = frozenset({"untradable", "narrow"})


@dataclass(frozen=True)
class ChaseCheck:
    """B3 input: the frozen verdict label and whether it is ``geç kaldın``."""

    verdict: str
    late: bool


@dataclass(frozen=True)
class CatalystCheck:
    """B4 input: frozen labels of the catalysts inside the window (empty when none).

    ``known`` is False when the chip could not be read (never fetched, or a part
    that reads ``bilinmiyor``). The checked list then names the check as
    ``bilinmiyor`` instead of a measured ``(0)`` (R-UN1, review FB-H3/FB-03).
    """

    in_window: tuple[str, ...]
    known: bool = True


@dataclass(frozen=True)
class PenaltyCheck:
    """A6 input: frozen Turkish names of the penalties applied to the source print."""

    applied: tuple[str, ...]


@dataclass(frozen=True)
class RowNarrative:
    reason: str
    counter: str  # starts with "AMA", or is exactly NO_COUNTER_FOUND
    counter_key: CounterKey | None  # None: no counter-argument found
    checked: str | None  # "Bakılanlar: ..." only when no counter-argument was found


def _join(items: tuple[str, ...]) -> str:
    return NARRATIVE_COPY["list_separator"].join(items)


def reason_sentence(row: BoardRow, evidence: RowEvidence) -> str:
    position = POSITION_CLAUSES[row.position_read]
    directional = evidence.directional_supporting_labels()
    amplifiers = evidence.non_directional_supporting_labels()
    if not directional and not amplifiers:
        return REASON_TEMPLATES["no_supporting"].format(position=position)
    if not directional:
        return REASON_TEMPLATES["amplified_only"].format(amplifiers=_join(amplifiers), position=position)
    key = "supporting_amplified" if amplifiers else "supporting"
    return REASON_TEMPLATES[key].format(
        count=len(directional),
        direction=DIRECTION_OBJECTS[row.direction],
        families=_join(directional),
        amplifiers=_join(amplifiers),
        position=position,
    )


def build_narrative(
    row: BoardRow,
    evidence: RowEvidence,
    chip: TradabilityRead,
    *,
    settings: NarrativeSettings,
    chase: ChaseCheck | None = None,
    catalyst: CatalystCheck | None = None,
    penalties: PenaltyCheck | None = None,
) -> RowNarrative:
    """The row's reason sentence and its counter-argument (or the explicit fallback)."""
    reason = reason_sentence(row, evidence)
    against = evidence.labels_in("against")
    unknown = evidence.labels_in("unknown")

    found: dict[CounterKey, str] = {}
    if chip.state in _COST_STATES:
        found["cost"] = (
            COUNTER_TEMPLATES["cost"].format(reason=chip.reason, state=chip.label)
            if chip.reason
            else COUNTER_TEMPLATES["cost_without_reason"].format(state=chip.label)
        )
    if against:
        found["against"] = COUNTER_TEMPLATES["against"].format(count=len(against), families=_join(against))
    if chase is not None and chase.late:
        found["chase_late"] = COUNTER_TEMPLATES["chase_late"].format(verdict=chase.verdict)
    if catalyst is not None and catalyst.in_window:
        found["catalyst"] = COUNTER_TEMPLATES["catalyst"].format(catalysts=_join(catalyst.in_window))
    if len(unknown) >= settings.counter_min_unknown_families:
        found["unknown"] = COUNTER_TEMPLATES["unknown"].format(
            count=len(unknown), families=_join(unknown), not_clean=UNKNOWN_NOT_CLEAN,
        )
    if penalties is not None and penalties.applied:
        found["penalties"] = COUNTER_TEMPLATES["penalties"].format(penalties=_join(penalties.applied))

    for key in COUNTER_PRIORITY:
        if key in found:
            return RowNarrative(reason=reason, counter=found[key], counter_key=key, checked=None)

    checks = [
        CHECK_TEMPLATES["cost"].format(state=chip.label),
        CHECK_TEMPLATES["against"].format(count=len(against)),
    ]
    if chase is not None:
        checks.append(CHECK_TEMPLATES["chase"].format(verdict=chase.verdict))
    if catalyst is not None:
        checks.append(
            CHECK_TEMPLATES["catalyst"].format(count=len(catalyst.in_window))
            if catalyst.known
            else CHECK_TEMPLATES["catalyst_unknown"],
        )
    checks.append(CHECK_TEMPLATES["unknown"].format(count=len(unknown)))
    if penalties is not None:
        checks.append(CHECK_TEMPLATES["penalties"].format(count=len(penalties.applied)))
    return RowNarrative(
        reason=reason,
        counter=NO_COUNTER_FOUND,
        counter_key=None,
        checked=CHECK_TEMPLATES["lead"].format(checks=_join(tuple(checks))),
    )
