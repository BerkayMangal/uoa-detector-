"""The row's ``ek kanıt (gecikmeli)`` block (Phase 5.2.D1-D3, contract §7, rule R-DL1).

This is the render side of ``webapp/board/delayed.py``. It reads the database
only — stored delayed rows, their fetch coverage and the daily closes — and
makes zero Unusual Whales calls (contract §4.1).

Honesty rules this module carries (§2):

- **Never counted (R-DL1).** No type here has an evidence-count field, and no
  panel is ever passed to ``evidence.py``. The bucket cannot move the L/A/U
  counts, the strength label, the clean-candidate rule, the R-EM1 banner, the
  counter-argument choice or the row order.
- **Unknown is not clean (R-UN1).** A family that was never fetched reads
  ``bilinmiyor`` and renders dashed and dimmed. Only a family whose source
  actually answered may read ``kayıt yok``.
- **Every source shows its age.** Each item carries its filing (or as-of) date
  and its delay; each family carries the date of its last check.
- **Truncation is disclosed**, never silently dropped.

Display volume. A family can hold far more rows than a row should show (FTD
alone runs 20-40 days per active ticker inside its window), so the newest
``delayed.max_items_per_family`` items are listed and the rest are disclosed as
a number. Nothing is deleted: ``alfa_delayed`` stays append-only.

``RENDERED_FAMILIES`` grows per commit (D1 Kongre, D2 İçeriden, D3 Short and
FTD) so each commit stays green on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

from webapp.board.daily_close import ensure_daily_close_tables, load_closes_by_ticker
from webapp.board.delayed import (
    BUCKET_LABEL,
    EXCLUSION_NOTE,
    DelayedFamily,
    build_delayed_evidence,
    ensure_delayed_tables,
    family_label,
    load_delayed_records_by_ticker,
)
from webapp.board.delayed_coverage import coverage_by_family, load_coverage
from webapp.board.honesty import ensure_clean

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from sqlalchemy.engine import Engine

    from webapp.board.delayed import DelayedItem
    from webapp.board.delayed_coverage import CoverageView
    from webapp.board.settings import DelayedSettings

# Families with a rendered block. D1 ships Kongre; D2 and D3 append to this tuple.
RENDERED_FAMILIES: Final[tuple[DelayedFamily, ...]] = ("congress",)

PanelState = Literal["items", "empty", "never_fetched", "unanswered", "unreadable"]

_ET: Final = ZoneInfo("America/New_York")

# Frozen Turkish copy (R-WD1: every generated string passes ensure_clean).
_TEXT: Final[Mapping[str, str]] = MappingProxyType({
    "state.never_fetched": "bilinmiyor — bu aile hiç çekilmedi",
    "state.unanswered": "bilinmiyor — kaynak yanıt vermedi (son deneme {date})",
    "state.empty": "kayıt yok — kaynak yanıt verdi, pencerede kayıt yok (son kontrol {date})",
    "state.unreadable": "gecikmeli ek kanıt okunamadı; bu, kayıt yok demek değil",
    "note.checked": "son kontrol {date}",
    "note.checked_unknown": "son kontrol tarihi bilinmiyor",
    "note.stale": "son deneme ({date}) başarısız; kayıtlar {success} itibarıyla",
    "note.truncated": "kaynak, döndürdüğünden fazla kayıt olduğunu bildirdi; liste eksik olabilir",
    "omitted": "en yeni {shown} kayıt gösteriliyor; {omitted} kayıt daha var",
})


def _say(key: str, **values: object) -> str:
    return ensure_clean(_TEXT[key].format(**values))


@dataclass(frozen=True)
class DelayedFamilyPanel:
    """One delayed family in the bucket. Nothing here is, or feeds, an evidence count."""

    family: DelayedFamily
    label: str
    state: PanelState
    state_text: str | None  # None while items are listed
    dimmed: bool  # R-UN1: unknown renders dashed and dimmed
    items: tuple[DelayedItem, ...]  # newest first, capped for display
    omitted: int  # stored items beyond the display cap
    omitted_text: str | None
    notes: tuple[str, ...]  # freshness, staleness and truncation disclosures


@dataclass(frozen=True)
class DelayedPanel:
    """A row's ``ek kanıt (gecikmeli)`` bucket. Never enters any evidence count (R-DL1)."""

    ticker: str
    bucket_label: str
    exclusion_note: str
    families: tuple[DelayedFamilyPanel, ...]


