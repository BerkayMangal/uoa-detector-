"""Four-state evidence per counted family (Phase 5.2.A3).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-UN1/R-UN2, §5 A3,
§5 A4 (counts and the strength label) and §9 (the mapping, cell by cell);
decisions P8 (``nötr``), P9 (dealer gamma is never ``aleyhte``) and P10
(``Açık pozisyon`` comes from T+1 confirmation only).

Every counted family gets one state: ``lehte``, ``aleyhte``, ``bilinmiyor`` or
``kapsam-dışı``. The display variant ``nötr (ölçüldü, yön göstermiyor)`` is a
measured result that points neither way; it enters no count.

Sources, per family:

- **Dealer gamma, Karanlık havuz, Sektör, Fiyat teyidi.** The stage telemetry
  of the row's source print: the largest-premium print of the dominant
  contract (``alfa_stage_telemetry``, written by the live worker since A0c).
  - The §9 table maps each branch string. A branch the table does not name
    reads ``bilinmiyor``, as does a missing stage row.
  - A degraded provider call on that print always reads ``bilinmiyor``.
  - Sektör ``no_sector`` is ``kapsam-dışı`` only when ``alfa_ticker_info`` says
    the ticker is an ETF or an index. For a common stock, or with no info row,
    it is ``bilinmiyor``.
- **Orientation.** M23, M25 and M26 score relative to option type. When the
  side-aware direction reverses the option-type direction (a sold option),
  ``lehte`` and ``aleyhte`` swap. M21 is non-directional (P9) and is never
  flipped. Akış and Açık pozisyon are read in the row's own direction, so they
  are not flipped either.
- **Akış.** The net-premium tape (``alfa_net_prem``), summed over the source
  print's trading day as of the last fetch. The row direction's net aggressor
  premium above ``evidence.flow_net_premium_deadband_usd`` is ``lehte``; the
  opposite side above it is ``aleyhte``; anything inside it is ``nötr``. No
  tape, or a tape last fetched more than ``tape.max_age_seconds`` ago, is
  ``bilinmiyor``.
- **Açık pozisyon.** The optional T+1 confirmation state (B4). Without one it
  reads ``bilinmiyor (T+1 bekleniyor)``.
- **Legacy prints** (no telemetry row at all). Only the persisted values in §9
  map, compared with the live calibration profile's scores. Every other
  telemetry family reads ``bilinmiyor``, labelled ``telemetri yok (eski
  satır)``. M27's ``opening_closing_score`` never maps.

Counts: ``L`` is lehte, ``A`` aleyhte, ``U`` bilinmiyor. ``nötr`` and
``kapsam-dışı`` are in none of them. The strength label passes the R-UN2
guard, which refuses ``Güçlü`` when ``U`` exceeds
``evidence.max_unknown_for_strong``.

Every label comes from the frozen dictionaries below (R-WD1). Page renders read
the database only, through ``read_evidence_inputs``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from uoa_detector.calibration import load_profile
from webapp.board.copy_tr import STRONG_LABEL
from webapp.board.direction import option_type_direction
from webapp.board.netprem import ensure_netprem_tables, read_tape_summaries
from webapp.board.telemetry import AlfaStageTelemetry, ensure_telemetry_tables
from webapp.board.ticker_info import ensure_ticker_info_tables, read_ticker_infos

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from decimal import Decimal
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration.profile import CalibrationProfile
    from webapp.board.aggregate import BoardRow, RowPrint
    from webapp.board.direction import Direction
    from webapp.board.netprem import TapeSummary
    from webapp.board.settings import BoardSettings, EvidenceSettings
    from webapp.board.ticker_info import TickerInfoView

_logger = logging.getLogger(__name__)

FamilyState = Literal["supporting", "against", "neutral", "unknown", "out_of_scope"]
StrengthKey = Literal["strong", "moderate", "weak"]
OIConfirmState = Literal["opening", "closing", "unconfirmed", "expires_before_t1"]

_ET: Final = ZoneInfo("America/New_York")
_SECONDS_PER_MINUTE: Final = 60
_TAPE_SOURCE: Final = "alfa_net_prem"
_OI_SOURCE: Final = "alfa_oi_confirm"
_NO_SECTOR_BRANCH: Final = "no_sector"

# ---------------------------------------------------------------------------
# Frozen copy (R-WD1)
# ---------------------------------------------------------------------------

FAMILY_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "flow": "Akış",
        "dealer_gamma": "Dealer gamma",
        "dark_pool": "Karanlık havuz",
        "sector": "Sektör",
        "price_confirmation": "Fiyat teyidi",
        "open_interest": "Açık pozisyon",
    },
)

STATE_LABELS: Final[Mapping[FamilyState, str]] = MappingProxyType(
    {
        "supporting": "lehte",
        "against": "aleyhte",
        "neutral": "nötr (ölçüldü, yön göstermiyor)",
        "unknown": "bilinmiyor",
        "out_of_scope": "kapsam-dışı",
    },
)

# A note starting with "(" follows the state directly; any other note follows " · ".
NOTE_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "amplifies": "(hareketi büyütebilir)",
        "degraded": "sağlayıcı çağrısı bu baskıda bozuldu",
        "legacy": "telemetri yok (eski satır)",
        "legacy_value": "eski satır: kayıtlı skordan okundu",
        "no_stage_row": "bu aşama için telemetri yok",
        "unmapped_branch": "tanınmayan aşama sonucu",
        "no_sector": "sektör verisi yok",
        "no_sector_unverified": "sektör verisi yok; hisse türü doğrulanmadı",
        "fund_or_index": "ETF veya endeks: sektör kapsamı yok",
        "unknown_option_type": "opsiyon tipi tanınmadı",
        "sold_flip": "satılan opsiyon: sonuç yöne göre ters çevrildi",
        "oi_pending": "(T+1 bekleniyor)",
        "oi_unconfirmed": "(henüz doğrulanmadı)",
        "oi_expires": "(T+1'den önce vade)",
        "tape_missing": "net prim bandı kaydı yok",
        "tape_stale": "net prim bandı {minutes} dk önce alındı, eski",
        "tape_net": "bu yönde gün içi net prim {amount}",
        "evidence_unread": "kanıt tabloları okunamadı",
    },
)

EVIDENCE_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "chip": "{family}: {state}",
        "chip_note": "{family}: {state} · {note}",
        "chip_paren_note": "{family}: {state} {note}",
        "word": "{supporting} lehte · {against} aleyhte · {unknown} bilinmiyor",
    },
)

STRENGTH_LABELS: Final[Mapping[StrengthKey, str]] = MappingProxyType(
    {"strong": STRONG_LABEL, "moderate": "Orta", "weak": "Zayıf"},
)

# ---------------------------------------------------------------------------
# §9 mapping (branch → state, relative to option type)
# ---------------------------------------------------------------------------

STAGE_BY_FAMILY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "dealer_gamma": "m21_dealer_gamma",
        "dark_pool": "m26_dark_pool",
        "sector": "m25_sector_peer",
        "price_confirmation": "m23_price_confirmation",
    },
)

BRANCH_STATES: Final[Mapping[str, Mapping[str, FamilyState]]] = MappingProxyType(
    {
        # M21 is non-directional: never aleyhte (decision P9).
        "dealer_gamma": MappingProxyType(
            {
                "full_short_and_proximate": "supporting",
                "partial_one_condition": "neutral",
                "no_conditions_met": "neutral",
                "extreme_distance_cutoff": "neutral",
                "no_data": "unknown",
                "timeout": "unknown",
            },
        ),
        "dark_pool": MappingProxyType(
            {
                "confirmed_match": "supporting",
                "direction_mismatch": "against",
                "direction_unclear": "neutral",
                "no_qualifying_prints": "neutral",
                "timeout": "unknown",
                "unknown_option_type": "out_of_scope",
            },
        ),
        # "no_sector" is resolved against alfa_ticker_info in stage_family.
        "sector": MappingProxyType(
            {
                "strong": "supporting",
                "moderate": "supporting",
                "contrarian": "against",
                "weak": "neutral",
                "all_neutral": "neutral",
                "empty_peer_flow": "neutral",
                "timeout": "unknown",
                "unknown_option_type": "out_of_scope",
            },
        ),
        "price_confirmation": MappingProxyType(
            {
                "call_confirmed": "supporting",
                "put_confirmed": "supporting",
                "call_contrarian": "against",
                "put_contrarian": "against",
                "neutral": "neutral",
                "timeout": "unknown",
                "provider_error": "unknown",
                "data_missing_neutral": "unknown",
                "neutral_unknown_type": "out_of_scope",
            },
        ),
    },
)

BRANCH_NOTES: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "dealer_gamma": MappingProxyType({"full_short_and_proximate": "amplifies"}),
        "dark_pool": MappingProxyType({"unknown_option_type": "unknown_option_type"}),
        "sector": MappingProxyType({"unknown_option_type": "unknown_option_type"}),
        "price_confirmation": MappingProxyType({"neutral_unknown_type": "unknown_option_type"}),
    },
)

# B4 T+1 confirmation → state; the Açık pozisyon family is read in the row's direction.
OI_CONFIRM_STATES: Final[Mapping[OIConfirmState, FamilyState]] = MappingProxyType(
    {
        "opening": "supporting",
        "closing": "against",
        "unconfirmed": "unknown",
        "expires_before_t1": "out_of_scope",
    },
)
_OI_NOTES: Final[Mapping[OIConfirmState, str]] = MappingProxyType(
    {"unconfirmed": "oi_unconfirmed", "expires_before_t1": "oi_expires"},
)

_SWAPPED: Final[Mapping[FamilyState, FamilyState]] = MappingProxyType(
    {"supporting": "against", "against": "supporting"},
)
# Families whose stage result is relative to option type (swapped for sold options).
_ORIENTED_FAMILIES: Final = frozenset({"dark_pool", "sector", "price_confirmation"})
_UNCOUNTED_STATES: Final = frozenset({"neutral", "out_of_scope"})
_DIMMED_STATES: Final = frozenset({"unknown", "out_of_scope"})


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageTelemetryView:
    """The board's slice of one ``alfa_stage_telemetry`` row."""

    stage_name: str
    branch: str | None
    degraded: bool
    metadata: Mapping[str, str] | None = None


