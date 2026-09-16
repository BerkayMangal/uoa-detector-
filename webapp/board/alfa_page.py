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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

from uoa_detector.calibration import load_profile
from webapp.board.aggregate import POSITION_READ_LABELS, BoardRow, build_board_rows
from webapp.board.copy_tr import (
    EVIDENCE_HOVER,
    GATE_LABEL,
    IV_NOT_SELL_VOL,
    NO_CLEAN_CANDIDATE,
    UNKNOWN_NOT_CLEAN,
)
from webapp.board.delayed_panel import DelayedPanel, load_delayed_panels, unreadable_panel
from webapp.board.direction import DIRECTION_LABELS, FALLBACK_MARKER
from webapp.board.evidence import (
    EMPTY_INPUTS,
    LegacyScores,
    RowEvidence,
    build_row_evidence,
    evidence_sort_key,
    guard_strength,
    load_legacy_scores,
    read_evidence_inputs,
    request_for,
    strength_label,
)
from webapp.board.narrative import NARRATIVE_COPY, PenaltyCheck, RowNarrative, build_narrative
from webapp.board.penalty_ledger import (
    LEDGER_COPY,
    M24_STAGE,
    PenaltyLedger,
    build_penalty_ledger,
    read_profile_hashes,
    resolve_profile,
)
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
    from decimal import Decimal

    from sqlalchemy.engine import Engine

    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration.profile import CalibrationProfile
    from webapp.board.evidence import EvidenceCounts, EvidenceInputs, EvidenceRequest, StrengthKey
    from webapp.board.settings import BoardSettings, CleanCandidateSettings, DelayedSettings
    from webapp.board.signals import BoardPrint
    from webapp.board.tradability import DepthView, QuoteView

    QuoteSource = Callable[
        [Sequence[str]], tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]
    ]
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
        # R-EM1 banner variants (review FA-04); "Bugün temiz aday yok" stays copy_tr.NO_CLEAN_CANDIDATE.
        "no_clean_candidate_dated": "{date} seansında temiz aday yok",
        "no_clean_candidate_undated": "Bu çalışmada temiz aday yok",
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
    delayed_source: DelayedSource | None = None,
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
    if prints is None:
        return AlfaPage(
            rows=(), views=(), sections=(), print_count=0, load_failed=True, gate_on=gate_on,
            today=_et_date(moment),
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
    signals = {p.event_id: p.signal for p in prints}
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
        evidence = build_row_evidence(
            row,
            settings=settings,
            now=moment,
            signal=signal,
            telemetry=inputs.telemetry.get(request.event_id),
            tape=inputs.tapes.get((request.ticker, request.trade_date)),
            ticker_info=inputs.infos.get(request.ticker),
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
                    penalties=PenaltyCheck(applied=ledger.applied_names),
                ),
                ledger=ledger,
                clean_candidate=is_clean_candidate(
                    chip, evidence.counts, settings.clean_candidate,
                    non_directional_supporting=len(evidence.non_directional_supporting_labels()),
                ),
                delayed=_row_panel(row.ticker, panels, wired=delayed_source is not None),
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
        today=_et_date(moment),
        newest_print_at=newest,
        rendered_at=moment,
        delayed_failed=delayed_failed,
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


def db_quote_source(engine: Engine) -> QuoteSource:
    """A quote source reading ``alfa_quote`` and ``alfa_contract_depth`` only."""

    def _source(symbols: Sequence[str]) -> tuple[Mapping[str, QuoteView], Mapping[str, DepthView]]:
        return read_board_quotes(engine, symbols)

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
        "no_clean_candidate_label": NO_CLEAN_CANDIDATE,
        "iv_not_sell_vol": IV_NOT_SELL_VOL,
    }
