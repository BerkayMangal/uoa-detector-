"""The pass ledger page model — ``/defter`` (Phase 5.2.C2a).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2) and
``docs/phase-5.2-alfa-board-acceptance.md`` §1 ("It must record what the owner
passed on as carefully as what he took, so his own edge can be measured
forward").

The page lists decision cards newest first, filtered by decision (``log`` /
``pas``) and by ticker, each with its FROZEN snapshot and its outcomes. It is a
pure read: the page model gets the cards and the outcomes and turns them into
text. It makes no Unusual Whales call, and it never recomputes a card — what a
card says is what it said the day it was written.

Two rules shape the whole module:

- **The snapshot is read defensively.** ``card_json`` is whatever the board's
  row view looked like on the day of the press, and the serializer that froze it
  names no field (``cards.snapshot``). A card written by an older — or newer —
  board therefore has fields this page has never heard of, and may be missing
  ones it knows. Every read goes through :func:`_text` / :func:`_count`, which
  answer ``None`` rather than raising, and ``None`` renders as ``bilinmiyor``.
  A ledger that 500s on an old card would lose the very history it exists to
  show.
- **Counts until EVERY group clears the sample gate.** Contract §4: "Nothing is
  aggregated unless each group has at least ``fills.min_n_for_stats`` cards with
  final outcomes. Below that, only counts are shown" — the contract's own line,
  ``12 pas, 3 log; istatistik için yetersiz örnek``. So a full log sample does
  not unlock a median while the pas control group is thin, and vice versa:
  :attr:`HorizonSummary.reportable_groups` is empty until both are ready, and
  :func:`outcomes.excess_stats` returns ``None`` for every statistic of a group
  below the gate, so a template cannot leak one by accident. There is no hit
  rate, no average and no t-statistic here at any sample size, and log and pas
  are measured with identical math so the two stay comparable (§4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from webapp.board.cards import DECISIONS, Json
from webapp.board.fills import FILL_COPY
from webapp.board.outcomes import (
    COMPUTED,
    FINAL_STATUSES,
    OUTCOME_COPY,
    ExcessStats,
    Outcome,
    excess_stats,
    horizon_text,
    signed_pct_text,
)
from webapp.board.tradability import CHIP_COPY

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from webapp.board.cards import DecisionCard

_UNKNOWN: Final = CHIP_COPY["unknown"]
_CREATED_FORMAT: Final = "%Y-%m-%d %H:%M UTC"
_CARD_PATH: Final = "/kart/{card_id}"

# Frozen Turkish copy for the pass ledger (rule R-WD1). Every generated string is
# formatted from these templates, and a test passes each one through
# ``webapp.board.honesty.ensure_clean``.
DEFTER_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "page_title": "Karar defteri",
        "intro": "Loglanan ve pas geçilen kartlar, aynı ölçüyle.",
        # Filters. The control names are Turkish (``karar``, ``hisse``) on purpose:
        # the board must never carry a control named "ticker", "label", "sort" or
        # "min_score" again — those were the old dashboard's score filters.
        "filter_decision": "Karar",
        "filter_ticker": "Hisse",
        "filter_all": "hepsi",
        "filter_submit": "Süz",
        "decision_log": "log",
        "decision_pas": "pas",
        "empty": "Kayıtlı karar kartı yok.",
        "empty_filtered": "Bu süzgeçle eşleşen kart yok.",
        "load_failed": "Defter okunamadı; hiçbir kayıt silinmedi, sadece gösterilemiyor.",
        # The cap is disclosed whether or not it bit, so the page never looks
        # like the whole ledger when it is only the newest page of it.
        "cap": "En son {n} kart listelenir.",
        "capped": "Liste doldu; daha eski kartlar bu sayfada yok.",
        # Contract §4's own example line, with the counts filled in.
        "counts_only": "{pas} pas, {log} log; istatistik için yetersiz örnek",
        "counts": "{pas} pas, {log} log",
        "stats_title": "Sonuç özeti",
        "stats_scope": "bu listedeki kartlar",
        "median": "Medyan fark",
        "iqr": "Çeyrekler arası aralık",
        "sample": "{n} kart",
        "open_card": "Kartı aç",
        "evidence": "Kanıt",
        "evidence_counts": "{supporting} lehte · {against} aleyhte · {unknown} bilinmiyor",
        "reason": "Gerekçe",
        "counter": "Karşı argüman",
        "contract": "Baskın kontrat",
        "outcomes_title": "Sonuç",
    },
)


# ---------------------------------------------------------------------------
# Reading the frozen snapshot (defensively)
# ---------------------------------------------------------------------------


def _node(value: Json, *path: str) -> Json:
    """Walk ``path`` through a frozen snapshot; ``None`` as soon as it does not fit."""
    cursor: Json = value
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


def _text(value: Json, *path: str) -> str | None:
    found = _node(value, *path)
    return found if isinstance(found, str) and found.strip() else None


def _count(value: Json, *path: str) -> int | None:
    found = _node(value, *path)
    if isinstance(found, bool) or not isinstance(found, int):
        return None
    return found


def shown(value: str | None) -> str:
    """A snapshot field as text, or ``bilinmiyor`` when the card did not carry it."""
    return value if value else _UNKNOWN


@dataclass(frozen=True)
class CardFace:
    """What a frozen card shows on the ledger: the row as the owner saw it.

    Every field is optional because the snapshot is generic and spans board
    versions. Nothing here is recomputed; a missing field reads ``bilinmiyor``,
    never a fresh value from today's board.
    """

    strength: str | None
    spread: str | None
    round_trip: str | None
    lot: str | None
    exit_depth: str | None
    quote_age: str | None
    reason: str | None
    counter: str | None
    symbol: str | None
    supporting: int | None
    against: int | None
    unknown: int | None

    @property
    def evidence_counts_text(self) -> str:
        """``4 lehte · 0 aleyhte · 2 bilinmiyor``, or ``bilinmiyor`` for an unreadable card."""
        if self.supporting is None or self.against is None or self.unknown is None:
            return _UNKNOWN
        return DEFTER_COPY["evidence_counts"].format(
            supporting=self.supporting, against=self.against, unknown=self.unknown,
        )


def card_face(card: DecisionCard) -> CardFace:
    """Read one card's frozen row view. Never raises on a card it does not recognise."""
    view: Json = card.card
    return CardFace(
        strength=_text(view, "row_view", "strength_text"),
        spread=_text(view, "row_view", "chip_text", "spread"),
        round_trip=_text(view, "row_view", "chip_text", "round_trip"),
        lot=_text(view, "row_view", "chip_text", "lot"),
        exit_depth=_text(view, "row_view", "chip_text", "exit_depth"),
        quote_age=_text(view, "row_view", "chip_text", "quote_age"),
        reason=_text(view, "row_view", "narrative", "reason"),
        counter=_text(view, "row_view", "narrative", "counter"),
        symbol=_text(view, "row_view", "symbol") or card.dominant_option_symbol,
        supporting=_count(view, "row_view", "evidence", "counts", "supporting"),
        against=_count(view, "row_view", "evidence", "counts", "against"),
        unknown=_count(view, "row_view", "evidence", "counts", "unknown"),
    )


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeCell:
    """One card's outcome at one horizon, or the honest absence of one."""

    horizon_days: int
    outcome: Outcome | None

    @property
    def horizon_text(self) -> str:
        return horizon_text(self.horizon_days)

    @property
    def status_text(self) -> str:
        """The stored status, or ``henüz hesaplanmadı`` when no row exists yet.

        A (card, horizon) with no row has not been measured: either the horizon
        has not passed or the job has not reached it. It is never a zero.
        """
        return OUTCOME_COPY["not_computed"] if self.outcome is None else self.outcome.status

    @property
    def excess_text(self) -> str:
        return _UNKNOWN if self.outcome is None else self.outcome.excess_text

    @property
    def option_bid_text(self) -> str:
        return _UNKNOWN if self.outcome is None else self.outcome.option_bid_text

    @property
    def measured(self) -> bool:
        return self.outcome is not None and self.outcome.status == COMPUTED


