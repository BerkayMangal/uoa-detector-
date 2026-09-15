"""The ``/alfa`` page view model and its frozen Turkish copy (Phase 5.2.A1, A2).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Render"), §5 A1
and §5 A2 ("Gate"); decisions P12, P15 and P16.

- A1: one row per (ticker, direction) over the whole run.
- A2: each row carries the tradability chip of its dominant contract. The
  gate ``Alabileceklerimi göster`` is on by default:
  - on: İŞLENİR and DAR rows in the main section, then ``kotasyon yok``, then
    ``İŞLENMEZ``, each with its reason;
  - off: every row in one list, each with its chip.
  Rows are never hidden: every row appears in exactly one section.

The template only shows strings from the frozen dictionaries here and in
``direction.py``, ``aggregate.py`` and ``tradability.py`` (rule R-WD1).
Summary and chip lines are formatted from frozen templates with row values;
there is no free text. Quotes come from the database through a quote source;
this module never calls Unusual Whales.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from uoa_detector.calibration import load_profile
from webapp.board.aggregate import POSITION_READ_LABELS, BoardRow, build_board_rows
from webapp.board.copy_tr import GATE_LABEL
from webapp.board.direction import DIRECTION_LABELS, FALLBACK_MARKER
from webapp.board.quotes import dominant_symbol, read_board_quotes
from webapp.board.tradability import (
    CHIP_COPY,
    STATE_LABELS,
    TradabilityRead,
    assess_tradability,
    format_pct,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.settings import BoardSettings
    from webapp.board.signals import BoardPrint
    from webapp.board.tradability import DepthView, QuoteView

    QuoteSource = Callable[
        [Sequence[str]], tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]
    ]

_logger = logging.getLogger(__name__)

SectionKey = Literal["main", "no_quote", "untradable", "all"]

# The calibration profile the live worker scores with; only its spread cutoff is read.
LIVE_CALIBRATION_PROFILE: Final = Path("profiles/v5_default.yaml")
GATE_OFF_PARAM: Final = "off"

ALFA_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "page_title": "Alfa Board",
        "intro": (
            "Seçili çalışmanın tüm baskıları hisse ve yön bazında tek satırda "
            "toplanır. Karar desteğidir; hiçbir emir açmaz."
        ),
        "run_label": "Çalışma",
        "summary": "{rows} satır · {prints} baskı",
        "empty_run": "Bu çalışmada baskı yok.",
        "no_runs": "Henüz kayıtlı çalışma yok.",
        "load_failed": "Baskılar okunamadı. Bu, boş bir tahta demek değil.",
        "col_premium": "Toplam prim",
        "col_prints": "Baskı",
        "col_contracts": "Farklı sözleşme",
        "col_dominant": "Baskın sözleşme",
        "col_concentration": "Konsantrasyon",
        "concentration_help": "Satır priminin tek strike'ta toplanan payı",
        "col_dominance": "Hakimiyet",
        "dominance_help": "Aynı hissenin iki yönlü priminde bu yönün payı",
        "col_position": "Pozisyon okuması",
        "side_counts": "{side_aware} baskı alım/satım tarafından · {fallback} baskı opsiyon tipinden",
        "detail_summary": "Baskıların hepsi ({count})",
        "th_time": "Zaman (UTC)",
        "th_contract": "Sözleşme",
        "th_premium": "Prim",
        "th_price": "Baskı fiyatı",
        "th_side": "Taraf",
        "th_direction_source": "Yön kaynağı",
        "source_side": "alım/satım tarafı",
        "source_type": "opsiyon tipi",
        "unknown": "bilinmiyor",
        # A2: cost gate and sections.
        "gate_on": "açık",
        "gate_off": "kapalı",
        "section_main": "İŞLENİR ve DAR",
        "section_no_quote": "kotasyon yok: bilinmeyen durum, İŞLENMEZ demek değil",
        "section_untradable": "İŞLENMEZ: sebebiyle listelenir, silinmez",
        "section_all": "Tüm satırlar (kapı kapalı)",
        "section_count": "{title} ({count})",
        "main_empty": "Kapıdan geçen satır yok.",
        "quotes_failed": "Kotasyonlar okunamadı; maliyet hücreleri bilinmiyor.",
    },
)

OPTION_TYPE_LABELS: Final[Mapping[str, str]] = MappingProxyType({"call": "call", "put": "put"})

FILL_SIDE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "at_ask": "ask (alım)",
        "above_ask": "ask üstü (alım)",
        "at_bid": "bid (satım)",
        "below_bid": "bid altı (satım)",
        "midpoint": "orta (taraf belirsiz)",
        "unknown": "bilinmiyor",
    },
)

_MAIN_STATES: Final = frozenset({"tradable", "narrow"})


@dataclass(frozen=True)
class ChipText:
    """Display strings for one tradability chip, formatted from frozen templates."""

    label: str
    reason: str | None
    spread: str
    bid_ask: str | None
    round_trip: str
    lot: str
    exit_depth: str
    quote_age: str | None
    last_trade: str | None
    default_marker: str | None  # "(varsayılan değer)" while owner values are unconfirmed


@dataclass(frozen=True)
class AlfaRowView:
    row: BoardRow
    symbol: str | None  # the dominant contract's quote symbol
    chip: TradabilityRead
    chip_text: ChipText


@dataclass(frozen=True)
class AlfaSection:
    key: SectionKey
    title: str
    views: tuple[AlfaRowView, ...]


@dataclass(frozen=True)
class AlfaPage:
    rows: tuple[BoardRow, ...]
    views: tuple[AlfaRowView, ...]
    sections: tuple[AlfaSection, ...]
    print_count: int
    load_failed: bool
    gate_on: bool = True
    quotes_failed: bool = False

    @property
    def summary(self) -> str:
        return ALFA_COPY["summary"].format(rows=len(self.rows), prints=self.print_count)


def side_counts_text(row: BoardRow) -> str:
    return ALFA_COPY["side_counts"].format(
        side_aware=row.side_aware_prints, fallback=row.fallback_prints,
    )


def detail_summary_text(row: BoardRow) -> str:
    return ALFA_COPY["detail_summary"].format(count=row.print_count)


def fill_side_label(fill_side: str | None) -> str:
    if fill_side is None:
        return ALFA_COPY["unknown"]
    return FILL_SIDE_LABELS.get(fill_side, ALFA_COPY["unknown"])


def section_heading(section: AlfaSection) -> str:
    return ALFA_COPY["section_count"].format(title=section.title, count=len(section.views))


def _usd(value: float) -> str:
    return f"{value:.2f}"


def _share_pct(value: float) -> str:
    """Two decimals, trailing zeros dropped: a 1-lot share is usually below 1% of capital."""
    return f"{round(value, 2):g}"


def chip_text(read: TradabilityRead) -> ChipText:
    """The chip's display strings. Mid is never shown (R-CO1)."""
    unknown = CHIP_COPY["unknown"]
    bid_ask = (
        CHIP_COPY["bid_ask"].format(bid=_usd(read.bid), ask=_usd(read.ask))
        if read.bid is not None and read.ask is not None
        else None
    )
    lot = (
        f"${read.lot_cost_usd:,.0f} · " + CHIP_COPY["lot_pct"].format(pct=_share_pct(read.lot_pct_capital))
        if read.lot_cost_usd is not None and read.lot_pct_capital is not None
        else unknown
    )
    exit_depth = (
        f"{CHIP_COPY['contracts'].format(size=read.exit_depth)} ({CHIP_COPY['at_last_trade']})"
        if read.exit_depth is not None
        else unknown
    )
    return ChipText(
        label=read.label,
        reason=read.reason,
        spread=f"%{format_pct(read.spread_pct)}" if read.spread_pct is not None else unknown,
        bid_ask=bid_ask,
        round_trip=f"${read.round_trip_usd:.2f}" if read.round_trip_usd is not None else unknown,
        lot=lot,
        exit_depth=exit_depth,
        quote_age=(
            CHIP_COPY["quote_age"].format(seconds=read.quote_age_seconds)
            if read.quote_age_seconds is not None
            else None
        ),
        last_trade=(
            CHIP_COPY["last_trade"].format(minutes=read.last_trade_minutes)
            if read.last_trade_minutes is not None
            else None
        ),
        default_marker=None if read.values_confirmed else CHIP_COPY["default_value"],
    )


