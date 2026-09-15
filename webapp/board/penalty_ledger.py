"""Penalty ledger for the Alfa Board audit block (Phase 5.2.A6).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A6.

The ledger answers "why this score" with named defects and named blind spots.
It lists the eight Module 36 penalties (``src/uoa_detector/scoring/penalties.py``)
plus the M24 post-earnings IV adjustment, with frozen Turkish names. Each entry
has one status:

- ``uygulandı``: the name is in the source print's ``penalties_applied``; the
  entry shows the recorded value and reason.
- ``uygulanmadı``: measured on the live path and did not fire. Per the
  contract only ``thin_oi`` is measured live; the entry notes that a print
  without open interest skips it (``penalties.py:77``).
- ``canlı yolda ölçülmüyor``: the input is never set on the live path. Each
  entry carries the contract's evidence:
  - ``iv_rank_high``: ``apply(event)`` is called without iv_rank
    (``orchestrator.py:213``);
  - ``post_gap_move``, ``post_event``, ``isolated_print``,
    ``flow_contradicts_price``: no production setter; only
    ``sources/scenarios.py`` sets them;
  - ``next_day_oi_failed``: the field is not updatable;
  - ``wide_spread``: live prints carry bid = ask = price
    (``flow_poll.py:190-192``); the A2 chip is the honest cost signal.
- ``kaydedilmedi``: the M24 adjustment is folded into the pre-penalty score and
  not persisted separately. When the source print has M24 telemetry, the entry
  adds whether the stage emitted the adjustment on that print.

The statuses describe the live path, and the ledger title says so.

Numbers (deductions and triggers) come from the calibration profile that wrote
the row: the profile whose ``content_hash()`` equals
``SignalRow.profile_content_hash``. Profiles are found by loading every
``profiles/*.yaml`` once per process. An unknown hash shows no numbers; nothing
is guessed.

Every label comes from the frozen dictionaries below (R-WD1). The recorded
``reason`` of an applied penalty is the scoring engine's own text, shown as
data.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.calibration import load_profile

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine

    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration.profile import CalibrationProfile
    from webapp.board.evidence import StageTelemetryView

_logger = logging.getLogger(__name__)

LedgerStatus = Literal["applied", "not_applied", "not_measured_live", "not_recorded"]

M24_KEY: Final = "m24_post_earnings_iv"
M24_STAGE: Final = "m24_iv_exhaustion"
PROFILES_DIR: Final = Path("profiles")
_HASH_DISPLAY_CHARS: Final = 12
_M24_EMITTED: Final = "post_earnings_penalty_emitted"

# The eight Module 36 penalties in engine order, then the M24 adjustment.
PENALTY_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "post_gap_move": "Boşluklu açılış sonrası akış",
        "iv_rank_high": "IV sıralaması yüksek",
        "wide_spread": "Geniş alış-satış makası",
        "thin_oi": "Düşük açık pozisyon",
        "flow_contradicts_price": "Akış fiyat yönüyle çelişiyor",
        "post_event": "Katalizör sonrası akış",
        "isolated_print": "Tekil baskı (tekrar yok)",
        "next_day_oi_failed": "Ertesi gün OI teyidi başarısız",
        M24_KEY: "Kazanç sonrası yüksek IV (M24)",
    },
)

STATUS_LABELS: Final[Mapping[LedgerStatus, str]] = MappingProxyType(
    {
        "applied": "uygulandı",
        "not_applied": "uygulanmadı",
        "not_measured_live": "canlı yolda ölçülmüyor",
        "not_recorded": "kaydedilmedi",
    },
)

# Contract §5 A6: the status of an entry that is not in penalties_applied.
LIVE_STATUS: Final[Mapping[str, LedgerStatus]] = MappingProxyType(
    {
        "post_gap_move": "not_measured_live",
        "iv_rank_high": "not_measured_live",
        "wide_spread": "not_measured_live",
        "thin_oi": "not_applied",
        "flow_contradicts_price": "not_measured_live",
        "post_event": "not_measured_live",
        "isolated_print": "not_measured_live",
        "next_day_oi_failed": "not_measured_live",
        M24_KEY: "not_recorded",
    },
)

_NO_SETTER: Final = "üretimde ayarlayan yok; yalnızca sources/scenarios.py ayarlıyor"

EVIDENCE_NOTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "post_gap_move": _NO_SETTER,
        "iv_rank_high": "apply(event) iv_rank olmadan çağrılıyor (orchestrator.py:213)",
        "wide_spread": (
            "canlı baskılarda bid = ask = fiyat (flow_poll.py:190-192); "
            "gerçek maliyet işlenebilirlik çipinde"
        ),
        "thin_oi": "canlı yolda ölçülür; baskıda OI yoksa atlanır (penalties.py:77)",
        "flow_contradicts_price": _NO_SETTER,
        "post_event": _NO_SETTER,
        "isolated_print": _NO_SETTER,
        "next_day_oi_failed": "alan sonradan güncellenemiyor",
        M24_KEY: "ceza öncesi birleşik skora katılır, ayrı saklanmaz",
    },
)

# The writing profile's numbers per entry (deduction, then trigger).
CONFIG_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "post_gap_move": "ceza {value} · eşik açılış boşluğu > %{gap}",
        "iv_rank_high": "ceza {value} · eşik IV sıralaması > {iv_rank}",
        "wide_spread": "ceza {value} · eşik makas > %{spread}",
        "thin_oi": "ceza {value} · eşik OI < {thin_oi}",
        "flow_contradicts_price": "ceza {value}",
        "post_event": "ceza {value} · katalizörden sonra {sessions} seans",
        "isolated_print": "ceza {value} · {window} dk içinde tekrar yok",
        "next_day_oi_failed": "ceza {value} · ertesi gün OI düşüşü > %{drop}",
        M24_KEY: "ayar {value} · IV sıralaması > {iv_rank} ve {days} seans içinde katalizör",
    },
)

LEDGER_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "title": "Ceza defteri (durumlar canlı yola göre)",
        "profile_found": "Sayılar satırı yazan profilden: {profile_id} (hash {hash})",
        "profile_missing": "Satırı yazan profil bulunamadı (hash {hash}); sayılar gösterilmiyor.",
        "profile_unread": "Satırı yazan profil okunamadı; sayılar gösterilmiyor.",
        "applied": "değer {value} · kayıtlı gerekçe: {reason}",
        "unknown_penalty": "tanınmayan ceza ({name})",
        "m24_emitted": "aşama telemetrisi: bu baskıda ayar üretildi",
        "m24_not_emitted": "aşama telemetrisi: bu baskıda ayar üretilmedi",
        "detail_separator": " · ",
    },
)


@dataclass(frozen=True)
class LedgerEntry:
    key: str
    name: str
    status: LedgerStatus
    details: tuple[str, ...]

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def detail_text(self) -> str:
        return LEDGER_COPY["detail_separator"].join(self.details)


@dataclass(frozen=True)
class PenaltyLedger:
    entries: tuple[LedgerEntry, ...]
    profile_text: str
    applied_names: tuple[str, ...]  # frozen names of the applied entries, in ledger order


def _value(number: float) -> str:
    return f"{number:.2f}"


def _plain(number: float) -> str:
    return f"{number:g}"


def _config_text(key: str, profile: CalibrationProfile) -> str:
    p = profile.penalties
    t = profile.penalty_triggers
    m24 = profile.scoring.modules.m24
    values: dict[str, dict[str, str]] = {
        "post_gap_move": {"value": _value(p.post_gap_move), "gap": _plain(t.gap_threshold_pct)},
        "iv_rank_high": {"value": _value(p.iv_rank_high), "iv_rank": _plain(t.iv_rank_threshold)},
        "wide_spread": {"value": _value(p.wide_spread), "spread": _plain(t.spread_pct_threshold)},
        "thin_oi": {"value": _value(p.thin_oi), "thin_oi": _plain(t.thin_oi_threshold)},
        "flow_contradicts_price": {"value": _value(p.flow_contradicts_price)},
        "post_event": {"value": _value(p.post_event), "sessions": _plain(t.post_event_sessions)},
        "isolated_print": {"value": _value(p.isolated_print), "window": _plain(t.isolated_window_min)},
        "next_day_oi_failed": {"value": _value(p.next_day_oi_failed), "drop": _plain(t.next_day_oi_drop_pct)},
        M24_KEY: {
            "value": _value(m24.post_earnings_iv_penalty),
            "iv_rank": _plain(m24.post_earnings_iv_penalty_threshold),
            "days": _plain(m24.post_earnings_session_days),
        },
    }
    return CONFIG_TEMPLATES[key].format(**values[key])


def _m24_note(telemetry: StageTelemetryView | None) -> str | None:
    if telemetry is None or telemetry.degraded or not telemetry.metadata:
        return None
    emitted = telemetry.metadata.get(_M24_EMITTED)
    if emitted == "yes":
        return LEDGER_COPY["m24_emitted"]
    if emitted == "no":
        return LEDGER_COPY["m24_not_emitted"]
    return None


def _profile_text(profile_hash: str | None, profile: CalibrationProfile | None) -> str:
    if not profile_hash:
        return LEDGER_COPY["profile_unread"]
    short = profile_hash[:_HASH_DISPLAY_CHARS]
    if profile is None:
        return LEDGER_COPY["profile_missing"].format(hash=short)
    return LEDGER_COPY["profile_found"].format(profile_id=profile.profile_id, hash=short)


def build_penalty_ledger(
    signal: StoredSignal | None,
    *,
    profile_hash: str | None,
    profile: CalibrationProfile | None,
    m24_telemetry: StageTelemetryView | None = None,
) -> PenaltyLedger:
    """The nine ledger entries for a row's source print, plus any applied name the ledger does not know."""
    applied = {p.name: p for p in (signal.penalties_applied if signal is not None else [])}
    entries: list[LedgerEntry] = []
    for key, name in PENALTY_NAMES.items():
        penalty = applied.get(key)
        details: list[str] = []
        status: LedgerStatus
        if penalty is not None:
            status = "applied"
            details.append(LEDGER_COPY["applied"].format(value=_value(penalty.value), reason=penalty.reason))
        else:
            status = LIVE_STATUS[key]
            details.append(EVIDENCE_NOTES[key])
            note = _m24_note(m24_telemetry) if key == M24_KEY else None
            if note is not None:
                details.append(note)
        if profile is not None:
            details.append(_config_text(key, profile))
        entries.append(LedgerEntry(key=key, name=name, status=status, details=tuple(details)))
    for key, penalty in applied.items():
        if key in PENALTY_NAMES:
            continue
        entries.append(
            LedgerEntry(
                key=key,
                name=LEDGER_COPY["unknown_penalty"].format(name=key),
                status="applied",
                details=(LEDGER_COPY["applied"].format(value=_value(penalty.value), reason=penalty.reason),),
            ),
        )
    return PenaltyLedger(
        entries=tuple(entries),
        profile_text=_profile_text(profile_hash, profile),
        applied_names=tuple(e.name for e in entries if e.status == "applied"),
    )