@dataclass(frozen=True)
class FamilyRead:
    family: str
    state: FamilyState
    note: str | None = None
    source: str | None = None  # stage and branch (or the persisted field) for the audit tooltip
    flipped: bool = False

    @property
    def label(self) -> str:
        return FAMILY_LABELS[self.family]

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def dimmed(self) -> bool:
        """Unknown and out-of-scope chips render dashed and dimmed (R-UN1)."""
        return self.state in _DIMMED_STATES

    @property
    def text(self) -> str:
        if self.note is None:
            return EVIDENCE_COPY["chip"].format(family=self.label, state=self.state_label)
        key = "chip_paren_note" if self.note.startswith("(") else "chip_note"
        return EVIDENCE_COPY[key].format(family=self.label, state=self.state_label, note=self.note)


@dataclass(frozen=True)
class EvidenceCounts:
    supporting: int
    against: int
    unknown: int


@dataclass(frozen=True)
class RowEvidence:
    families: tuple[FamilyRead, ...]  # counted families, in profile order
    counts: EvidenceCounts
    strength: StrengthKey  # already through the R-UN2 guard
    source_event_id: str | None
    legacy: bool
    unread: bool

    @property
    def word(self) -> str:
        return EVIDENCE_COPY["word"].format(
            supporting=self.counts.supporting,
            against=self.counts.against,
            unknown=self.counts.unknown,
        )

    def labels_in(self, state: FamilyState) -> tuple[str, ...]:
        return tuple(f.label for f in self.families if f.state == state)