def _sections(views: tuple[AlfaRowView, ...], gate_on: bool) -> tuple[AlfaSection, ...]:
    if not gate_on:
        return (AlfaSection(key="all", title=ALFA_COPY["section_all"], views=views),)
    main = tuple(v for v in views if v.chip.state in _MAIN_STATES)
    no_quote = tuple(v for v in views if v.chip.state == "no_quote")
    untradable = tuple(v for v in views if v.chip.state == "untradable")
    sections = [AlfaSection(key="main", title=ALFA_COPY["section_main"], views=main)]
    if no_quote:
        sections.append(AlfaSection(key="no_quote", title=ALFA_COPY["section_no_quote"], views=no_quote))
    if untradable:
        sections.append(
            AlfaSection(key="untradable", title=ALFA_COPY["section_untradable"], views=untradable),
        )
    return tuple(sections)


def build_alfa_page(
    prints: Sequence[BoardPrint] | None,
    settings: BoardSettings,
    *,
    gate_on: bool = True,
    spread_cutoff_pct: float | None = None,
    now: datetime | None = None,
    quote_source: QuoteSource | None = None,
) -> AlfaPage:
    """The page model; ``prints=None`` means the database read failed.

    Without a quote source every chip reads ``kotasyon yok``. A failed quote
    read sets ``quotes_failed`` and leaves every chip unknown, never clean.
    """
    if prints is None:
        return AlfaPage(
            rows=(), views=(), sections=(), print_count=0, load_failed=True, gate_on=gate_on,
        )
    rows = tuple(build_board_rows(prints, settings.aggregation))
    symbols = tuple(dominant_symbol(r) for r in rows)
    wanted = [s for s in symbols if s is not None]
    quotes: Mapping[str, QuoteView] = {}
    depths: Mapping[str, DepthView] = {}
    quotes_failed = False
    if quote_source is not None and wanted:
        try:
            quotes, depths = quote_source(wanted)
        except Exception:
            _logger.exception("alfa board: quote read failed; chips read kotasyon yok")
            quotes_failed = True
    moment = now if now is not None else datetime.now(UTC)
    views: list[AlfaRowView] = []
    for row, symbol in zip(rows, symbols, strict=True):
        chip = assess_tradability(
            row.ticker,
            quotes.get(symbol) if symbol is not None else None,
            depths.get(symbol) if symbol is not None else None,
            tradability=settings.tradability,
            spread_cutoff_pct=spread_cutoff_pct,
            cost=settings.cost,
            sizing=settings.sizing,
            now=moment,
        )
        views.append(AlfaRowView(row=row, symbol=symbol, chip=chip, chip_text=chip_text(chip)))
    frozen_views = tuple(views)
    return AlfaPage(
        rows=rows,
        views=frozen_views,
        sections=_sections(frozen_views, gate_on),
        print_count=len(prints),
        load_failed=False,
        gate_on=gate_on,
        quotes_failed=quotes_failed,
    )


