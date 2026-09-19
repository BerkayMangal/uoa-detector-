"""The ``/alfa`` page view model and its frozen Turkish copy (Phase 5.2.A1, A2, A4-A6).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Render"), §5 A1,
§5 A2 ("Gate"), §5 A4, §5 A5 and §5 A6; decisions P12, P15 and P16.

- A1: one row per (ticker, direction) over the whole run.
- A2: each row carries the tradability chip of its dominant contract. The
  gate ``Alabileceklerimi göster`` is on by default:
  - on: İŞLENİR and DAR rows in the main section, then ``kotasyon yok``, then
    ``İŞLENMEZ``, each with its reason;
  - off: every row in one list, each with its chip.
  Rows are never hidden: every row appears in exactly one section.
- A4: each row carries its evidence strip (``webapp/board/evidence.py``), the
  derived evidence word with the hover ``Kâr olasılığı DEĞİL.``, and the
  strength label through the R-UN2 guard.
  - Inside every section rows sort by L desc, A asc, U asc, then total premium
    desc (then ticker and direction, for a stable order).
  - The combined score is never on the row face, on a chip, or a sort key. It
    appears only in the row's ``Denetim`` block, next to its inputs, as
    ``Birleşik skor (denetim, sınırsız ölçek): 0.xx``. It is not clamped.
  - A failed evidence read is flagged on the page, and every family reads
    ``bilinmiyor``: unknown, never clean.
- A5: each row carries its reason sentence and its mandatory counter-argument
  (``webapp/board/narrative.py``), rendered as two columns with the same
  classes (R-CA2).
- A6: each row's audit block carries the penalty ledger of its source print
  (``webapp/board/penalty_ledger.py``), with numbers from the calibration
  profile that wrote the row. Applied penalties feed the counter-argument's
  last priority.
- B1: each row carries its position-size cell (``webapp/board/sizing.py``): one
  lot in dollars and as a share of capital, and the profile risk bucket's lot
  count. The ``max_r`` disclosure goes in the audit block only (R-EV2).
- B2: each row carries the break-even move its dominant contract needs against
  the move the ATM straddle prices in (``webapp/board/moves.py``), read from
  ``alfa_atm`` through an injected source. Missing rows read ``bilinmiyor``,
  and so do rows older than ``tradability.max_quote_age_seconds``, the cutoff
  B3's chase spot already applies to the same table (review FB-H4).
- B3: each row carries its chase verdict (``webapp/board/chase.py``): the print
  price against the current ask, the underlying's move since, and the
  since-print flow as context only. ``geç kaldın`` feeds the counter-argument's
  priority 3, and the verdict is named in the fallback's checked list.
- A7: the page counts clean candidates (R-EM1): İŞLENİR rows whose evidence is
  within ``clean_candidate``. ``min_supporting`` counts only families that
  point the row's way: a non-directional lehte family (dealer gamma, decision
  P9) never makes a row clean on its own (review FA-03).
- R-EM1 banner (review FA-04). With no clean candidate:
  - ``Bugün temiz aday yok`` only when the run's newest print is on today's
    ET date;
  - ``{date} seansında temiz aday yok`` for an earlier session, and
    ``Bu çalışmada temiz aday yok`` when the session date is unknown;
  - never after a failed evidence or quote read: every row then reads
    unknown, so the page says the clean-candidate state could not be read;
  - never for a failed print read or when there is no run: nothing was
    evaluated, and those states have their own copy.

The template only shows strings from the frozen dictionaries here and in
``direction.py``, ``aggregate.py``, ``tradability.py``, ``evidence.py``,
``narrative.py`` and ``penalty_ledger.py`` (rule R-WD1). Summary, chip, audit and narrative lines are
formatted from frozen templates with row values; there is no free text. Quotes
and evidence come from the database through injected sources; this module never
calls Unusual Whales.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

from uoa_detector.calibration import load_profile
from webapp.board.aggregate import POSITION_READ_LABELS, BoardRow, build_board_rows
from webapp.board.atm import read_board_atm
from webapp.board.cards import CARD_COPY
from webapp.board.catalysts import (
    CHIP_UNKNOWN,
    EXPIRED_WINDOW,
    KIND_LABELS,
    M22_MAY_DIFFER,
    read_board_catalysts,
)
from webapp.board.chase import ChaseRead, ChaseText, build_chase, chase_text
from webapp.board.copy_tr import (
    EVIDENCE_HOVER,
    GATE_LABEL,
    IV_NOT_SELL_VOL,
    NO_CLEAN_CANDIDATE,
    UNKNOWN_NOT_CLEAN,
)
from webapp.board.daily_close import ensure_daily_bar_tables, load_bars_by_ticker
from webapp.board.delayed_panel import DelayedPanel, load_delayed_panels, unreadable_panel
from webapp.board.direction import DIRECTION_LABELS, FALLBACK_MARKER
from webapp.board.etf_holdings import read_focused_holdings
from webapp.board.evidence import (
    EMPTY_INPUTS,
    LegacyScores,
    RowEvidence,
    build_row_evidence,
    dominant_print,
    evidence_sort_key,
    guard_strength,
    is_sold,
    load_legacy_scores,
    read_evidence_inputs,
    request_for,
    strength_label,
)
from webapp.board.moves import ATM_AGE_TEMPLATE, compare_moves, expected_move
from webapp.board.narrative import (
    NARRATIVE_COPY,
    CatalystCheck,
    ChaseCheck,
    PenaltyCheck,
    RowNarrative,
    build_narrative,
)
from webapp.board.netprem import read_net_premium_since_many
from webapp.board.oi_confirm import board_state, oi_label, read_board_oi
from webapp.board.penalty_ledger import (
    LEDGER_COPY,
    M24_STAGE,
    PenaltyLedger,
    build_penalty_ledger,
    read_profile_hashes,
    resolve_profile,
)
from webapp.board.portfolio import (
    capital_header,
    journal_overlap,
    same_sector_links,
    single_bet_clusters,
)
from webapp.board.quotes import dominant_symbol, read_board_quotes
from webapp.board.regime import (
    GAMMA_TICKERS,
    STATUS_SEPARATOR,
    RegimeInputs,
    build_regime_band,
    read_regime_inputs,
)
from webapp.board.sizing import SIZE_COPY, SizeRead, SizeText, build_size, size_text
from webapp.board.spot import (
    REASON_NO_STOP_DISTANCE,
    REASON_NOT_ENOUGH_BARS,
    REASON_STALE_SPOT,
    Bar,
    SpotFrame,
    build_spot_frame,
)
from webapp.board.ticker_info import read_ticker_infos
from webapp.board.tradability import (
    CHIP_COPY,
    STATE_LABELS,
    TradabilityRead,
    assess_tradability,
    executable_ask,
    format_pct,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from decimal import Decimal

    from sqlalchemy.engine import Engine

    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration.profile import CalibrationProfile
    from webapp.board.atm import AtmView
    from webapp.board.catalysts import CatalystChip
    from webapp.board.direction import Direction
    from webapp.board.etf_holdings import HoldingView
    from webapp.board.evidence import (
        EvidenceCounts,
        EvidenceInputs,
        EvidenceRequest,
        OIConfirmState,
        StrengthKey,
    )
    from webapp.board.netprem import TapeSummary
    from webapp.board.oi_confirm import OiConfirmView
    from webapp.board.portfolio import (
        CapitalHeader,
        OverlapView,
        SectorLink,
        SingleBetCluster,
    )
    from webapp.board.portfolio import (
        Direction as PortfolioDirection,
    )
    from webapp.board.regime import RegimeBand
    from webapp.board.settings import BoardSettings, CleanCandidateSettings, DelayedSettings
    from webapp.board.signals import BoardPrint
    from webapp.board.tradability import DepthView, QuoteView
    from webapp.journal import TradeRow
    from webapp.ohlc import RegularBar

    QuoteSource = Callable[
        [Sequence[str]], tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]
    ]
    AtmSource = Callable[[Sequence[str]], Mapping[str, tuple[AtmView, ...]]]
    # 5.3.3: stored regular-session bars per ticker, oldest first, for the ATR.
    BarsSource = Callable[[Sequence[str]], Mapping[str, tuple[RegularBar, ...]]]
    # B4: (dominant contract symbol, the source print's ET trade date) → its T+1 confirmation.
    OiKey = tuple[str, date]
    OiSource = Callable[[Sequence[OiKey]], Mapping[OiKey, OiConfirmView]]
    # B4: (ticker, the dominant contract's expiry close) plus the page clock → its chip.
    CatalystKey = tuple[str, datetime]
    CatalystSource = Callable[
        [Sequence[CatalystKey], datetime], Mapping[CatalystKey, CatalystChip]
    ]
    # B5: the latest reading per regime source as of the page clock.
    RegimeSource = Callable[[datetime], RegimeInputs]
    # B6: the open journal, the focused-ETF holdings and the sector of each ticker.
    TradesSource = Callable[[], Sequence[TradeRow]]
    HoldingsSource = Callable[[], Sequence[HoldingView]]
    SectorSource = Callable[[Sequence[str]], Mapping[str, str | None]]
    # B3: (ticker, trade date, the source print's time) → the tape summed since that print.
    FlowSinceKey = tuple[str, date, datetime]
    FlowSinceSource = Callable[[Sequence[FlowSinceKey]], Mapping[FlowSinceKey, TapeSummary]]
    EvidenceSource = Callable[[str, Sequence[EvidenceRequest]], EvidenceInputs]
    ProfileHashSource = Callable[[str, Sequence[str]], Mapping[str, str]]
    ProfileResolver = Callable[[str], CalibrationProfile | None]
    # Phase 5.2.D1: (row tickers, the page's ET date) → each ticker's delayed bucket.
    DelayedSource = Callable[[Sequence[str], date], Mapping[str, DelayedPanel]]

_logger = logging.getLogger(__name__)

SectionKey = Literal["main", "no_quote", "untradable", "all"]

# The calibration profile the live worker scores with; only its spread cutoff
# and its legacy evidence scores are read.
LIVE_CALIBRATION_PROFILE: Final = Path("profiles/v5_default.yaml")
GATE_OFF_PARAM: Final = "off"

# R-CA2: the bull and bear columns share exactly these classes.
CASE_CLASS: Final = "rounded-md border border-gray-800 bg-gray-950/40 px-3 py-2 text-sm leading-snug text-gray-200"
CASE_TITLE_CLASS: Final = "text-xs font-semibold text-gray-500 mb-1"

ALFA_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "page_title": "Alfa Board",
        "intro": (
            "Seçili çalışmanın tüm baskıları hisse ve yön bazında tek satırda "
            "toplanır. Karar desteğidir; hiçbir emir açmaz."
        ),
        "run_label": "Çalışma",
        "summary": "{rows} satır · {prints} baskı",
        # Board-level freshness (restores the A7-removed page badge as a plain, dated line).
        "freshness": "Son baskı {time} ET ({date}) · {age} önce",
        "freshness_unknown": "Son baskı zamanı bilinmiyor",
        "age_seconds": "{n} sn",
        "age_minutes": "{n} dk",
        "age_hours": "{h} sa {m} dk",
        "age_days": "{n} gün",
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
        # A4: evidence and the audit block.
        "evidence_failed": "Kanıt tabloları okunamadı; aileler bilinmiyor, bu temiz demek değil.",
        "audit_summary": "Denetim",
        "audit_source": "Kaynak baskı: {event_id} (baskın sözleşmenin en büyük baskısı)",
        "audit_score": "Birleşik skor (denetim, sınırsız ölçek): {score}",
        "audit_pre": "Ceza öncesi birleşik skor: {score}",
        "audit_inputs": "Skorun girdileri",
        "audit_record_missing": "kayıt okunamadı",
        # B4: the T+1 opening/closing reading of the dominant contract.
        "opening_title": "Açılış mı kapanış mı",
        # B5: how old the reading behind a regime chip is (R-CO2's rule, for a chip).
        "regime_age": "{age} önce alındı",
        # B6: the portfolio-overlap strip and the matching open trade of a row.
        "portfolio_title": "Tek bahis şeridi",
        "overlap_detail_title": "Eşleşen açık işlem",
        "detail_separator": " · ",
        # R-EM1 banner variants (review FA-04); "Bugün temiz aday yok" stays copy_tr.NO_CLEAN_CANDIDATE.
        "no_clean_candidate_dated": "{date} seansında temiz aday yok",
        "no_clean_candidate_undated": "Bu çalışmada temiz aday yok",
        # Phase 5.3: the market being shut is not the same fact as a stale feed.
        # Without this the board shows twenty rows of "kotasyon yok" on a Saturday
        # and never says why, which reads as a broken page rather than a closed one.
        "market_closed_dated": (
            "Piyasa kapalı. Aşağıdaki satırlar {date} seansının verisiyle, canlı kotasyonla değil."
        ),
        "market_closed_undated": "Piyasa kapalı. Son tamamlanmış seans bilinmiyor.",
        "clean_candidate_unread": (
            "Temiz aday durumu okunamadı: kanıt veya kotasyon tabloları okunamadı. "
            "Bu, temiz aday yok demek değil."
        ),
    },
)

# StoredSignal field → audit label, in display order. M27's score is the
# previous session's OI change, not this print (contract §5 A3, decision P10).
AUDIT_INPUT_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "uoa_score": "Akış baskısı (UOA)",
        "convexity_score": "Konveksite",
        "event_score": "Olay takvimi",
        "gamma_score": "Dealer gamma",
        "price_confirmation_score": "Fiyat teyidi",
        "sector_confirmation_score": "Sektör",
        "time_of_day_weight": "Gün içi zaman ağırlığı",
        "cluster_density_score": "Küme yoğunluğu",
        "relative_premium_score": "Göreli prim",
        "dte_multiplier_applied": "Vade çarpanı",
        "opening_closing_score": "önceki seans OI değişimi (bu baskı değil)",
    },
)

OPTION_TYPE_LABELS: Final[Mapping[str, str]] = MappingProxyType({"call": "call", "put": "put"})

# B5: the regime band's own frozen copy, and the empty inputs a failed read renders from.
REGIME_COPY: Final[Mapping[str, str]] = MappingProxyType({"status_separator": STATUS_SEPARATOR})
EMPTY_REGIME_INPUTS: Final = RegimeInputs(
    tide_buckets=(), gamma=(), gex=(), curve=None, vix=None, history=(),
)
REGIME_CHIP_KEYS: Final[tuple[str, ...]] = (
    "tide",
    *(f"gamma:{t}" for t in GAMMA_TICKERS),
    *(f"flip:{t}" for t in GAMMA_TICKERS),
    "curve",
    "vix_curve",
    "vix_spot",
)

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
_ET: Final = ZoneInfo("America/New_York")
# Market structure, not a cutoff: the regular session closes at 16:00 ET, which is where
# the catalyst window of a contract expiring that day ends (contract §6 B4).
_EXPIRY_CLOSE_ET: Final = time(16, 0)
# The Açık pozisyon states that are a reading rather than an absence (R-UN1 dimming).
_OI_KNOWN_STATES: Final = frozenset({"opening", "closing"})
_NO_OI_ROW: Final = "yok"
# B6: the board's direction as ``webapp/board/portfolio.py`` names it.
_PORTFOLIO_DIRECTIONS: Final[Mapping[Direction, PortfolioDirection]] = MappingProxyType(
    {"up": "yukarı", "down": "aşağı"},
)


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
class MoveView:
    """B2: the break-even move against the move the ATM straddle prices in."""

    text: str
    disclosure: str
    age: str | None  # when the ATM row the comparison used was fetched (R-CO2)
    fallback: bool  # the labelled IV estimate was used instead of a straddle
    known: bool  # False: both halves read bilinmiyor


@dataclass(frozen=True)
class OpeningView:
    """B4: the dominant contract's T+1 opening/closing reading and its catalyst chip."""

    label: str  # oi_confirm.STATUS_LABELS, or "henüz doğrulanmadı" with no confirmation row
    status: str  # the stored status, or "yok" when there is no row yet
    state: OIConfirmState | None  # what the Açık pozisyon family read (None: T+1 awaited)
    dimmed: bool  # unknown or out of scope: dashed and dimmed, never clean (R-UN1)
    catalyst_text: str  # the chip, or the out-of-scope line of an expired contract
    catalyst_in_window: tuple[str, ...]  # frozen kind labels; feeds the counter-argument (A5)
    catalyst_dimmed: bool

    @property
    def catalyst_known(self) -> bool:
        """Was the chip read at all? A dimmed chip is one whose count is not a zero.

        Phase 5.2.B-fix3 (review FB-H3/FB-03): with no chip, or with any part
        reading ``bilinmiyor``, the row must not claim ``vade içi katalizör (0)``
        or ``data-catalyst="yok"`` (R-UN1).
        """
        return not self.catalyst_dimmed