@dataclass(frozen=True)
class LedgerRow:
    """One decision card on the ledger: its identity, its frozen face, its outcomes."""

    card: DecisionCard
    face: CardFace
    cells: tuple[OutcomeCell, ...]

    @property
    def href(self) -> str:
        return _CARD_PATH.format(card_id=self.card.id)

    @property
    def decision_text(self) -> str:
        """``loglandı`` / ``pas geçildi`` — the same words the card page uses."""
        return FILL_COPY["decision_log" if self.card.decision == "log" else "decision_pas"]

    @property
    def created_text(self) -> str:
        return self.card.created_at.astimezone(UTC).strftime(_CREATED_FORMAT)


# ---------------------------------------------------------------------------
# Count-gated aggregates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionGroup:
    """One decision's sample at one horizon: how many, and — only above the gate — how much."""

    decision: str
    final_n: int  # cards whose outcome at this horizon reached a final status
    stats: ExcessStats

    @property
    def sample_text(self) -> str:
        return DEFTER_COPY["sample"].format(n=self.stats.n)

    @property
    def median_text(self) -> str:
        return signed_pct_text(self.stats.median_pct)

    @property
    def q1_text(self) -> str:
        return signed_pct_text(self.stats.q1_pct)

    @property
    def q3_text(self) -> str:
        return signed_pct_text(self.stats.q3_pct)