@dataclass(frozen=True)
class LegacyScores:
    """Persisted scores that map on legacy prints (§9); None when a value is ambiguous."""

    m23_confirmed: float | None
    m23_contrarian: float | None
    m25_supporting_min: float | None
    m25_contrarian: float | None

    @classmethod
    def from_profile(cls, profile: CalibrationProfile) -> LegacyScores:
        """Read the M23 and M25 scores; a value that equals a fallback score never maps."""
        m23 = profile.scoring.modules.m23
        m25 = profile.scoring.modules.m25
        m23_fallbacks = {m23.neutral_score}
        m25_fallbacks = {
            m25.timeout_score, m25.no_sector_score, m25.empty_peer_flow_score, m25.weak_alignment_score,
        }
        return cls(
            m23_confirmed=None if m23.confirmed_score in m23_fallbacks else m23.confirmed_score,
            m23_contrarian=None if m23.contrarian_score in m23_fallbacks else m23.contrarian_score,
            m25_supporting_min=(
                None
                if m25.moderate_alignment_score <= max(m25_fallbacks)
                else m25.moderate_alignment_score
            ),
            m25_contrarian=None if m25.contrarian_score in m25_fallbacks else m25.contrarian_score,
        )


def load_legacy_scores(profile_path: Path) -> LegacyScores:
    """Legacy scores of the calibration profile the live worker scores with (read only)."""
    return LegacyScores.from_profile(load_profile(profile_path))


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _read(
    family: str,
    state: FamilyState,
    note_key: str | None = None,
    *,
    source: str | None = None,
    flipped: bool = False,
    **values: object,
) -> FamilyRead:
    note = NOTE_TEMPLATES[note_key].format(**values) if note_key is not None else None
    return FamilyRead(family=family, state=state, note=note, source=source, flipped=flipped)