@dataclass(frozen=True)
class RegimeView:
    """B5: the board-level regime band, with each source's age.

    Context only. It never enters the evidence counts, the strength label, the
    clean-candidate rule or the counter-argument choice.
    """

    band: RegimeBand
    ages: Mapping[str, str]  # chip key → how old the reading behind it is


@dataclass(frozen=True)
class AuditView:
    """The ``Denetim`` block: the only place the combined score is shown (R-EV2)."""

    source_text: str
    score_text: str
    pre_text: str
    inputs: tuple[tuple[str, str], ...]  # (label, value)


@dataclass(frozen=True)
class AlfaRowView:
    row: BoardRow
    symbol: str | None  # the dominant contract's quote symbol
    chip: TradabilityRead
    chip_text: ChipText
    evidence: RowEvidence
    strength_key: StrengthKey  # through the R-UN2 guard
    strength_text: str  # through the R-UN2 guard
    audit: AuditView
    narrative: RowNarrative
    ledger: PenaltyLedger
    clean_candidate: bool  # R-EM1
    # Phase 5.2.D1 (R-DL1): the delayed bucket. Never counted, never part of any score.
    delayed: DelayedPanel | None = None
    # FAZ B fields are appended with defaults, so every earlier construction stays valid.
    size: SizeRead | None = None  # B1
    size_text: SizeText | None = None  # B1
    move: MoveView | None = None  # B2
    chase: ChaseRead | None = None  # B3
    chase_text: ChaseText | None = None  # B3
    opening: OpeningView | None = None  # B4
    overlap: OverlapView | None = None  # B6
    # Phase 5.3.3: the spot frame, appended with defaults so every earlier
    # construction in the tests stays valid (the FAZ B pattern).
    spot: SpotFrame | None = None
    spot_text: SpotText | None = None


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
    evidence_failed: bool = False
    clean_candidate_count: int = 0
    session_date: date | None = None  # ET date of the run's newest print
    today: date | None = None  # ET date of the page's clock
    newest_print_at: datetime | None = None  # the run's newest print (or signal) time
    rendered_at: datetime | None = None  # the page's clock
    delayed_failed: bool = False  # Phase 5.2.D1: the delayed bucket could not be read
    # Phase 5.3: passed in by the caller; the page model stays pure and never
    # reads a clock or a calendar of its own.
    market_closed: bool = False
    regime: RegimeView | None = None  # B5; None only when no regime source was supplied
    # B6: the capital header and the single-bet strip. None and empty mean "no source".
    capital: CapitalHeader | None = None
    clusters: tuple[SingleBetCluster, ...] = ()
    sector_links: tuple[SectorLink, ...] = ()

    @property
    def no_clean_candidate(self) -> bool:
        """R-EM1: no row qualifies; never claimed for a failed print, evidence or quote read."""
        return not self._read_failed and self.clean_candidate_count == 0

    @property
    def clean_candidate_unread(self) -> bool:
        """No clean candidate only because evidence or quotes could not be read: unknown, not "none"."""
        return not self.load_failed and (self.evidence_failed or self.quotes_failed)

    @property
    def no_clean_candidate_text(self) -> str:
        """``Bugün temiz aday yok`` for today's session; a dated or undated variant otherwise."""
        if self.session_date is None:
            return ALFA_COPY["no_clean_candidate_undated"]
        if self.session_date == self.today:
            return NO_CLEAN_CANDIDATE
        return ALFA_COPY["no_clean_candidate_dated"].format(date=self.session_date.isoformat())

    @property
    def market_closed_text(self) -> str:
        """Says the market is shut and dates what is on screen. Never implies a live quote."""
        if self.session_date is None:
            return ALFA_COPY["market_closed_undated"]
        return ALFA_COPY["market_closed_dated"].format(date=self.session_date.isoformat())

    @property
    def _read_failed(self) -> bool:
        return self.load_failed or self.evidence_failed or self.quotes_failed

    @property
    def summary(self) -> str:
        return ALFA_COPY["summary"].format(rows=len(self.rows), prints=self.print_count)

    @property
    def freshness(self) -> str:
        """The newest print's ET time, date and age; never an implied "live" claim."""
        if self.newest_print_at is None or self.rendered_at is None:
            return ALFA_COPY["freshness_unknown"]
        newest = _aware(self.newest_print_at)
        local = newest.astimezone(_ET)
        return ALFA_COPY["freshness"].format(
            time=local.strftime("%H:%M"),
            date=local.date().isoformat(),
            age=age_text(_aware(self.rendered_at) - newest),
        )


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def age_text(delta: timedelta) -> str:
    """A Turkish age: seconds under a minute, minutes under an hour, then hours, then days."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return ALFA_COPY["age_seconds"].format(n=seconds)
    if seconds < 3600:
        return ALFA_COPY["age_minutes"].format(n=seconds // 60)
    if seconds < 86400:
        return ALFA_COPY["age_hours"].format(h=seconds // 3600, m=(seconds % 3600) // 60)
    return ALFA_COPY["age_days"].format(n=seconds // 86400)


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


def _score(value: float | None) -> str:
    """Two decimals, sign kept, never clamped: the score is not a 0-1 gauge."""
    return ALFA_COPY["unknown"] if value is None else f"{value:.2f}"


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


def build_audit(event_id: str | None, signal: StoredSignal | None) -> AuditView:
    """The ``Denetim`` block of a row's source print (the largest print of the dominant contract)."""
    source = ALFA_COPY["audit_source"].format(event_id=event_id or ALFA_COPY["unknown"])
    if signal is None:
        missing = ALFA_COPY["audit_record_missing"]
        return AuditView(
            source_text=source,
            score_text=ALFA_COPY["audit_score"].format(score=missing),
            pre_text=ALFA_COPY["audit_pre"].format(score=missing),
            inputs=(),
        )
    return AuditView(
        source_text=source,
        score_text=ALFA_COPY["audit_score"].format(score=_score(signal.combined_score_post_penalty)),
        pre_text=ALFA_COPY["audit_pre"].format(score=_score(signal.combined_score_pre_penalty)),
        inputs=tuple(
            (label, _score(getattr(signal, name))) for name, label in AUDIT_INPUT_LABELS.items()
        ),
    )