@dataclass(frozen=True)
class HorizonSummary:
    """Both decisions at one horizon. Counts always; statistics only above the gate."""

    horizon_days: int
    groups: tuple[DecisionGroup, ...]

    @property
    def horizon_text(self) -> str:
        return horizon_text(self.horizon_days)

    @property
    def enough(self) -> bool:
        """True only when EVERY group has a reportable sample (contract §4).

        "Nothing is aggregated unless each group has at least
        ``fills.min_n_for_stats`` cards with final outcomes." One full group is
        not enough: a median for the taken cards shown beside a three-card
        control group is an invitation to read a comparison that the samples
        cannot support, which is the one thing the pass ledger exists to avoid.
        """
        return bool(self.groups) and all(group.stats.enough for group in self.groups)

    @property
    def reportable_groups(self) -> tuple[DecisionGroup, ...]:
        """The groups whose statistics may be shown — none until the gate opens.

        The template iterates this rather than :attr:`groups`, so it cannot
        render a median while any group is still below the gate even by
        accident. When it is non-empty, every group in it has a measured sample
        at or above ``min_n``.
        """
        return self.groups if self.enough else ()

    @property
    def counts_text(self) -> str:
        """``12 pas, 3 log`` — with the contract's "yetersiz örnek" tail below the gate.

        The tail is attached while ANY group is short, so the line never reads
        as a finished measurement of a sample that is not there yet.
        """
        counts = {group.decision: group.final_n for group in self.groups}
        key = "counts" if self.enough else "counts_only"
        return DEFTER_COPY[key].format(
            pas=counts.get("pas", 0), log=counts.get("log", 0),
        )


def _group(
    decision: str, rows: Sequence[LedgerRow], horizon_days: int, *, min_n: int,
) -> DecisionGroup:
    found = [
        cell.outcome
        for row in rows
        if row.card.decision == decision
        for cell in row.cells
        if cell.horizon_days == horizon_days and cell.outcome is not None
    ]
    return DecisionGroup(
        decision=decision,
        final_n=sum(1 for outcome in found if outcome.status in FINAL_STATUSES),
        stats=excess_stats(found, min_n=min_n),
    )


def build_summaries(
    rows: Sequence[LedgerRow], *, horizons: Sequence[int], min_n: int,
) -> tuple[HorizonSummary, ...]:
    """One summary per horizon, each with one group per decision, count-gated.

    ``min_n`` is ``fills.min_n_for_stats`` from the board profile — never a
    literal. Each group counts its own measured cards, and the gate opens only
    when EVERY group has reached ``min_n`` (contract §4): log and pas are
    reported together or not at all, because the comparison between them is the
    only reason the numbers are shown.
    """
    return tuple(
        HorizonSummary(
            horizon_days=horizon,
            groups=tuple(
                _group(decision, rows, horizon, min_n=min_n) for decision in DECISIONS
            ),
        )
        for horizon in horizons
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerPage:
    """Everything ``defter.html`` renders. Built once, from one card read and one outcome read."""

    rows: tuple[LedgerRow, ...]
    summaries: tuple[HorizonSummary, ...]
    horizons: tuple[int, ...]
    limit: int
    decision: str  # the active filter, "" when every decision is listed
    ticker: str  # the active filter, "" when every ticker is listed
    load_failed: bool = False
    filtered: bool = False

    @property
    def capped(self) -> bool:
        """True when the page is full, so older cards exist that it does not show."""
        return len(self.rows) >= self.limit

    @property
    def cap_text(self) -> str:
        return DEFTER_COPY["cap"].format(n=self.limit)

    @property
    def empty_text(self) -> str:
        """Says which kind of nothing this is: an empty ledger, or an empty filter."""
        return DEFTER_COPY["empty_filtered" if self.filtered else "empty"]


def build_ledger_page(
    cards: Sequence[DecisionCard],
    outcomes: Sequence[Outcome],
    *,
    horizons: Sequence[int],
    min_n: int,
    limit: int,
    decision: str = "",
    ticker: str = "",
    load_failed: bool = False,
) -> LedgerPage:
    """The ``/defter`` page model: cards as they were frozen, with their outcomes.

    ``cards`` arrive newest first and already filtered and capped by the
    repository; ``outcomes`` are every stored outcome of those cards, in one
    read. Nothing here queries anything.
    """
    by_card: dict[str, dict[int, Outcome]] = {}
    for outcome in outcomes:
        by_card.setdefault(outcome.card_id, {})[outcome.horizon_days] = outcome
    wanted = tuple(horizons)
    rows = tuple(
        LedgerRow(
            card=card,
            face=card_face(card),
            cells=tuple(
                OutcomeCell(horizon_days=horizon, outcome=by_card.get(card.id, {}).get(horizon))
                for horizon in wanted
            ),
        )
        for card in cards
    )
    return LedgerPage(
        rows=rows,
        summaries=build_summaries(rows, horizons=wanted, min_n=min_n),
        horizons=wanted,
        limit=limit,
        decision=decision,
        ticker=ticker,
        load_failed=load_failed,
        filtered=bool(decision or ticker),
    )


def template_context() -> dict[str, object]:
    """Frozen copy and helpers for the ledger templates."""
    return {
        "defter_copy": DEFTER_COPY,
        "defter_decisions": DECISIONS,
        "defter_shown": shown,
        "chip_copy": CHIP_COPY,
    }