def _oriented(
    family: str, state: FamilyState, note_key: str | None, *, sold: bool, source: str | None,
) -> FamilyRead:
    if sold and family in _ORIENTED_FAMILIES and state in _SWAPPED:
        return _read(family, _SWAPPED[state], note_key or "sold_flip", source=source, flipped=True)
    return _read(family, state, note_key, source=source)


def stage_family(
    family: str,
    telemetry: StageTelemetryView | None,
    *,
    sold: bool,
    ticker_info: TickerInfoView | None = None,
) -> FamilyRead:
    """A telemetry family (dealer gamma, dark pool, sector, price confirmation) per §9."""
    stage = STAGE_BY_FAMILY[family]
    if telemetry is None:
        return _read(family, "unknown", "no_stage_row", source=stage)
    source = f"{stage}: {telemetry.branch}" if telemetry.branch else stage
    if telemetry.degraded:
        return _read(family, "unknown", "degraded", source=source)
    branch = telemetry.branch or ""
    if family == "sector" and branch == _NO_SECTOR_BRANCH:
        if ticker_info is None:
            return _read(family, "unknown", "no_sector_unverified", source=source)
        if ticker_info.fund_or_index:
            return _read(family, "out_of_scope", "fund_or_index", source=source)
        return _read(family, "unknown", "no_sector", source=source)
    state = BRANCH_STATES[family].get(branch)
    if state is None:
        return _read(family, "unknown", "unmapped_branch", source=source)
    return _oriented(family, state, BRANCH_NOTES[family].get(branch), sold=sold, source=source)