# ---------------------------------------------------------------------------
# Phase 5.3.3: the spot frame on the row (contract §3.2, rules R-SP1-R-SP8)
# ---------------------------------------------------------------------------

# The arithmetic lives in webapp/board/spot.py, which holds no UI text by design;
# these are the only sentences the frame speaks. No probability is stated and the
# frame never says al or sat (R-SP4, R-SP5).
SPOT_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "title": "Hisse çerçevesi",
        "option_detail": "Opsiyon detayı",
        "entry": "giriş {price}",
        "entry_unknown": "giriş bilinmiyor",
        "stop": "stop {price} · mesafe {distance} (%{pct})",
        "stop_unknown": "stop bilinmiyor",
        "shares": "{shares} hisse · riske edilen {risked} / {risk}",
        "shares_unknown": "adet bilinmiyor",
        # Not "beklenen hareket": the board's word for this quantity is the straddle's own
        # pricing, and test_alfa_render_hygiene forbids "beklenen"/"expected" page-wide so
        # the vol board's "IV-implied 1σ" stays the only reading of an implied move.
        "target": "hedef {price} (ATM straddle) · {r}R",
        "target_unknown": "hedef bilinmiyor (ATM straddle okunamadı)",
        "atr": "ATR({period}) {atr} · {sessions} seans",
        "atr_unknown": "ATR bilinmiyor · {sessions} seans",
        "capped": "pozisyon üst sınırı bağladı: sermayenin en çok %{pct} kadarı",
        "not_enough_bars": "yeterli günlük bar yok",
        "no_stop_distance": "stop mesafesi yok; adet hesaplanmaz",
        "stale_spot": "spot fiyat yok veya eski; çerçeve kurulmaz",
        # R-EV1 is pinned by a COUNT: "olasılı" may appear exactly once per row, in the
        # evidence hover. A frame that denied being a probability would double it, so the
        # denial is phrased as "tahmin değildir" — the word moves.py already uses.
        "disclosure": (
            "Hedef, ATM straddle'ın bu vadeye fiyatladığı hareket kadar uzaklıktadır; tahmin "
            "değildir. Adet, R tutarına göre aşağı yuvarlanır."
        ),
    },
)

