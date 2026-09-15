"""The ``/alfa`` page view model and its frozen Turkish copy (Phase 5.2.A1).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Render"), §5 A1;
decision P12 (the board is built on ``/alfa`` first; A7 moves it to ``/``).

The template only shows strings from the frozen dictionaries here and in
``direction.py`` / ``aggregate.py`` (rule R-WD1). Summary lines are formatted
from frozen templates with row values; there is no free text.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from webapp.board.aggregate import POSITION_READ_LABELS, BoardRow, build_board_rows
from webapp.board.direction import DIRECTION_LABELS, FALLBACK_MARKER

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from webapp.board.settings import BoardSettings
    from webapp.board.signals import BoardPrint

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


@dataclass(frozen=True)
class AlfaPage:
    rows: tuple[BoardRow, ...]
    print_count: int
    load_failed: bool

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


def build_alfa_page(prints: Sequence[BoardPrint] | None, settings: BoardSettings) -> AlfaPage:
    """The page model; ``prints=None`` means the database read failed."""
    if prints is None:
        return AlfaPage(rows=(), print_count=0, load_failed=True)
    rows = build_board_rows(prints, settings.aggregation)
    return AlfaPage(rows=tuple(rows), print_count=len(prints), load_failed=False)


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
    }