def db_quote_source(engine: Engine) -> QuoteSource:
    """A quote source reading ``alfa_quote`` and ``alfa_contract_depth`` only."""

    def _source(symbols: Sequence[str]) -> tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]:
        return read_board_quotes(engine, symbols)

    return _source


def load_spread_cutoff_pct(profile_path: Path = LIVE_CALIBRATION_PROFILE) -> float:
    """``penalty_triggers.spread_pct_threshold`` of the live calibration profile (read only)."""
    return load_profile(profile_path).penalty_triggers.spread_pct_threshold


def template_context() -> dict[str, object]:
    """Frozen copy and label helpers for ``alfa.html`` and ``_alfa_row.html``."""
    return {
        "copy": ALFA_COPY,
        "direction_labels": DIRECTION_LABELS,
        "position_labels": POSITION_READ_LABELS,
        "option_type_labels": OPTION_TYPE_LABELS,
        "fallback_marker": FALLBACK_MARKER,
        "side_counts_text": side_counts_text,
        "detail_summary_text": detail_summary_text,
        "fill_side_label": fill_side_label,
        "chip_copy": CHIP_COPY,
        "state_labels": STATE_LABELS,
        "gate_label": GATE_LABEL,
        "gate_off_param": GATE_OFF_PARAM,
        "section_heading": section_heading,
    }