# spot.py returns reason KEYS, never sentences, so the mapping lives here.
_SPOT_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        REASON_NOT_ENOUGH_BARS: SPOT_COPY["not_enough_bars"],
        REASON_NO_STOP_DISTANCE: SPOT_COPY["no_stop_distance"],
        REASON_STALE_SPOT: SPOT_COPY["stale_spot"],
    },
)


@dataclass(frozen=True)
class SpotText:
    """The row's rendered spot cells. Every unknown carries the reason it is unknown."""

    title: str
    entry: str
    stop: str
    shares: str
    target: str
    atr: str
    reason: str | None  # R-SP1, R-SP2, R-SP8
    capped: str | None  # R-SP6: disclosed, never silent
    disclosure: str
    # P15: the share count rests on capital_usd and r_usd. While the owner has not
    # confirmed them the cell says so, exactly as the option size cell does; 5.3.5
    # flips the flag and both markers disappear together.
    default_marker: str | None
    known: bool  # False: the frame was refused and says so


def _dollars(value: float | None) -> str:
    """Two decimals: these are share prices, not premiums."""
    return ALFA_COPY["unknown"] if value is None else f"${value:,.2f}"


def spot_text(frame: SpotFrame, *, settings: BoardSettings) -> SpotText:
    """Render one frame. A refused cell reads ``bilinmiyor``, never a fallback number."""
    shares = (
        SPOT_COPY["shares"].format(
            shares=frame.shares,
            risked=_dollars(frame.risked_usd),
            risk=_dollars(frame.risk_usd),
        )
        if frame.shares is not None
        else SPOT_COPY["shares_unknown"]
    )
    return SpotText(
        title=SPOT_COPY["title"],
        entry=(
            SPOT_COPY["entry"].format(price=_dollars(frame.entry))
            if frame.entry is not None
            else SPOT_COPY["entry_unknown"]
        ),
        stop=(
            SPOT_COPY["stop"].format(
                price=_dollars(frame.stop),
                distance=_dollars(frame.stop_distance),
                pct=f"{frame.stop_pct:.2f}",
            )
            if frame.stop is not None and frame.stop_distance is not None
            and frame.stop_pct is not None
            else SPOT_COPY["stop_unknown"]
        ),
        shares=shares,
        target=(
            SPOT_COPY["target"].format(
                price=_dollars(frame.target), r=f"{frame.r_to_target:.1f}",
            )
            if frame.target is not None and frame.r_to_target is not None
            else SPOT_COPY["target_unknown"]
        ),
        atr=(
            SPOT_COPY["atr"].format(
                period=settings.spot.atr_period,
                atr=_dollars(frame.atr),
                sessions=frame.sessions_used,
            )
            if frame.atr is not None
            else SPOT_COPY["atr_unknown"].format(sessions=frame.sessions_used)
        ),
        reason=_SPOT_REASONS.get(frame.reason) if frame.reason is not None else None,
        capped=(
            SPOT_COPY["capped"].format(
                pct=f"{settings.spot.max_position_pct_of_capital:.0f}",
            )
            if frame.capped_by_position_limit
            else None
        ),
        disclosure=SPOT_COPY["disclosure"],
        default_marker=(
            None if settings.sizing.values_confirmed_by_owner else SIZE_COPY["default_value"]
        ),
        known=frame.reason is None,
    )


