"""Small stored daily-pilot projection; GET never evaluates or exports sources."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from webapp.news_research import config

DISPLAY_LIMIT = 10
ARM_LABELS = {
    "A": "Akış + fiyat",
    "B": "Akış + fiyat + göreli güç",
    "C": "Akış + fiyat + haber",
    "D": "Göreli güç VE haber",
    "E": "Göreli güç VEYA haber",
}
REASON_LABELS = {
    "entry_missing_observed_price": "Giriş fiyatı zamanında gözlenmedi",
    "exit_missing_observed_price": "Çıkış fiyatı zamanında gözlenmedi",
    "entry_missing_exact_time_spy": "Girişte aynı zamanlı SPY fiyatı yok",
    "exit_missing_exact_time_spy": "Çıkışta aynı zamanlı SPY fiyatı yok",
    "legacy_unknown_roster": "İlk fırsatta yeni kaynak kaydı yok",
    "price_context_unavailable": "Fiyat bağlamı eksik",
    "price_source_session_or_freshness_unavailable": "Fiyatın seansı veya tazeliği doğrulanamadı",
    "relative_strength_unavailable": "Göreli güç ölçülemedi",
    "news_check_unavailable": "Haber kontrolü doğrulanamadı",
    "source_limit_exceeded": "Kaynak sınırı aşıldı; kısmi sonuç üretilmedi",
    "source_export_refused": "Kaynak bütünlüğü doğrulanamadı",
    "export_busy": "Başka aktarım sürüyor",
    "physical_context_unavailable": "Fiziksel kaynak kaydı yok",
    "scan_storage_unavailable": "Kaynak kayıtları hazır değil",
    "report_unverified": "Rapor kimliği veya sayıları doğrulanamadı",
}


@dataclass(frozen=True)
class Missing:
    code: str
    label: str
    count: int


@dataclass(frozen=True)
class Arm:
    key: str
    label: str
    selected: int
    ineligible: int
    unknown: int
    priced: int
    complete_days: int
    incomplete_days: int
    net: float | None
    excess: float | None


@dataclass(frozen=True)
class Cost:
    bps: float
    complete_common_sessions: int
    arms: tuple[Arm, ...]


@dataclass(frozen=True)
class Row:
    day: date
    status: str
    status_label: str
    recorded_at: datetime
    reason: str
    key: str
    source_identity: str | None = None
    source_scans: int | None = None
    opportunity_rows: int | None = None
    roster_slots: int | None = None
    complete_common_sessions: int | None = None
    eb_paired_days: int | None = None
    unit_missing: tuple[Missing, ...] = ()
    audit_missing: tuple[Missing, ...] = ()
    arms: tuple[Arm, ...] = ()
    stresses: tuple[Cost, ...] = ()


@dataclass(frozen=True)
class Report:
    as_of: datetime
    status: str
    reason: str
    profile_hash: str
    implementation_hash: str
    rows: tuple[Row, ...] = ()
    limited: bool = False
    ignored_versions: int = 0
    invalid_records: int = 0
    completed_days: int = 0
    source_scans: int | None = None
    opportunity_rows: int | None = None
    roster_slots: int | None = None
    complete_common_sessions: int | None = None
    eb_paired_days: int | None = None
    minimum_common_sessions: int = 80
    base_cost_bps: float = 5
    inference: str = "NOT_PERFORMED"
    winner: None = None


def _read(engine: Engine, now: datetime, limit: int) -> list[dict[str, Any]]:
    # Import lazily: absent optional storage is unavailable, never an empty study.
    from webapp.news_pilot import storage

    if not storage.available(engine):
        raise ValueError("pilot storage unavailable")
    return storage.read(engine, now, limit=limit)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid count")
    return value


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("invalid number")
    return float(value)


def _clock(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing clock")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("naive clock")
    return at.astimezone(UTC)


def _missing(value: object) -> tuple[Missing, ...]:
    if not isinstance(value, dict):
        raise ValueError("invalid missing counts")
    result = []
    for code, count in sorted(value.items()):
        if not isinstance(code, str):
            raise ValueError("invalid reason")
        result.append(
            Missing(
                code, REASON_LABELS.get(code, "Diğer doğrulanamayan kaynak koşulu"), _count(count)
            )
        )
    return tuple(result)


def _arms(value: object) -> tuple[Arm, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("invalid arm list")
    found = {}
    for raw in value:
        if is_dataclass(raw) and not isinstance(raw, type):
            raw = asdict(raw)
        if not isinstance(raw, dict) or raw.get("arm") not in ARM_LABELS or raw["arm"] in found:
            raise ValueError("invalid arm identity")
        key = raw["arm"]
        found[key] = Arm(
            key,
            ARM_LABELS[key],
            _count(raw.get("selected")),
            _count(raw.get("ineligible")),
            _count(raw.get("unknown")),
            _count(raw.get("priced")),
            _count(raw.get("complete_days")),
            _count(raw.get("incomplete_days")),
            _number(raw.get("net_complete_days")),
            _number(raw.get("excess_complete_days")),
        )
    if set(found) != set(ARM_LABELS):
        raise ValueError("missing arm")
    return tuple(found[k] for k in ARM_LABELS)


def _row(raw: dict[str, Any], day: date, at: datetime) -> Row:
    at = at.astimezone(ZoneInfo("Europe/London"))
    state = raw.get("status")
    key = raw.get("key")
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("invalid record identity")
    if state in ("pending", "failed"):
        return Row(
            day,
            state,
            "Hesap bekliyor" if state == "pending" else "Hesap tamamlanamadı",
            at,
            REASON_LABELS.get(
                str(raw.get("reason")),
                "Sonuç henüz ölçülmedi"
                if state == "pending"
                else "Gün tamamlanamadı; getiri bilinmiyor",
            ),
            key,
        )
    if state != "completed" or not isinstance(raw.get("summary"), dict):
        raise ValueError("invalid status")
    summary = raw["summary"]
    if (
        summary.get("day") != day.isoformat()
        or summary.get("profile_hash") != raw["profile_hash"]
        or summary.get("implementation_hash") != raw["implementation_hash"]
        or summary.get("inference") != "NOT_PERFORMED"
        or summary.get("winner") is not None
    ):
        raise ValueError("unverified summary")
    identity = summary.get("source_identity")
    if (
        not isinstance(identity, str)
        or len(identity) != 64
        or any(c not in "0123456789abcdef" for c in identity)
    ):
        raise ValueError("invalid source identity")
    common = _count(summary.get("complete_common_sessions"))
    costs = []
    for cost in summary.get("stresses", []):
        if not isinstance(cost, dict):
            raise ValueError("invalid cost summary")
        bps = _number(cost.get("slippage_bps_per_side", cost.get("cost_bps")))
        if bps is None or bps < 0:
            raise ValueError("invalid cost")
        costs.append(
            Cost(bps, _count(cost.get("complete_common_sessions")), _arms(cost.get("arms")))
        )
    return Row(
        day,
        "completed",
        "Ölçüm eksik" if common == 0 else "Betimsel hesap kayıtlı",
        at,
        "Tam ortak seans yok; karşılaştırmalı getiri bilinmiyor."
        if common == 0
        else "Kayıtlı sonuç; kalibre edilmiş üstünlük kanıtı değildir.",
        key,
        identity,
        _count(summary.get("source_scans")),
        _count(summary.get("opportunity_rows")),
        _count(summary.get("roster_slots")),
        common,
        _count(summary.get("eb_paired_days")),
        _missing(summary.get("unit_missing")),
        _missing(summary.get("audit_missing")),
        _arms(summary.get("arms")),
        tuple(costs),
    )


def build(engine: Engine, now: datetime) -> Report:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware pilot view clock required")
    now = now.astimezone(UTC)
    profile, implementation = config.profile_hash(), config.implementation_hash()
    base = (
        now,
        "STORAGE_UNAVAILABLE",
        "Günlük deney kayıtları henüz okunamıyor; sonuç bilinmiyor.",
        profile,
        implementation,
    )
    try:
        records = _read(engine, now, DISPLAY_LIMIT + 1)
    except Exception:
        return Report(*base)
    limited = len(records) > DISPLAY_LIMIT
    ignored = invalid = 0
    current: dict[date, Row] = {}
    for raw in records[:DISPLAY_LIMIT]:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        if raw.get("profile_hash") != profile or raw.get("implementation_hash") != implementation:
            ignored += 1
            continue
        try:
            day = date.fromisoformat(raw["day"])
            at = _clock(raw.get("recorded_at"))
            if at > now:
                raise ValueError("future record")
            row = _row(raw, day, at)
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if day not in current or current[day].recorded_at < at:
            current[day] = row
    rows = tuple(sorted(current.values(), key=lambda r: (r.day, r.recorded_at), reverse=True))
    completed = tuple(r for r in rows if r.status == "completed")

    def total(name: str) -> int | None:
        return sum(int(getattr(r, name)) for r in completed) if completed else None

    return Report(
        now,
        "LIMITED" if limited else "AVAILABLE" if rows else "EMPTY",
        "Yalnız görünen tamamlanmış günlerin sayımları; eksik fiyatlar getiri değildir."
        if rows
        else "Henüz kayıtlı günlük deney yok; getiri bilinmiyor.",
        profile,
        implementation,
        rows,
        limited,
        ignored,
        invalid,
        len(completed),
        total("source_scans"),
        total("opportunity_rows"),
        total("roster_slots"),
        total("complete_common_sessions"),
        total("eb_paired_days"),
        config.settings().minimum_common_sessions,
        config.settings().slippage_bps_per_side,
    )