def build_delayed_panel(
    ticker: str,
    items: Sequence[DelayedItem],
    coverage: Mapping[str, CoverageView],
    *,
    settings: DelayedSettings,
) -> DelayedPanel:
    """Group a ticker's delayed items into the rendered families, with their coverage state."""
    return DelayedPanel(
        ticker=ticker.strip().upper(),
        bucket_label=BUCKET_LABEL,
        exclusion_note=EXCLUSION_NOTE,
        families=tuple(
            _family_panel(
                family,
                [item for item in items if item.family == family],
                coverage.get(family),
                settings=settings,
            )
            for family in RENDERED_FAMILIES
        ),
    )


def unreadable_panel(ticker: str) -> DelayedPanel:
    """The panel of a failed read: unknown everywhere, never an empty block that looks clean."""
    text = _say("state.unreadable")
    return DelayedPanel(
        ticker=ticker.strip().upper(),
        bucket_label=BUCKET_LABEL,
        exclusion_note=EXCLUSION_NOTE,
        families=tuple(
            DelayedFamilyPanel(
                family=family,
                label=family_label(family),
                state="unreadable",
                state_text=text,
                dimmed=True,
                items=(),
                omitted=0,
                omitted_text=None,
                notes=(),
            )
            for family in RENDERED_FAMILIES
        ),
    )


def load_delayed_panels(
    engine: Engine,
    tickers: Sequence[str],
    *,
    today: date,
    settings: DelayedSettings,
) -> dict[str, DelayedPanel]:
    """Every requested ticker's delayed panel, read in three queries. Zero UW calls.

    The delayed tables are created once per engine, so a fresh database renders
    the honest ``bilinmiyor`` state instead of failing the page.
    """
    symbols = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    if not symbols:
        return {}
    _ensure_tables(engine)
    records = load_delayed_records_by_ticker(engine, symbols)
    closes = load_closes_by_ticker(engine, symbols)
    coverage = load_coverage(engine, symbols)
    out: dict[str, DelayedPanel] = {}
    for symbol in symbols:
        evidence = build_delayed_evidence(
            symbol, records.get(symbol, ()), closes.get(symbol, ()), today=today, settings=settings,
        )
        out[symbol] = build_delayed_panel(
            symbol,
            evidence.items,
            coverage_by_family(coverage, symbol),
            settings=settings,
        )
    return out


# The engine whose delayed tables are known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def _ensure_tables(engine: Engine) -> None:
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_delayed_tables(engine)  # also creates the fetch-coverage table
        ensure_daily_close_tables(engine)
        _tables_ready_for[:] = [engine]


def _family_panel(
    family: DelayedFamily,
    items: Sequence[DelayedItem],
    coverage: CoverageView | None,
    *,
    settings: DelayedSettings,
) -> DelayedFamilyPanel:
    label = family_label(family)
    if not items:
        state, text = _empty_state(coverage)
        return DelayedFamilyPanel(
            family=family, label=label, state=state, state_text=text, dimmed=True,
            items=(), omitted=0, omitted_text=None, notes=(),
        )
    shown = tuple(items[: settings.max_items_per_family])
    omitted = len(items) - len(shown)
    return DelayedFamilyPanel(
        family=family,
        label=label,
        state="items",
        state_text=None,
        dimmed=False,
        items=shown,
        omitted=omitted,
        omitted_text=_say("omitted", shown=len(shown), omitted=omitted) if omitted else None,
        notes=_notes(coverage),
    )


def _empty_state(coverage: CoverageView | None) -> tuple[PanelState, str]:
    """No item to show: unknown unless the source actually answered (R-UN1)."""
    if coverage is None:
        return "never_fetched", _say("state.never_fetched")
    if not coverage.answered:
        return "unanswered", _say("state.unanswered", date=_et_day(coverage.last_attempt_at))
    return "empty", _say("state.empty", date=_et_day(coverage.last_attempt_at))


def _notes(coverage: CoverageView | None) -> tuple[str, ...]:
    """Freshness first, then a failed newest attempt, then the source's truncation signal."""
    if coverage is None:
        # Rows stored before coverage was recorded: say so rather than imply a fresh check.
        return (_say("note.checked_unknown"),)
    notes = [_say("note.checked", date=_et_day(coverage.last_attempt_at))]
    if coverage.last_attempt_failed and coverage.last_success_at is not None:
        notes.append(_say(
            "note.stale",
            date=_et_day(coverage.last_attempt_at),
            success=_et_day(coverage.last_success_at),
        ))
    if coverage.truncated:
        notes.append(_say("note.truncated"))
    return tuple(notes)


def _et_day(moment: datetime) -> str:
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    return aware.astimezone(_ET).date().isoformat()