def build_row_spot_frame(
    row: BoardRow,
    bars: Sequence[RegularBar],
    atm_rows: Sequence[AtmView],
    now: datetime,
    *,
    settings: BoardSettings,
) -> SpotFrame:
    """The row's frame, entered at the same price B2 prices its comparison from.

    The entry is a *fresh* ATM row's ``stock_price`` under the identical filter
    ``build_move_view`` applies, so the two cells of one row can never disagree
    about what the stock costs (contract §3.2). With no fresh row there is no
    entry, and R-SP8 refuses the whole frame rather than sizing a real position
    off a price that no longer exists.

    The target is the expected move ``moves.py`` already computes from the ATM
    straddle, applied in the row's direction. Nothing new is derived here (R-SP4).
    """
    moment = _aware(now)
    max_age = settings.tradability.max_quote_age_seconds
    fresh = tuple(
        r for r in atm_rows
        if (moment - _aware(r.fetched_at)).total_seconds() <= max_age
    )
    entry = next((r.stock_price for r in fresh if r.stock_price is not None), None)
    expected = (
        expected_move(atm_rows=fresh, expiry=row.dominant.key.expiry, now=moment)
        if fresh
        else None
    )
    target: float | None = None
    if entry is not None and expected is not None:
        move = entry * expected.expected_move_pct / 100.0
        target = entry + move if row.direction == "up" else entry - move
    return build_spot_frame(
        [Bar(high=bar.high, low=bar.low, close=bar.close) for bar in bars],
        entry=entry,
        direction=row.direction,
        spot=settings.spot,
        sizing=settings.sizing,
        target=target,
        entry_is_stale=entry is None,
    )


def build_move_view(
    row: BoardRow,
    chip: TradabilityRead,
    atm_rows: Sequence[AtmView],
    now: datetime,
    *,
    max_age_seconds: int,
) -> MoveView:
    """B2: the dominant contract's break-even move vs the ATM straddle for that expiry.

    The entry price is the executable ask (B1's rule), so an unexecutable or
    stale quote reads ``bilinmiyor`` rather than pricing a break-even nobody
    could pay. An ATM row older than ``max_age_seconds`` is refused the same
    way: ``alfa_atm`` rows survive restarts and weekends, and B3's chase spot
    already refuses them, so pricing a cost statement off one would make the two
    halves of one row disagree (review FB-H4). The row that was used carries its
    own age, and so does the newest stale row when nothing fresh is left, so a
    ``bilinmiyor`` reading says why.
    """
    moment = _aware(now)
    fresh = tuple(
        r for r in atm_rows
        if (moment - _aware(r.fetched_at)).total_seconds() <= max_age_seconds
    )
    key = row.dominant.key
    option_type: Literal["call", "put"] = "call" if key.option_type == "call" else "put"
    comparison = compare_moves(
        option_type=option_type,
        strike=float(key.strike),
        expiry=key.expiry,
        ask=executable_ask(chip),
        atm_rows=fresh,
        now=now,
    )
    expected = comparison.expected
    used = (
        next((r for r in fresh if r.expiry == expected.atm_expiry), None)
        if expected is not None and expected.atm_expiry is not None
        else None
    )
    if used is None and not fresh and atm_rows:
        used = max(atm_rows, key=lambda r: _aware(r.fetched_at))
    return MoveView(
        text=comparison.text,
        disclosure=comparison.disclosure,
        age=(
            ATM_AGE_TEMPLATE.format(age=age_text(_aware(now) - _aware(used.fetched_at)))
            if used is not None
            else None
        ),
        fallback=expected is not None and expected.source == "iv_estimate",
        known=comparison.required_move_pct is not None or expected is not None,
    )


def build_regime_view(
    inputs: RegimeInputs, *, settings: BoardSettings, now: datetime,
) -> RegimeView:
    """B5: the band plus the age of the reading behind each chip (contract §6 B5)."""
    moment = _aware(now)
    stamps: dict[str, datetime] = {}
    if inputs.tide_buckets:
        stamps["tide"] = inputs.tide_buckets[-1].fetched_at
    for reading in inputs.gamma:
        stamps[f"gamma:{reading.ticker}"] = reading.fetched_at
    for levels in inputs.gex:
        stamps[f"flip:{levels.ticker}"] = levels.fetched_at
    if inputs.curve is not None:
        stamps["curve"] = inputs.curve.fetched_at
    if inputs.vix is not None:
        stamps["vix_spot"] = inputs.vix.fetched_at
    return RegimeView(
        band=build_regime_band(inputs, settings=settings, now=now),
        ages=MappingProxyType({
            key: ALFA_COPY["regime_age"].format(age=age_text(moment - _aware(stamp)))
            for key, stamp in stamps.items()
        }),
    )


def expiry_close(expiry: date) -> datetime:
    """The end of a row's catalyst window: its dominant contract's expiry, 16:00 ET (§6 B4)."""
    return datetime.combine(expiry, _EXPIRY_CLOSE_ET, tzinfo=_ET)