# ---------------------------------------------------------------------------
# The profile that wrote a row
# ---------------------------------------------------------------------------

_profiles_lock = threading.Lock()
_profiles_by_dir: dict[Path, Mapping[str, CalibrationProfile]] = {}


def profiles_by_hash(profiles_dir: Path = PROFILES_DIR) -> Mapping[str, CalibrationProfile]:
    """Every loadable calibration profile in ``profiles_dir``, by content hash (loaded once)."""
    key = profiles_dir.resolve()
    with _profiles_lock:
        cached = _profiles_by_dir.get(key)
        if cached is None:
            found: dict[str, CalibrationProfile] = {}
            for path in sorted(profiles_dir.glob("*.yaml")):
                try:
                    profile = load_profile(path, profiles_dir=profiles_dir)
                except Exception as exc:  # not a calibration profile (e.g. board_v1.yaml)
                    _logger.debug("penalty ledger: %s is not a loadable calibration profile (%s)", path, exc)
                    continue
                found.setdefault(profile.content_hash(), profile)
            cached = MappingProxyType(found)
            _profiles_by_dir[key] = cached
        return cached


def resolve_profile(content_hash: str, profiles_dir: Path = PROFILES_DIR) -> CalibrationProfile | None:
    """The calibration profile with ``content_hash``, or None when no profile file matches."""
    return profiles_by_hash(profiles_dir).get(content_hash)


def read_profile_hashes(engine: Engine, run_id: str, event_ids: Sequence[str]) -> dict[str, str]:
    """``SignalRow.profile_content_hash`` per event of ``run_id`` (reads the database only)."""
    wanted = sorted(set(event_ids))
    if not wanted:
        return {}
    with Session(engine) as session:
        rows = session.execute(
            select(SignalRow.event_id, SignalRow.profile_content_hash).where(
                SignalRow.run_id == run_id, SignalRow.event_id.in_(wanted),
            ),
        )
        return {event_id: content_hash for event_id, content_hash in rows}
