"""Bounded, SELECT-only daily accountability for distinct recorded methods.

Timed references, legacy gross c0 marks and model-cost PAPER closures retain
separate populations. This projection never creates a target or a price.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, inspect, select
from sqlalchemy.orm import defer

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.db import digest
from webapp.board.db import session_factory
from webapp.evidence_reads import scoped_read
from webapp.live_alpha.results import paper_result
from webapp.live_alpha.store import LivePaper, LiveScan
from webapp.opening import storage as opening_storage
from webapp.opening.model import Advice
from webapp.shared_news import config as shared_config
from webapp.shared_news import storage as shared_storage

from .config import enabled, settings
from .model import Activation, Assessment, Completion, LegEvidence, Target, aware, stored

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

METHOD = "timed_measurement_v1"
LONDON = ZoneInfo("Europe/London")
READ_LIMIT = 200


@dataclass(frozen=True)
class Coverage:
    first_calls: int | None = None
    eligible: int | None = None
    not_applicable: int | None = None
    due: int | None = None
    pending: int | None = None
    mature: int | None = None
    own_observed: int | None = None
    paired: int | None = None
    entry_spy_missing: int | None = None
    exit_spy_missing: int | None = None
    unknown: int | None = None
    rejected: int | None = None
    missing: int | None = None
    own_coverage_pct: float | None = None
    paired_coverage_pct: float | None = None


@dataclass(frozen=True)
class TargetRow:
    key: str
    call_key: str
    ticker: str
    instrument: str
    call_at: datetime
    horizon: str
    horizon_minutes: int
    target_at: datetime
    deadline: datetime
    state: str
    state_label: str
    reasons: tuple[str, ...]
    eligible: bool
    mature: bool
    entry_spy_missing: bool
    exit_spy_missing: bool
    own_source_at: datetime | None = None
    own_received_at: datetime | None = None
    own_confirmed_at: datetime | None = None
    spy_source_at: datetime | None = None
    spy_received_at: datetime | None = None
    spy_confirmed_at: datetime | None = None
    own_pct: float | None = None
    excess_pp: float | None = None
    own_source_age_seconds: float | None = None
    own_recording_lag_seconds: float | None = None
    spy_source_age_seconds: float | None = None
    spy_recording_lag_seconds: float | None = None
    exit_skew_seconds: float | None = None
    rejected_legs: int = 0


@dataclass(frozen=True)
class HorizonSummary:
    horizon: str
    horizon_minutes: int
    eligible: int
    mature: int
    own_observed: int
    paired: int
    mean_pct: float | None
    mean_excess_pp: float | None
    own_coverage_pct: float | None
    paired_coverage_pct: float | None


@dataclass(frozen=True)
class LegacyGroup:
    policy: str
    instrument: str
    first_calls: int
    own_observed: int
    paired: int
    mean_pct: float | None
    mean_excess_pp: float | None
    limited: bool
    method: str = "legacy_gross_c0"


@dataclass(frozen=True)
class PaperGroup:
    policy: str
    instrument: str
    closed: int
    unfilled: int
    unknown: int
    pnl_usd: float | None
    mean_usd: float | None
    limited: bool
    method: str = "model_cost_paper"


@dataclass(frozen=True)
class FeedStatus:
    provider: str
    status: str
    reason: str
    last_attempt_at: datetime | None = None
    last_received_at: datetime | None = None
    last_confirmed_at: datetime | None = None


@dataclass(frozen=True)
class Arm:
    key: str
    label: str


@dataclass(frozen=True)
class UWReadiness:
    status: str
    reason: str
    profile_hash: str
    packet_count: int | None
    receipt_count: int | None
    sampled_scans: int
    context_scans: int
    limited: bool
    prospective_sessions: int
    minimum_common_sessions: int
    arms: tuple[Arm, ...]
    inference: str = "NOT_PERFORMED"


@dataclass(frozen=True)
class DailyReport:
    day: date
    as_of: datetime
    status: str
    reason: str
    activation_at: datetime | None
    method: str
    first_live_observation_pending: bool | None
    limited: bool
    summary: Coverage
    rows: tuple[TargetRow, ...]
    horizons: tuple[HorizonSummary, ...]
    legacy: tuple[LegacyGroup, ...]
    paper: tuple[PaperGroup, ...]
    news_feeds: tuple[FeedStatus, ...]
    news_research: UWReadiness
    day_basis: str = "İlk çağrının ABD seans günü; tüm saatler Europe/London."
    paper_day_basis: str = "PAPER kapanışlarının Europe/London takvim günü."
    read_limit: int = READ_LIMIT


@dataclass(frozen=True)
class CallReport:
    key: str
    status: str
    reason: str
    activation_at: datetime | None
    method: str
    first: bool | None
    canonical_key: str | None
    rows: tuple[TargetRow, ...]
    limited: bool = False


REASONS = {
    "auth": "Kaynak erişimi reddedildi; günün yeni ölçüm istekleri durduruldu.",
    "daily_quota": "Kaynak günlük istek sınırı bildirdi; günün yeni ölçüm istekleri durduruldu.",
    "unknown_provider_outcome": "Kaynak yanıtının sonucu doğrulanamadı; fiyat bilinmiyor.",
    "transport": "Kaynak bağlantısı tamamlanamadı; fiyat bilinmiyor.",
    "timeout": "Kaynak isteği süre sınırında tamamlanmadı.",
    "not_found": "Kaynak istenen hisse için yanıt bulamadı.",
    "invalid_payload": "Kaynak yanıtı beklenen veri biçiminde değil.",
    "body_limit": "Kaynak yanıtı sabit boyut sınırını aştı; kullanılmadı.",
    "circuit_open": "Kaynak erişim koruması yeni isteğe izin vermedi.",
    "late": "Fiziksel kayıt onayı yakalama süresine yetişmedi.",
    "cooldown": "Kaynak hız sınırından sonra sabit bekleme uygulanıyor.",
    "bootstrap_hold": "Güvenli başlangıç kontrolü tamamlanmadan istek gönderilmedi.",
    "slot_conflict": "Kalıcı istek sahipliği doğrulanamadı; ek istek gönderilmedi.",
    "claim_unverified": "Denemenin kalıcı sahipliği doğrulanamadı; istek gönderilmedi.",
    "leg_binding_invalid": "Hisse veya deneme kimliği hedefle eşleşmedi; istek gönderilmedi.",
    "already_reserved": "Bu deneme ayağı daha önce sahiplenildi; tekrar gönderilmedi.",
    "missing_entry_reference": "Özgün planın dayanak giriş referansı doğrulanamadı; hedef uygulanamaz.",
    "modulecap": "Bu ölçüm akışının günlük istek sınırına ulaşıldı.",
    "sharedcap": "Ortak günlük istek sınırı izin vermedi.",
    "quota_unknown": "Kaynak kota yanıtı doğrulanamadı; günün yeni ölçüm istekleri durduruldu.",
    "paired_reference": "Aynı denemeden uygun dayanak/SPY çifti kaydedildi.",
    "window_expired": "Yakalama penceresi sona erdi.",
    "attempts_exhausted": "Sabit üç deneme tamamlandı.",
    "incomplete_attempt": "Sahiplenilmiş denemenin tamamlanma kaydı yok; tekrar gönderilmez.",
    "clock_error": "Fiziksel saat sırası doğrulanamadı; sonuç bilinmiyor.",
    "day_halted": "Günlük kaynak erişimi veya kota durumu yeni isteklere izin vermedi.",
    "not_applicable": "Hedef normal seans koşullarında uygulanamaz.",
    "invalid_attempt": "Denemenin kalıcı kimliği veya saat sırası doğrulanamadı.",
    "nonfinite_return": "Sonuç sonlu bir sayı olarak doğrulanamadı.",
    "quote_envelope": "Kaynak yanıtının beklenen alanları yok.",
    "last_trade_missing": "Yanıtta son işlem kaydı yok.",
    "last_trade_price": "Son işlem fiyatı doğrulanamadı.",
    "last_trade_time": "Son işlem zamanı doğrulanamadı.",
    "entry_spy_missing": "Özgün giriş anının SPY referansı yok; sonraki fiyatla tamamlanmaz.",
    "missing_entry_spy": "Özgün giriş anının SPY referansı yok; sonraki fiyatla tamamlanmaz.",
    "exit_spy_missing": "Bu denemede uygun SPY çıkış gözlemi yok.",
    "missing_exit_spy": "Bu denemede uygun SPY çıkış gözlemi yok.",
    "missing_own": "Uygun dayanak çıkış gözlemi yok.",
    "no_own_observation": "Uygun dayanak çıkış gözlemi yok.",
    "no_qualifying_pair": "Aynı denemeden uygun dayanak/SPY çifti oluşmadı.",
    "late_discovery": "Hedef, yakalama süresi bittikten sonra keşfedildi.",
    "discovered_late": "Hedef, yakalama süresi bittikten sonra keşfedildi.",
    "outside_session": "Hedef normal seansın içinde değil; uygulanamaz.",
    "outside_regular_session": "Hedef normal seansın içinde değil; uygulanamaz.",
    "unknown_calendar": "Seans takvimi doğrulanamadı; uygulanabilirlik bilinmiyor.",
    "calendar_unknown": "Seans takvimi doğrulanamadı; uygulanabilirlik bilinmiyor.",
    "invalid_source": "Kaynak gözlemi kalite koşullarını sağlamadı.",
    "request_receipt_clock": "İstek ve fiziksel yanıt saatleri koşulları sağlamadı.",
    "last_trade_outside_window": "Son işlem hedef/seans/tazelik penceresinin dışında.",
    "ticker_mismatch": "Yanıttaki hisse kodu istekle eşleşmiyor.",
    "not_regular_phase": "Yanıt normal seans işlemi olarak doğrulanamadı.",
    "auth_refused": "Kaynak erişimi reddedildi; yeni istekler durduruldu.",
    "quota_refused": "Günlük istek kotası izin vermedi.",
    "module_cap": "Bu ölçüm akışının günlük istek sınırına ulaşıldı.",
    "shared_cap": "Ortak günlük istek sınırı izin vermedi.",
    "rate_limited": "Kaynak hız sınırı bildirdi; bekleme uygulanır.",
    "transport_error": "Kaynak isteği tamamlanamadı; fiyat bilinmiyor.",
    "recording_unavailable": "Fiziksel kayıt onayı alınamadı; gözlem kullanılamaz.",
    "claim_incomplete": "Sahiplenilmiş denemenin tamamlanma kaydı yok; tekrar gönderilmez.",
    "attempt_incomplete": "Sahiplenilmiş denemenin tamamlanma kaydı yok; tekrar gönderilmez.",
    "window_closed": "Yakalama penceresi sona erdi.",
    "deadline": "Yakalama penceresi sona erdi.",
    "exhausted": "Sabit üç deneme tamamlandı.",
    "complete": "Aynı denemeden uygun dayanak/SPY çifti kaydedildi.",
    "partial": "Yalnız uygun dayanak referansı var; eşleşmiş SPY sonucu yok.",
}


def _reason(value: str) -> str:
    return REASONS.get(value, "Kayıtlı tamamlanma veya kaynak koşulu doğrulanamadı.")


def _number(value: object, *, positive: bool = False) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and (not positive or number > 0) else None


def _mean(values: list[float]) -> float | None:
    return math.fsum(v / len(values) for v in values) if values else None


def _sum(values: list[float]) -> float | None:
    if not values:
        return None
    try:
        result = math.fsum(values)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _ratio(value: int, denominator: int) -> float | None:
    return 100 * value / denominator if denominator else None


def _object(raw: str) -> dict[str, object]:
    parsed: object = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        return None


def _lag(later: datetime | None, earlier: datetime | None, now: datetime) -> float | None:
    return (later - earlier).total_seconds() if later is not None and earlier is not None and earlier <= later <= now else None


def _row(target: Target, assessment: Assessment | None, legs: tuple[LegEvidence, ...], now: datetime,
         completions: tuple[Completion, ...] = ()) -> TargetRow:
    from .collector import eligible

    if assessment is not None and assessment.assessed_at > now:
        assessment = None
    own = next((leg for leg in legs if assessment and leg.key == assessment.own_key and leg.leg == "own"), None)
    spy = next((leg for leg in legs if assessment and leg.key == assessment.spy_key and leg.leg == "spy"), None)
    own_completion = next((c for c in completions if own and c.target_key == target.key and c.own_key == own.key
                           and c.completed_at <= c.confirmed_at <= now), None)
    spy_completion = next((c for c in completions if spy and c.target_key == target.key and c.spy_key == spy.key
                           and c.completed_at <= c.confirmed_at <= now), None)
    own_valid = bool(own_completion and eligible(target, own, own_completion, now, leg="own"))
    spy_valid = bool(spy_completion and eligible(target, spy, spy_completion, now, leg="spy"))
    mature = bool(target.eligible and (target.deadline <= now or assessment is not None))
    own_pct = _number(assessment.return_pct) if assessment and assessment.status in {"observed", "partial"} and own_valid else None
    excess = _number(assessment.excess_pp) if assessment and assessment.status == "observed" and own_valid and spy_valid else None
    entry_missing = _number(target.spy_entry, positive=True) is None
    same_attempt = bool(own and spy and own.attempt_key == spy.attempt_key)
    if not same_attempt:
        excess = None
    skew = abs((own.trade_at - spy.trade_at).total_seconds()) if own_valid and spy_valid and own and spy and own.trade_at and spy.trade_at else None
    if entry_missing or skew is None or skew > settings().pair_skew_seconds:
        excess = None
    if _number(target.entry_reference, positive=True) is None:
        own_pct = excess = None
    if own_pct is None:
        excess = None
    exit_missing = bool(mature and own_pct is not None and not (spy_valid and same_attempt and skew is not None and skew <= settings().pair_skew_seconds))
    reasons: list[str] = []
    if target.reason:
        reasons.append(_reason(target.reason))
    if assessment and assessment.reason:
        reasons.append(_reason(assessment.reason))
    rejected = [leg for leg in legs if leg.status != "ok"]
    for leg in rejected:
        reasons.append(("Dayanak: " if leg.leg == "own" else "SPY: ") + _reason(leg.reason or leg.status))
    if not target.eligible:
        state, label = "not_applicable", "Uygulanamaz"
    elif assessment is not None and assessment.status == "unknown":
        state, label = "unknown", "Kesin sonuç doğrulanamadı"
    elif assessment is not None and own_pct is not None:
        state, label = ("complete", "Dayanak + SPY eşleşti") if excess is not None else ("partial", "Yalnız dayanak gözlendi")
    elif assessment is not None and assessment.status == "missing":
        state, label = "missing", "Nitelikli sonuç yok"
    elif assessment is not None:
        state, label = "unknown", "Fiziksel sonuç eşleşmesi doğrulanamadı"
    elif now < target.target_at:
        state, label = "pending", "Hedef saati bekleniyor"
    elif now < target.deadline:
        state, label = "due", "Yakalama penceresi açık"
    else:
        state, label = "assessment_pending", "Pencere kapandı; kesin kayıt bekleniyor"
    if entry_missing:
        reasons.append(REASONS["entry_spy_missing"])
    if exit_missing:
        reasons.append(REASONS["exit_spy_missing"])
    if assessment is not None and assessment.return_pct is not None and own_pct is None:
        reasons.append("Sonucun fiziksel kaynak/saat eşleşmesi doğrulanamadı; sayı gösterilmedi.")
    # Rejected/pending attempts may expose their recorded diagnostic clocks.
    # They never replace the assessment's exact packet keys for outcome admission.
    shown_own = own or next((leg for leg in legs if leg.leg == "own"), None)
    shown_spy = spy or next((leg for leg in legs if leg.leg == "spy"), None)
    shown_own_completion = next((c for c in completions if shown_own and c.own_key == shown_own.key and c.target_key == target.key), None)
    shown_spy_completion = next((c for c in completions if shown_spy and c.spy_key == shown_spy.key and c.target_key == target.key), None)
    shown_own_confirmed = max(shown_own.confirmed_at, shown_own_completion.own_confirmed_at) if shown_own and shown_own_completion and shown_own_completion.own_confirmed_at else None
    shown_spy_confirmed = max(shown_spy.confirmed_at, shown_spy_completion.spy_confirmed_at) if shown_spy and shown_spy_completion and shown_spy_completion.spy_confirmed_at else None
    if shown_own_confirmed is not None and shown_own_confirmed > now:
        shown_own_confirmed = None
    if shown_spy_confirmed is not None and shown_spy_confirmed > now:
        shown_spy_confirmed = None
    return TargetRow(
        key=target.key, call_key=target.call_key, ticker=target.ticker, instrument=target.instrument, call_at=target.call_at,
        horizon=target.horizon, horizon_minutes=target.horizon_minutes, target_at=target.target_at, deadline=target.deadline,
        state=state, state_label=label, reasons=tuple(dict.fromkeys(reasons)), eligible=target.eligible,
        mature=mature, entry_spy_missing=entry_missing, exit_spy_missing=exit_missing,
        own_source_at=shown_own.trade_at if shown_own else None, own_received_at=shown_own.received_at if shown_own else None, own_confirmed_at=shown_own_confirmed,
        spy_source_at=shown_spy.trade_at if shown_spy else None, spy_received_at=shown_spy.received_at if shown_spy else None, spy_confirmed_at=shown_spy_confirmed,
        own_pct=own_pct, excess_pp=excess,
        own_source_age_seconds=_lag(shown_own_confirmed, shown_own.trade_at if shown_own else None, now),
        own_recording_lag_seconds=_lag(shown_own_confirmed, shown_own.received_at if shown_own else None, now),
        spy_source_age_seconds=_lag(shown_spy_confirmed, shown_spy.trade_at if shown_spy else None, now),
        spy_recording_lag_seconds=_lag(shown_spy_confirmed, shown_spy.received_at if shown_spy else None, now),
        exit_skew_seconds=skew, rejected_legs=len(rejected),
    )


def _coverage(rows: tuple[TargetRow, ...], first_calls: int | None) -> Coverage:
    eligible = [r for r in rows if r.eligible]
    mature = [r for r in eligible if r.mature]
    observed = sum(r.own_pct is not None for r in mature)
    paired = sum(r.excess_pp is not None for r in mature)
    return Coverage(first_calls=first_calls, eligible=len(eligible), not_applicable=sum(not r.eligible for r in rows),
                    due=sum(r.state == "due" for r in eligible), pending=sum(r.state == "pending" for r in eligible),
                    mature=len(mature), own_observed=observed, paired=paired,
                    entry_spy_missing=sum(r.entry_spy_missing for r in eligible), exit_spy_missing=sum(r.exit_spy_missing for r in mature),
                    unknown=sum(r.state in {"unknown", "assessment_pending"} for r in eligible),
                    rejected=sum(r.rejected_legs > 0 for r in eligible), missing=sum(r.state == "missing" for r in mature),
                    own_coverage_pct=_ratio(observed, len(mature)), paired_coverage_pct=_ratio(paired, len(mature)))


def _horizons(rows: tuple[TargetRow, ...]) -> tuple[HorizonSummary, ...]:
    output = []
    for minutes in (15, 60):
        selected = tuple(r for r in rows if r.horizon_minutes == minutes)
        counts = _coverage(selected, None)
        own = [r.own_pct for r in selected if r.mature and r.own_pct is not None]
        paired = [r.excess_pp for r in selected if r.mature and r.excess_pp is not None]
        output.append(HorizonSummary(f"m{minutes}", minutes, counts.eligible or 0, counts.mature or 0,
                                     len(own), len(paired), _mean(own), _mean(paired),
                                     counts.own_coverage_pct, counts.paired_coverage_pct))
    return tuple(output)


def _legacy(engine: Engine, now: datetime, day: date, names: set[str]) -> tuple[tuple[LegacyGroup, ...], int | None, bool]:
    required = (opening_storage.OpeningPrimary, opening_storage.OpeningDecision, opening_storage.OpeningOutcome, opening_storage.OpeningCycle)
    if any(t.__tablename__ not in names for t in required):
        return (), None, False
    primary, decision, outcome, cycle = required
    with session_factory(engine)() as session:
        conditions = (primary.kind == "entry", primary.day == day, primary.published_at <= now)
        total = session.scalar(select(func.count()).select_from(primary).where(*conditions)) or 0
        selected = session.execute(select(primary, decision, outcome, cycle)
                    .outerjoin(decision, decision.key == primary.decision_key)
                    .outerjoin(outcome, (outcome.primary_key == primary.key) & (outcome.horizon == "c0") & (outcome.observed_at <= now))
                    .outerjoin(cycle, cycle.key == primary.cycle_key)
                    .where(*conditions).order_by(primary.published_at, primary.key).limit(READ_LIMIT)).all()
    limited = total > READ_LIMIT
    grouped: dict[tuple[str, str], list[tuple[float | None, float | None]]] = defaultdict(list)
    seen: set[str] = set()
    for first, saved, mark, original_cycle in selected:
        if first.key in seen:
            limited = True
            continue
        seen.add(first.key)
        policy, instrument = "bilinmiyor", "bilinmiyor"
        own = excess = None
        try:
            if saved is not None and original_cycle is not None:
                advice = Advice.model_validate_json(saved.payload_json)
                metadata = _object(original_cycle.payload_json)
                profile = metadata.get("profile_hash")
                if isinstance(profile, str):
                    policy = profile
                instrument = advice.instrument
                valid = (saved.action == "BUY" and saved.ticker == first.ticker and saved.day == first.day
                         and saved.cycle_key == first.cycle_key and saved.key == first.decision_key
                         and stored(saved.published_at) == stored(first.published_at)
                         and first.key == digest((policy, first.day, "entry", first.ticker)))
                if valid and mark is not None:
                    payload = _object(mark.payload_json)
                    observed_at = _timestamp(payload.get("observed_at"))
                    target_at = _timestamp(payload.get("target_at"))
                    if (observed_at is not None and observed_at == stored(mark.observed_at) and observed_at <= now and target_at is not None
                            and target_at.date() == day and observed_at >= target_at
                            and payload.get("status") in {"complete", "missing_reference"}):
                        own = _number(payload.get("directional_underlying_pct"))
                        excess = _number(payload.get("directional_excess_pp")) if payload.get("status") == "complete" and own is not None and _number(payload.get("spy_pct")) is not None else None
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass
        grouped[policy, instrument].append((own, excess))
    groups = tuple(LegacyGroup(policy, instrument, len(values), sum(v[0] is not None for v in values),
                    sum(v[1] is not None for v in values), _mean([v[0] for v in values if v[0] is not None]),
                    _mean([v[1] for v in values if v[1] is not None]), limited)
                   for (policy, instrument), values in sorted(grouped.items()))
    return groups, int(total), limited


def _paper(engine: Engine, now: datetime, day: date, names: set[str]) -> tuple[tuple[PaperGroup, ...], bool]:
    if LivePaper.__tablename__ not in names:
        return (), False
    start = datetime.combine(day, time.min, LONDON).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, LONDON).astimezone(UTC)
    with session_factory(engine)() as session:
        condition = (LivePaper.state == "CLOSED_SIMULATED", LivePaper.exit_at >= start, LivePaper.exit_at < end, LivePaper.exit_at <= now)
        total = session.scalar(select(func.count()).select_from(LivePaper).where(*condition)) or 0
        records = session.scalars(select(LivePaper).where(*condition).order_by(LivePaper.exit_at, LivePaper.paper_id).limit(READ_LIMIT)).all()
    grouped: dict[tuple[str, str], list[tuple[str, float | None]]] = defaultdict(list)
    for record in records:
        try:
            result = paper_result(record)
            grouped[result.policy, result.instrument].append((result.category, _number(result.pnl_usd)))
        except (ValueError, TypeError, AttributeError, OverflowError):
            grouped["bilinmiyor", record.instrument].append(("unknown", None))
    output = []
    for (policy, instrument), values in sorted(grouped.items()):
        pnls = [pnl for category, pnl in values if category == "closed" and pnl is not None]
        output.append(PaperGroup(policy, instrument, len(pnls), sum(c == "unfilled" for c, _ in values),
                                 sum(c not in {"closed", "unfilled"} for c, _ in values),
                                 _sum(pnls), _mean(pnls), total > READ_LIMIT))
    return tuple(output), total > READ_LIMIT


def _feeds(engine: Engine, now: datetime, names: set[str]) -> tuple[FeedStatus, ...]:
    ready = all(t.__tablename__ in names for t in (shared_storage.Packet, shared_storage.Receipt))
    output = []
    for provider in ("mahalle", "stajyer"):
        if not ready:
            output.append(FeedStatus(provider, "unknown", "Fiziksel kaynak kayıt tablosu henüz yok."))
            continue
        with session_factory(engine)() as session:
            pair = session.execute(select(shared_storage.Packet, shared_storage.Receipt)
                   .options(defer(shared_storage.Packet.compressed_body))
                   .outerjoin(shared_storage.Receipt, shared_storage.Receipt.key == shared_storage.Packet.key)
                   .where(shared_storage.Packet.provider == provider, shared_storage.Packet.requested_at <= now,
                          shared_storage.Packet.received_at <= now)
                   .order_by(shared_storage.Packet.requested_at.desc(), shared_storage.Packet.key.desc()).limit(1)).first()
        if pair is None:
            output.append(FeedStatus(provider, "unknown", "Bu kaynak için saklanmış fiziksel kontrol yok."))
            continue
        packet, receipt = pair
        requested, received = stored(packet.requested_at), stored(packet.received_at)
        valid = shared_storage.valid_receipt(packet, receipt, now)
        confirmed = stored(receipt.available_at) if valid and receipt else None
        if valid and packet.category == "not_modified":
            from webapp.shared_news.model import same_etag
            with session_factory(engine)() as session:
                original = session.scalar(select(shared_storage.Packet).options(defer(shared_storage.Packet.compressed_body))
                           .where(shared_storage.Packet.key == packet.body_packet_key)) if packet.body_packet_key else None
                original_receipt = session.get(shared_storage.Receipt, packet.body_packet_key) if packet.body_packet_key else None
            valid = bool(original is not None and original.provider == provider and original.category == "ok"
                         and original.status_code == 200 and original.etag
                         and (packet.etag is None or same_etag(packet.etag, original.etag))
                         and shared_storage.valid_receipt(original, original_receipt, now)
                         and original_receipt is not None and stored(original_receipt.available_at) <= requested)
        if not valid:
            status, reason = "unknown", "Son yanıtın fiziksel kayıt makbuzu eksik veya doğrulanamadı."
        elif packet.category not in {"ok", "not_modified"}:
            status, reason = "unknown", "Son kayıtlı kaynak kontrolü başarılı değil; haber yokluğu çıkarılmaz."
        elif confirmed is not None and (now - confirmed).total_seconds() > shared_config.settings().max_age_seconds:
            status, reason = "stale", "Son başarılı fiziksel kontrol bayat; içerik yalnız kayıtlı bağlamdır."
        else:
            status, reason = "context", "Fiziksel kaynak kontrolü kayıtlı; yalnız bağlamdır, AL oyu değildir."
        output.append(FeedStatus(provider, status, reason, requested, received, confirmed))
    return tuple(output)


def _news(engine: Engine, now: datetime, names: set[str]) -> UWReadiness:
    from webapp.news_research import config as news_config
    from webapp.news_research.storage import Packet, Receipt

    cfg = news_config.settings()
    packets = receipts = None
    sampled = fresh = 0
    sessions: set[date] = set()
    limited = False
    with session_factory(engine)() as session:
        if Packet.__tablename__ in names:
            packets = session.scalar(select(func.count()).select_from(Packet).where(Packet.received_at <= now)) or 0
        if Receipt.__tablename__ in names:
            receipts = session.scalar(select(func.count()).select_from(Receipt).where(Receipt.available_at <= now)) or 0
        if LiveScan.__tablename__ in names:
            total = session.scalar(select(func.count()).select_from(LiveScan).where(LiveScan.started_at <= now, LiveScan.finished_at <= now)) or 0
            records = session.execute(select(LiveScan.finished_at, case((LiveScan.snapshot_json.contains('"news_research_context":'), 1), else_=0))
                       .where(LiveScan.started_at <= now, LiveScan.finished_at <= now)
                       .order_by(LiveScan.finished_at.desc(), LiveScan.scan_id.desc()).limit(READ_LIMIT)).all()
            limited = total > READ_LIMIT
            for at, has_context in records:
                sampled += 1
                if has_context:
                    fresh += 1
                    market = classify(stored(at), load_live_alpha_settings().calendar)
                    if market.mode is MarketMode.LIVE:
                        sessions.add(market.et_now.date())
    status = "STORAGE_UNAVAILABLE" if packets is None or receipts is None else "LIMITED" if limited else "COLLECTING" if fresh else "LEGACY_ONLY"
    return UWReadiness(status, "Sabit beş UW kolunun yalnız kayıt envanteri gösterilir. Yeni özellik işareti, tamamlanmış ortak getiri seansı değildir; karşılaştırma/inferans çalıştırılmadı. Yeni zamanlı ölçüm bu deneyi beslemez.",
                       news_config.profile_hash(), packets, receipts, sampled, fresh, limited, len(sessions), cfg.minimum_common_sessions,
                       (Arm("A", "Akış + fiyat"), Arm("B", "Akış + fiyat + göreli güç"), Arm("C", "Akış + fiyat + haber"),
                        Arm("D", "Akış + fiyat + göreli güç VE haber"), Arm("E", "Akış + fiyat + göreli güç VEYA haber")))


def _cohort(target: Target, activation: Activation, now: datetime) -> bool:
    return bool(target.activation_key == activation.key and target.profile_hash == activation.profile_hash
                and target.opening_profile_hash == activation.opening_profile_hash
                and activation.activated_at <= target.published_at <= target.discovered_at <= now)


def _read_rows(engine: Engine, now: datetime, targets: tuple[Target, ...]) -> tuple[TargetRow, ...]:
    from . import storage

    assessments = {a.target_key: a for a in storage.assessments(engine, now, target_keys=tuple(t.key for t in targets))}
    output = []
    for target in sorted(targets, key=lambda t: (t.published_at, t.ticker, t.horizon_minutes, t.key)):
        assessment = assessments.get(target.key)
        attempts = tuple(a for a in storage.attempts(engine, target.key) if a.claimed_at <= now)
        legs = tuple(leg for attempt in attempts for leg in storage.legs(engine, attempt.key, now))
        completions = storage.completions(engine, target.key, now)
        output.append(_row(target, assessment, legs, now, completions))
    return tuple(output)


def _first_pending(engine: Engine, now: datetime, activation: Activation) -> bool | None:
    from . import storage

    # Every inspected assessment's hash is checked before a status is trusted;
    # a corrupted status cannot masquerade as an absence of live observations.
    conditions = (storage.TimedTarget.activation_key == activation.key,
                  storage.TimedTarget.profile_hash == activation.profile_hash,
                  storage.TimedTarget.discovered_at <= now, storage.TimedAssessment.assessed_at <= now)
    with session_factory(engine)() as session:
        joined = storage.TimedAssessment.target_key == storage.TimedTarget.key
        total = session.scalar(select(func.count()).select_from(storage.TimedTarget).join(storage.TimedAssessment, joined).where(*conditions)) or 0
        records = session.scalars(select(storage.TimedTarget.key).join(storage.TimedAssessment, joined).where(*conditions)
                    .order_by(storage.TimedAssessment.assessed_at, storage.TimedTarget.key).limit(READ_LIMIT)).all()
    uncertain = False
    for key in records:
        try:
            targets = storage.targets(engine, now, target_keys=(key,))
            if len(targets) != 1:
                uncertain = True
                continue
            target = targets[0]
            if not _cohort(target, activation, now):
                uncertain = True
                continue
            assessments = storage.assessments(engine, now, target_keys=(target.key,))
            if len(assessments) != 1:
                uncertain = True
                continue
            if assessments[0].status not in {"observed", "partial"}:
                continue
            rows = _read_rows(engine, now, (target,))
            if rows and rows[0].own_pct is not None:
                return False
            uncertain = True
        except (ValueError, TypeError, AttributeError, OverflowError):
            uncertain = True
    return None if uncertain or total > READ_LIMIT else True


def _new_rows(engine: Engine, now: datetime, day: date, names: set[str]) -> tuple[Activation | None, tuple[TargetRow, ...], bool, bool | None]:
    from . import storage

    if any(t.__tablename__ not in names for t in storage.TABLES):
        return None, (), False, None
    activation = storage.activation(engine, now)
    if activation is None:
        return None, (), False, None
    raw_targets = storage.targets(engine, now, day=day)
    targets = tuple(t for t in raw_targets if _cohort(t, activation, now))
    with session_factory(engine)() as session:
        total = session.scalar(select(func.count()).select_from(storage.TimedTarget).where(
                    storage.TimedTarget.day == day, storage.TimedTarget.discovered_at <= now,
                    storage.TimedTarget.activation_key == activation.key,
                    storage.TimedTarget.profile_hash == activation.profile_hash)) or 0
    return activation, _read_rows(engine, now, targets), total > len(targets), _first_pending(engine, now, activation)


@scoped_read
def build(engine: Engine, now: datetime, day: date | None = None) -> DailyReport:
    """Read the chosen US call-session date. All displayed clocks use London."""
    now = aware(now)
    selected_day = day if day is not None else now.astimezone(LONDON).date()
    if not isinstance(selected_day, date) or isinstance(selected_day, datetime):
        raise ValueError("measurement day must be a date")
    names = set(inspect(engine).get_table_names())
    legacy, first_calls, legacy_limited = _legacy(engine, now, selected_day, names)
    paper, paper_limited = _paper(engine, now, selected_day, names)
    news_feeds = _feeds(engine, now, names)
    news_research = _news(engine, now, names)
    unverified = False
    try:
        activation, rows, limited, first_pending = _new_rows(engine, now, selected_day, names)
        from . import storage
        initialized = all(t.__tablename__ in names for t in storage.TABLES)
    except ImportError:
        activation, rows, limited, first_pending, initialized = None, (), False, None, False
    except (ValueError, TypeError, AttributeError, OverflowError):
        # A malformed immutable record is an unavailable population, never an
        # empty day or a successful zero. Do not disclose payload/exception text.
        activation, rows, limited, first_pending, initialized = None, (), False, None, False
        unverified = True
    counts = _coverage(rows, first_calls) if initialized else Coverage(first_calls=first_calls)
    limited = limited or legacy_limited or paper_limited
    if unverified:
        status, reason = "UNVERIFIED", "Yeni ölçüm kayıtlarının bütünlüğü doğrulanamadı; hedef kapsamı ve sonuçlar bilinmiyor. Kayıtlar atlanıp sıfır sonuç sayılmadı."
    elif not initialized:
        status, reason = "STORAGE_UNAVAILABLE", "Yeni zamanlı ölçüm tabloları henüz yok; GET tablo oluşturmaz."
    elif activation is None:
        status, reason = "INACTIVE", "Kalıcı aktivasyon doğrulanmadı; geçmiş çağrılara yeni sonuç üretilmez."
    elif limited:
        status, reason = "LIMITED", "Günlük görünüm sınırlı örneklemdir; oranlar yalnız gösterilen hedeflere aittir."
    elif not rows:
        status, reason = "EMPTY", "Seçilen günde yeni yöntem için keşfedilmiş hedef yok; başarı oranı ve getiri bilinmiyor."
    elif first_pending:
        status, reason = "AWAITING_FIRST_OBSERVATION", "Aktivasyon sonrası ilk nitelikli canlı dayanak gözlemi bekleniyor."
    else:
        status, reason = "COLLECTING", "Yeni yöntem kayıtları gösteriliyor; ölçüm kapsamı işlem başarısı veya doğrulanmış alfa değildir."
    if activation and not enabled():
        reason += " Toplayıcı şu anda kapalı; kayıtlar salt okunur gösteriliyor."
    return DailyReport(selected_day, now, status, reason, activation.activated_at if activation else None,
                        METHOD, first_pending, limited, counts, rows, _horizons(rows) if initialized else (), legacy, paper, news_feeds, news_research)


def build_call(engine: Engine, now: datetime, key: str) -> CallReport:
    """Do not attach a first publication's outcomes to a later update."""
    from . import storage

    now = aware(now)
    names = set(inspect(engine).get_table_names())
    activation = storage.activation(engine, now) if storage.TimedActivation.__tablename__ in names else None
    base = CallReport(key, "UNKNOWN", "Özgün ilk çağrı veya aktivasyon sınırı doğrulanamadı.",
                      activation.activated_at if activation else None, METHOD, None, None, ())
    if any(t.__tablename__ not in names for t in (opening_storage.OpeningDecision, opening_storage.OpeningPrimary)):
        return base
    with session_factory(engine)() as session:
        record = session.get(opening_storage.OpeningDecision, key)
        if record is None or stored(record.published_at) > now:
            return base
        primaries = session.scalars(select(opening_storage.OpeningPrimary).where(
                     opening_storage.OpeningPrimary.kind == "entry", opening_storage.OpeningPrimary.day == record.day,
                     opening_storage.OpeningPrimary.ticker == record.ticker, opening_storage.OpeningPrimary.published_at <= now)
                     .order_by(opening_storage.OpeningPrimary.published_at).limit(READ_LIMIT)).all()
    if activation:
        primaries = [p for p in primaries if p.key == digest((activation.opening_profile_hash, record.day, "entry", record.ticker))]
    if len(primaries) != 1:
        return base
    canonical = primaries[0]
    if canonical.decision_key != key:
        return replace(base, status="UPDATE_NOT_ELIGIBLE", reason="Bu kayıt günün ilk AL yayını değil; ilk çağrının sonucu bu güncellemeye taşınmaz.", first=False, canonical_key=canonical.decision_key)
    valid = (record.action == "BUY" and canonical.cycle_key == record.cycle_key and stored(canonical.published_at) == stored(record.published_at))
    if not valid:
        return base
    base = replace(base, first=True, canonical_key=key)
    if activation is None:
        return replace(base, status="INACTIVE", reason="Kalıcı aktivasyon doğrulanmadı; bu geçmiş çağrıya hedef üretilmez.")
    if stored(record.published_at) < activation.activated_at:
        return replace(base, status="PRE_ACTIVATION", reason="İlk çağrı yeni yöntemin aktivasyonundan önce yayımlandı; geriye dönük yakalama yapılmaz.")
    if storage.TimedTarget.__tablename__ not in names:
        return replace(base, status="UNKNOWN", reason="Yeni yöntem hedef tablosu henüz yok.")
    try:
        raw_targets = storage.targets(engine, now, decision_key=key)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return replace(base, status="UNKNOWN", reason="Bu çağrının yeni ölçüm kayıtlarının bütünlüğü doğrulanamadı; kapsam ve sonuçlar bilinmiyor.")
    targets = tuple(t for t in raw_targets if _cohort(t, activation, now))
    if not targets:
        return replace(base, status="AWAITING_DISCOVERY", reason="İlk yayın aktivasyon sonrası; doğrulanmış hedef keşif kaydı henüz yok.")
    try:
        rows = _read_rows(engine, now, targets)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return replace(base, status="UNKNOWN", reason="Bu çağrının yeni ölçüm kayıtlarının bütünlüğü doğrulanamadı; kapsam ve sonuçlar bilinmiyor.")
    return replace(base, status="RECORDED", reason="Yalnız bu ilk yayının yeni yöntem hedefleri gösterilir; eski işaretler ayrı kalır.",
                   rows=rows, limited=len(raw_targets) >= READ_LIMIT or len(targets) != len(raw_targets))