def build_opening_view(
    oi: OiConfirmView | None, chip: CatalystChip | None, *, expired: bool,
) -> OpeningView:
    """B4: the row's opening/closing line and catalyst chip, from frozen labels only.

    No confirmation row reads ``henüz doğrulanmadı`` — the T+1 job has not
    answered for this contract yet, which is not the same as "not opening".
    A chip that could not be read reads ``bilinmiyor``; a contract whose expiry
    has passed has an empty window and reads out of scope, never "no catalyst".
    """
    state = board_state(oi)
    if chip is not None:
        catalyst_text = chip.text
    elif expired:
        catalyst_text = EXPIRED_WINDOW
    else:
        catalyst_text = CHIP_UNKNOWN
    return OpeningView(
        label=oi_label(oi),
        status=oi.status if oi is not None else _NO_OI_ROW,
        state=state,
        dimmed=state not in _OI_KNOWN_STATES,
        catalyst_text=catalyst_text,
        catalyst_in_window=(
            tuple(KIND_LABELS[part.kind] for part in chip.parts if part.state == "var")
            if chip is not None
            else ()
        ),
        catalyst_dimmed=chip is None or any(part.state == "bilinmiyor" for part in chip.parts),
    )


def is_clean_candidate(
    chip: TradabilityRead,
    counts: EvidenceCounts,
    settings: CleanCandidateSettings,
    *,
    non_directional_supporting: int = 0,
) -> bool:
    """R-EM1: İŞLENİR (not DAR, not kotasyon yok) with the evidence inside ``clean_candidate``.

    ``min_supporting`` counts only lehte families that point the row's way, so
    ``non_directional_supporting`` (dealer gamma, decision P9) is taken out first.
    """
    return (
        chip.state == "tradable"
        and counts.supporting - non_directional_supporting >= settings.min_supporting
        and counts.against <= settings.max_against
        and counts.unknown <= settings.max_unknown
    )


def _et_date(moment: datetime) -> date:
    return _aware(moment).astimezone(_ET).date()