def legacy_family(
    family: str, signal: StoredSignal | None, *, sold: bool, scores: LegacyScores | None,
) -> FamilyRead:
    """A telemetry family on a print written before telemetry existed (§9 legacy table)."""
    state: FamilyState | None = None
    source: str | None = None
    if signal is not None and scores is not None:
        if family == "price_confirmation":
            value = signal.price_confirmation_score
            source = f"price_confirmation_score={value}"
            if value is not None and value == scores.m23_confirmed:
                state = "supporting"
            elif value is not None and value == scores.m23_contrarian:
                state = "against"
        elif family == "sector":
            value = signal.sector_confirmation_score
            source = f"sector_confirmation_score={value}"
            if value is not None and scores.m25_supporting_min is not None and value >= scores.m25_supporting_min:
                state = "supporting"
            elif value is not None and value == scores.m25_contrarian:
                state = "against"
        elif family == "dark_pool":
            source = f"dark_pool_confirmation={signal.dark_pool_confirmation}"
            if signal.dark_pool_confirmation:
                state = "supporting"
    if state is None:
        return _read(family, "unknown", "legacy", source=source)
    return _oriented(family, state, "legacy_value", sold=sold, source=source)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _signed_usd(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def flow_family(
    tape: TapeSummary | None,
    direction: Direction,
    *,
    deadband_usd: float,
    max_age_seconds: int,
    now: datetime,
) -> FamilyRead:
    """Akış from the net-premium tape, in the row's own direction."""
    if tape is None:
        return _read("flow", "unknown", "tape_missing", source=_TAPE_SOURCE)
    age = (_as_utc(now) - tape.fetched_at).total_seconds()
    if age > max_age_seconds:
        return _read(
            "flow", "unknown", "tape_stale", source=_TAPE_SOURCE,
            minutes=int(age // _SECONDS_PER_MINUTE),
        )
    net = tape.net_premium_for(direction)
    state: FamilyState
    if net > deadband_usd:
        state = "supporting"
    elif -net > deadband_usd:
        state = "against"
    else:
        state = "neutral"
    return _read("flow", state, "tape_net", source=_TAPE_SOURCE, amount=_signed_usd(net))


def open_interest_family(state: OIConfirmState | None) -> FamilyRead:
    """Açık pozisyon from the T+1 confirmation (B4); without one, ``bilinmiyor (T+1 bekleniyor)``."""
    if state is None:
        return _read("open_interest", "unknown", "oi_pending")
    return _read("open_interest", OI_CONFIRM_STATES[state], _OI_NOTES.get(state), source=_OI_SOURCE)


def count_families(families: Iterable[FamilyRead]) -> EvidenceCounts:
    reads = [f for f in families if f.state not in _UNCOUNTED_STATES]
    return EvidenceCounts(
        supporting=sum(1 for f in reads if f.state == "supporting"),
        against=sum(1 for f in reads if f.state == "against"),
        unknown=sum(1 for f in reads if f.state == "unknown"),
    )


def classify_strength(counts: EvidenceCounts, settings: EvidenceSettings) -> StrengthKey:
    if (
        counts.supporting >= settings.strong_min_supporting
        and not counts.against
        and counts.unknown <= settings.max_unknown_for_strong
    ):
        return "strong"
    if counts.supporting >= settings.moderate_min_supporting and counts.against <= counts.supporting:
        return "moderate"
    return "weak"


def guard_strength(strength: StrengthKey, counts: EvidenceCounts, settings: EvidenceSettings) -> StrengthKey:
    """R-UN2 render guard: ``Güçlü`` is refused with more than ``max_unknown_for_strong`` unknowns."""
    if strength != "strong" or counts.unknown <= settings.max_unknown_for_strong:
        return strength
    _logger.error(
        "alfa evidence: refused Güçlü with %d unknown families (max %d)",
        counts.unknown, settings.max_unknown_for_strong,
    )
    if counts.supporting >= settings.moderate_min_supporting and counts.against <= counts.supporting:
        return "moderate"
    return "weak"


def strength_label(strength: StrengthKey, counts: EvidenceCounts, settings: EvidenceSettings) -> str:
    """The label a template may show; always through the R-UN2 guard."""
    return STRENGTH_LABELS[guard_strength(strength, counts, settings)]


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def dominant_print(row: BoardRow) -> RowPrint:
    """The largest-premium print of the row's dominant contract (newest, then id, on ties)."""
    key = row.dominant.key
    members = [
        p for p in row.prints
        if p.expiry == key.expiry and p.strike == key.strike and p.option_type == key.option_type
    ]
    return max(members, key=lambda p: (p.premium, p.timestamp, p.event_id))


def trade_date_et(moment: datetime) -> date:
    return _as_utc(moment).astimezone(_ET).date()


def is_sold(print_: RowPrint, direction: Direction) -> bool:
    """True when the aggressor side reversed the option-type direction."""
    return print_.side_aware and direction != option_type_direction(print_.option_type)


def build_row_evidence(
    row: BoardRow,
    *,
    settings: BoardSettings,
    now: datetime,
    signal: StoredSignal | None,
    telemetry: Mapping[str, StageTelemetryView] | None,
    tape: TapeSummary | None,
    ticker_info: TickerInfoView | None,
    oi_state: OIConfirmState | None = None,
    legacy_scores: LegacyScores | None = None,
    unread: bool = False,
) -> RowEvidence:
    """Every counted family of one row, in ``evidence.counted_families`` order.

    ``telemetry`` holds the source print's stage rows by stage name; ``None`` or
    empty means a legacy print. ``unread`` means the evidence tables could not
    be read: every family is ``bilinmiyor``.
    """
    source = dominant_print(row)
    sold = is_sold(source, row.direction)
    legacy = not telemetry
    reads: list[FamilyRead] = []
    for family in settings.evidence.counted_families:
        if unread:
            reads.append(_read(family, "unknown", "evidence_unread"))
        elif family == "flow":
            reads.append(
                flow_family(
                    tape, row.direction,
                    deadband_usd=settings.evidence.flow_net_premium_deadband_usd,
                    max_age_seconds=settings.tape.max_age_seconds,
                    now=now,
                ),
            )
        elif family == "open_interest":
            reads.append(open_interest_family(oi_state))
        elif telemetry:
            stage_row = telemetry.get(STAGE_BY_FAMILY[family])
            reads.append(stage_family(family, stage_row, sold=sold, ticker_info=ticker_info))
        else:
            reads.append(legacy_family(family, signal, sold=sold, scores=legacy_scores))
    counts = count_families(reads)
    strength = guard_strength(classify_strength(counts, settings.evidence), counts, settings.evidence)
    return RowEvidence(
        families=tuple(reads),
        counts=counts,
        strength=strength,
        source_event_id=source.event_id,
        legacy=legacy and not unread,
        unread=unread,
    )


def evidence_sort_key(counts: EvidenceCounts, total_premium: Decimal) -> tuple[int, int, int, Decimal]:
    """Order inside a section: L desc, A asc, U asc, total premium desc. Never the score (R-EV2)."""
    return (-counts.supporting, counts.against, counts.unknown, -total_premium)


# ---------------------------------------------------------------------------
# Database inputs (render path: reads only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRequest:
    event_id: str
    ticker: str
    trade_date: date


@dataclass(frozen=True)
class EvidenceInputs:
    telemetry: Mapping[str, Mapping[str, StageTelemetryView]]  # event_id → stage_name → row
    tapes: Mapping[tuple[str, date], TapeSummary]
    infos: Mapping[str, TickerInfoView]


EMPTY_INPUTS: Final = EvidenceInputs(telemetry={}, tapes={}, infos={})


def request_for(row: BoardRow) -> EvidenceRequest:
    source = dominant_print(row)
    return EvidenceRequest(
        event_id=source.event_id, ticker=row.ticker.upper(), trade_date=trade_date_et(source.timestamp),
    )


def _metadata(raw: str | None) -> Mapping[str, str] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return MappingProxyType({str(k): str(v) for k, v in parsed.items()})


# The engine whose evidence tables are known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def read_evidence_inputs(engine: Engine, run_id: str, requests: Sequence[EvidenceRequest]) -> EvidenceInputs:
    """Telemetry of the source prints, the tapes of their trading days and ticker info."""
    if not requests:
        return EMPTY_INPUTS
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_telemetry_tables(engine)
        ensure_netprem_tables(engine)
        ensure_ticker_info_tables(engine)
        _tables_ready_for[:] = [engine]
    event_ids = sorted({r.event_id for r in requests})
    telemetry: dict[str, dict[str, StageTelemetryView]] = {}
    with Session(engine) as session:
        rows = session.execute(
            select(
                AlfaStageTelemetry.event_id,
                AlfaStageTelemetry.stage_name,
                AlfaStageTelemetry.branch,
                AlfaStageTelemetry.degraded,
                AlfaStageTelemetry.metadata_json,
            ).where(AlfaStageTelemetry.run_id == run_id, AlfaStageTelemetry.event_id.in_(event_ids)),
        )
        for event_id, stage_name, branch, degraded, metadata_json in rows:
            telemetry.setdefault(event_id, {})[stage_name] = StageTelemetryView(
                stage_name=stage_name,
                branch=branch,
                degraded=bool(degraded),
                metadata=_metadata(metadata_json),
            )
    return EvidenceInputs(
        telemetry=telemetry,
        tapes=read_tape_summaries(engine, {(r.ticker, r.trade_date) for r in requests}),
        infos=read_ticker_infos(engine, {r.ticker for r in requests}),
    )