def _view_order(view: AlfaRowView) -> tuple[int, int, int, Decimal, str, str]:
    """L desc, A asc, U asc, total premium desc; ticker and direction only break exact ties."""
    supporting, against, unknown, premium = evidence_sort_key(view.evidence.counts, view.row.total_premium)
    return supporting, against, unknown, premium, view.row.ticker, view.row.direction


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
    evidence_source: EvidenceSource | None = None,
    legacy_scores: LegacyScores | None = None,
    profile_hash_source: ProfileHashSource | None = None,
    profile_resolver: ProfileResolver | None = None,
    run_latest_ts: datetime | None = None,
    market_closed: bool = False,
    delayed_source: DelayedSource | None = None,
    atm_source: AtmSource | None = None,
    bars_source: BarsSource | None = None,
    flow_since_source: FlowSinceSource | None = None,
    oi_source: OiSource | None = None,
    catalyst_source: CatalystSource | None = None,
    regime_source: RegimeSource | None = None,
    trades_source: TradesSource | None = None,
    holdings_source: HoldingsSource | None = None,
    sector_source: SectorSource | None = None,
) -> AlfaPage:
    """The page model; ``prints=None`` means the database read failed.

    The session date for the R-EM1 banner is the ET date of the newest print,
    or of ``run_latest_ts`` (the run's newest signal time) when no print parsed.

    Without a quote source every chip reads ``kotasyon yok``. A failed quote
    read sets ``quotes_failed`` and leaves every chip unknown, never clean.
    Without an evidence source the telemetry families read as legacy (and
    ``bilinmiyor`` without legacy scores). A failed evidence read sets
    ``evidence_failed`` and every family reads ``bilinmiyor``.
    Without a profile hash source, or when it fails, the penalty ledger says
    the writing profile could not be read and shows no profile numbers.
    """
    moment = now if now is not None else datetime.now(UTC)
    regime = _read_regime(regime_source, settings=settings, now=moment)
    if prints is None:
        return AlfaPage(
            rows=(), views=(), sections=(), print_count=0, load_failed=True, gate_on=gate_on,
            today=_et_date(moment), regime=regime, market_closed=market_closed,
        )
    rows = build_board_rows(prints, settings.aggregation)
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
    requests = [request_for(r) for r in rows]
    inputs: EvidenceInputs = EMPTY_INPUTS
    evidence_failed = False
    if evidence_source is not None and requests:
        try:
            inputs = evidence_source(prints[0].run_id, requests)
        except Exception:
            _logger.exception("alfa board: evidence read failed; every family reads bilinmiyor")
            evidence_failed = True
    hashes: Mapping[str, str] = {}
    if profile_hash_source is not None and requests:
        try:
            hashes = profile_hash_source(prints[0].run_id, [r.event_id for r in requests])
        except Exception:
            _logger.exception("alfa board: profile hash read failed; ledgers show no profile numbers")
    panels: Mapping[str, DelayedPanel] = {}
    delayed_failed = False
    if delayed_source is not None and rows:
        try:
            panels = delayed_source([r.ticker for r in rows], _et_date(moment))
        except Exception:
            _logger.exception("alfa board: delayed read failed; the bucket reads bilinmiyor")
            delayed_failed = True
    atm_rows: Mapping[str, tuple[AtmView, ...]] = {}
    if atm_source is not None and rows:
        try:
            atm_rows = atm_source([r.ticker for r in rows])
        except Exception:
            _logger.exception("alfa board: ATM read failed; the move comparison reads bilinmiyor")
    # 5.3.3: stored bars for the ATR. A failed read leaves the frame refused with
    # its reason, exactly as too few bars would (R-SP1); it never guesses a stop.
    bars: Mapping[str, tuple[RegularBar, ...]] = {}
    if bars_source is not None and rows:
        try:
            bars = bars_source([r.ticker for r in rows])
        except Exception:
            _logger.exception("alfa board: bar read failed; the spot frame reads bilinmiyor")
    oi_rows: Mapping[OiKey, OiConfirmView] = {}
    if oi_source is not None and requests:
        try:
            oi_rows = oi_source([
                (symbol, request.trade_date)
                for symbol, request in zip(symbols, requests, strict=True)
                if symbol is not None
            ])
        except Exception:
            _logger.exception("alfa board: T+1 confirmation read failed; Açık pozisyon reads bilinmiyor")
    chips: Mapping[CatalystKey, CatalystChip] = {}
    if catalyst_source is not None and rows:
        try:
            chips = catalyst_source(
                [
                    (row.ticker.upper(), expiry_close(row.dominant.key.expiry))
                    for row in rows
                    if expiry_close(row.dominant.key.expiry) > moment
                ],
                moment,
            )
        except Exception:
            _logger.exception("alfa board: catalyst read failed; the chip reads bilinmiyor")
    today = _et_date(moment)
    # B6: a failed journal read leaves the capital header out. Rendering "$0 at risk"
    # from a read that failed would be a claim about the account, not an unknown.
    trades: Sequence[TradeRow] | None = None
    if trades_source is not None:
        try:
            trades = trades_source()
        except Exception:
            _logger.exception("alfa board: open journal read failed; no capital header is shown")
    holdings: Sequence[HoldingView] = ()
    if holdings_source is not None:
        try:
            holdings = holdings_source()
        except Exception:
            _logger.exception("alfa board: ETF holdings read failed; no cluster is claimed")
    overlap_tickers = list(dict.fromkeys(
        [row.ticker.strip().upper() for row in rows]
        + [
            trade.ticker.strip().upper()
            for trade in (trades or ())
            if isinstance(trade.ticker, str) and trade.ticker.strip()
        ],
    ))
    sectors: Mapping[str, str | None] = {}
    if sector_source is not None and overlap_tickers:
        try:
            sectors = sector_source(overlap_tickers)
        except Exception:
            _logger.exception("alfa board: sector read failed; no weak link is claimed")
    signals = {p.event_id: p.signal for p in prints}
    flow_since: Mapping[FlowSinceKey, TapeSummary] = {}
    if flow_since_source is not None and requests:
        try:
            flow_since = flow_since_source(
                [
                    (r.ticker, r.trade_date, signals[r.event_id].timestamp)
                    for r in requests
                    if r.event_id in signals
                ],
            )
        except Exception:
            _logger.exception("alfa board: since-print tape read failed; chase context reads bilinmiyor")
    views: list[AlfaRowView] = []
    for row, symbol, request in zip(rows, symbols, requests, strict=True):
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
        signal = signals.get(request.event_id)
        window_end = expiry_close(row.dominant.key.expiry)
        opening = build_opening_view(
            oi_rows.get((symbol, request.trade_date)) if symbol is not None else None,
            chips.get((row.ticker.upper(), window_end)),
            expired=window_end <= moment,
        )
        evidence = build_row_evidence(
            row,
            settings=settings,
            now=moment,
            signal=signal,
            telemetry=inputs.telemetry.get(request.event_id),
            tape=inputs.tapes.get((request.ticker, request.trade_date)),
            ticker_info=inputs.infos.get(request.ticker),
            oi_state=opening.state,
            legacy_scores=legacy_scores,
            unread=evidence_failed,
        )
        strength = guard_strength(evidence.strength, evidence.counts, settings.evidence)
        profile_hash = hashes.get(request.event_id)
        stage_rows = inputs.telemetry.get(request.event_id)
        ledger = build_penalty_ledger(
            signal,
            profile_hash=profile_hash,
            profile=profile_resolver(profile_hash) if profile_resolver is not None and profile_hash else None,
            m24_telemetry=stage_rows.get(M24_STAGE) if stage_rows is not None else None,
        )
        row_size = build_size(
            signal=signal, chip=chip, sizing=settings.sizing, cost=settings.cost,
        )
        row_chase = build_chase(
            signal=signal,
            chip=chip,
            direction=row.direction,
            sold=is_sold(dominant_print(row), row.direction),
            atm_rows=atm_rows.get(row.ticker, ()),
            tape_since=(
                flow_since.get((request.ticker, request.trade_date, signal.timestamp))
                if signal is not None
                else None
            ),
            settings=settings.chase,
            max_spot_age_seconds=settings.tradability.max_quote_age_seconds,
            now=moment,
        )
        row_spot = build_row_spot_frame(
            row, bars.get(row.ticker, ()), atm_rows.get(row.ticker, ()), moment,
            settings=settings,
        )
        views.append(
            AlfaRowView(
                row=row,
                symbol=symbol,
                chip=chip,
                chip_text=chip_text(chip),
                evidence=evidence,
                strength_key=strength,
                strength_text=strength_label(strength, evidence.counts, settings.evidence),
                audit=build_audit(request.event_id, signal),
                narrative=build_narrative(
                    row, evidence, chip,
                    settings=settings.narrative,
                    chase=ChaseCheck(verdict=row_chase.label, late=row_chase.late),
                    # The catalyst check only ran when a source supplied the chip (§5 A5),
                    # and a chip we could not read is named unknown, never a zero (B-fix3).
                    catalyst=(
                        CatalystCheck(
                            in_window=opening.catalyst_in_window,
                            known=opening.catalyst_known,
                        )
                        if catalyst_source is not None
                        else None
                    ),
                    penalties=PenaltyCheck(applied=ledger.applied_names),
                ),
                ledger=ledger,
                clean_candidate=is_clean_candidate(
                    chip, evidence.counts, settings.clean_candidate,
                    non_directional_supporting=len(evidence.non_directional_supporting_labels()),
                ),
                delayed=_row_panel(row.ticker, panels, wired=delayed_source is not None),
                spot=row_spot,
                spot_text=spot_text(row_spot, settings=settings),
                size=row_size,
                size_text=size_text(row_size),
                move=build_move_view(
                    row, chip, atm_rows.get(row.ticker, ()), moment,
                    max_age_seconds=settings.tradability.max_quote_age_seconds,
                ),
                chase=row_chase,
                chase_text=chase_text(row_chase),
                opening=opening,
                overlap=(
                    journal_overlap(
                        trades, ticker=row.ticker,
                        direction=_PORTFOLIO_DIRECTIONS[row.direction], today=today,
                    )
                    if trades is not None
                    else None
                ),
            ),
        )
    ordered = tuple(sorted(views, key=_view_order))
    newest = max((p.timestamp for row in rows for p in row.prints), default=run_latest_ts)
    return AlfaPage(
        rows=tuple(v.row for v in ordered),
        views=ordered,
        sections=_sections(ordered, gate_on),
        print_count=len(prints),
        load_failed=False,
        gate_on=gate_on,
        quotes_failed=quotes_failed,
        evidence_failed=evidence_failed,
        clean_candidate_count=sum(1 for v in ordered if v.clean_candidate),
        session_date=_et_date(newest) if newest is not None else None,
        today=today,
        newest_print_at=newest,
        rendered_at=moment,
        delayed_failed=delayed_failed,
        market_closed=market_closed,
        regime=regime,
        capital=(
            capital_header(trades, settings=settings, today=today) if trades is not None else None
        ),
        clusters=(
            single_bet_clusters(overlap_tickers, holdings, settings=settings) if holdings else ()
        ),
        sector_links=(
            same_sector_links(overlap_tickers, sectors, settings=settings) if sectors else ()
        ),
    )


def _row_panel(
    ticker: str, panels: Mapping[str, DelayedPanel], *, wired: bool,
) -> DelayedPanel | None:
    """A row's delayed bucket; a missing or failed read renders unknown, never an empty block.

    The reader keys its panels by ``ticker.strip().upper()``, so the lookup uses
    the same spelling: a row ticker with surrounding whitespace used to miss a
    panel that had been read and render the read-failure state (Phase
    5.2.D-fix3, review FD-05).
    """
    if not wired:
        return None
    return panels.get(ticker.strip().upper()) or unreadable_panel(ticker)


def _read_regime(
    source: RegimeSource | None, *, settings: BoardSettings, now: datetime,
) -> RegimeView | None:
    """The regime band, or None when no source was supplied.

    A failed read still renders the band: every chip then reads ``bilinmiyor``,
    which is what a source we could not read means (R-UN1).
    """
    if source is None:
        return None
    try:
        inputs = source(now)
    except Exception:
        _logger.exception("alfa board: regime read failed; every regime chip reads bilinmiyor")
        inputs = EMPTY_REGIME_INPUTS
    return build_regime_view(inputs, settings=settings, now=now)


def db_quote_source(engine: Engine) -> QuoteSource:
    """A quote source reading ``alfa_quote`` and ``alfa_contract_depth`` only."""

    def _source(symbols: Sequence[str]) -> tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]:
        return read_board_quotes(engine, symbols)

    return _source


def db_atm_source(engine: Engine) -> AtmSource:
    """An ATM source reading ``alfa_atm`` only (B2; no Unusual Whales call)."""

    def _source(tickers: Sequence[str]) -> Mapping[str, tuple[AtmView, ...]]:
        return read_board_atm(engine, tickers)

    return _source


def db_bars_source(engine: Engine) -> BarsSource:
    """A bar source reading ``alfa_daily_bar`` only (5.3.3; no Unusual Whales call).

    The bars were already fetched and stored by the ``daily_close`` job, so the
    spot frame costs the render one query and the budget nothing.
    """

    def _source(tickers: Sequence[str]) -> Mapping[str, tuple[RegularBar, ...]]:
        # Ensure at read time, as delayed_panel.py does for alfa_daily_close: a board
        # whose bar table has never been written must render a refused frame with its
        # reason, not log a caught exception on every request. checkfirst, never altering.
        ensure_daily_bar_tables(engine)
        return load_bars_by_ticker(engine, tickers)

    return _source


def db_flow_since_source(engine: Engine) -> FlowSinceSource:
    """A since-print tape source reading ``alfa_net_prem`` only (B3 context; no UW call)."""

    def _source(keys: Sequence[FlowSinceKey]) -> Mapping[FlowSinceKey, TapeSummary]:
        return read_net_premium_since_many(engine, keys)

    return _source


def db_oi_source(engine: Engine) -> OiSource:
    """A T+1 confirmation source reading ``alfa_oi_confirm`` only (B4; no UW call)."""

    def _source(keys: Sequence[OiKey]) -> Mapping[OiKey, OiConfirmView]:
        return read_board_oi(engine, keys)

    return _source


def db_catalyst_source(engine: Engine, settings: BoardSettings) -> CatalystSource:
    """A catalyst source reading ``alfa_catalyst`` and its fetch coverage only (B4; no UW call)."""

    def _source(keys: Sequence[CatalystKey], now: datetime) -> Mapping[CatalystKey, CatalystChip]:
        return read_board_catalysts(engine, keys, settings=settings, now=now)

    return _source


def db_holdings_source(engine: Engine) -> HoldingsSource:
    """A holdings source reading ``alfa_etf_holding`` only (B6; no Unusual Whales call)."""

    def _source() -> Sequence[HoldingView]:
        return read_focused_holdings(engine)

    return _source


def db_sector_source(engine: Engine) -> SectorSource:
    """A sector source reading ``alfa_ticker_info`` only (B6; no Unusual Whales call)."""

    def _source(tickers: Sequence[str]) -> Mapping[str, str | None]:
        return {
            ticker: info.sector for ticker, info in read_ticker_infos(engine, tickers).items()
        }

    return _source


def db_regime_source(engine: Engine) -> RegimeSource:
    """A regime source reading ``alfa_regime`` only (B5; no Unusual Whales call)."""

    def _source(now: datetime) -> RegimeInputs:
        return read_regime_inputs(engine, now=now)

    return _source


def db_evidence_source(engine: Engine) -> EvidenceSource:
    """An evidence source reading telemetry, the net-premium tape and ticker info only."""

    def _source(run_id: str, requests: Sequence[EvidenceRequest]) -> EvidenceInputs:
        return read_evidence_inputs(engine, run_id, requests)

    return _source


def db_delayed_source(engine: Engine, settings: DelayedSettings) -> DelayedSource:
    """A delayed source reading ``alfa_delayed``, its fetch coverage and ``alfa_daily_close`` only."""

    def _source(tickers: Sequence[str], today: date) -> Mapping[str, DelayedPanel]:
        return load_delayed_panels(engine, tickers, today=today, settings=settings)

    return _source


def db_profile_hash_source(engine: Engine) -> ProfileHashSource:
    """A source of ``SignalRow.profile_content_hash`` per event (reads the database only)."""

    def _source(run_id: str, event_ids: Sequence[str]) -> Mapping[str, str]:
        return read_profile_hashes(engine, run_id, event_ids)

    return _source


def resolve_writing_profile(content_hash: str) -> CalibrationProfile | None:
    """The calibration profile in ``profiles/`` whose content hash wrote the row, if any."""
    return resolve_profile(content_hash)


def load_spread_cutoff_pct(profile_path: Path = LIVE_CALIBRATION_PROFILE) -> float:
    """``penalty_triggers.spread_pct_threshold`` of the live calibration profile (read only)."""
    return load_profile(profile_path).penalty_triggers.spread_pct_threshold


def load_live_legacy_scores(profile_path: Path = LIVE_CALIBRATION_PROFILE) -> LegacyScores:
    """Legacy evidence scores of the live calibration profile (read only)."""
    return load_legacy_scores(profile_path)


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
        "evidence_hover": EVIDENCE_HOVER,
        "unknown_not_clean": UNKNOWN_NOT_CLEAN,
        "narrative_copy": NARRATIVE_COPY,
        "case_class": CASE_CLASS,
        "case_title_class": CASE_TITLE_CLASS,
        "ledger_copy": LEDGER_COPY,
        "size_copy": SIZE_COPY,
        "no_clean_candidate_label": NO_CLEAN_CANDIDATE,
        "iv_not_sell_vol": IV_NOT_SELL_VOL,
        # Phase 5.2.C1b: the decision-card buttons every row carries.
        "card_copy": CARD_COPY,
        # B4: the audit block discloses that the chip and M22's event score can disagree.
        "catalyst_note": M22_MAY_DIFFER,
        # B5: the band's own frozen copy (the tripwire line and its status).
        "regime_copy": REGIME_COPY,
        # 5.3.3: the spot frame's only sentences, and the option fold's summary.
        "spot_copy": SPOT_COPY,
    }
