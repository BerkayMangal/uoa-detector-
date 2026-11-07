"""Read-only fixed-arm accounting over the complete observed source roster.

Stored decision JSON is an untrusted persistence adapter. A missing/late decision
never removes its original candidate or becomes a zero-return observation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import session_factory
from webapp.fusion_live import config, storage
from webapp.live_alpha.store import LiveScan
from webapp.shadow_capture import config as source_config
from webapp.shadow_capture import storage as source_storage
from webapp.shadow_capture import view as source_view
from webapp.shadow_capture.model import Record as SourceRecord
from webapp.shadow_capture.model import aware, digest, stored

ET = ZoneInfo("America/New_York")
HORIZONS = ("m15", "m60")
ARM_LABELS = {
    "base": "Akış + fiyat",
    "semantic_positive": "Olumlu haber yorumu",
    "semantic_veto": "Olumsuz haber vetosu",
    "semantic_fundamental": "Olumlu haber + temel koşullar",
}
STATES = frozenset(("PAPER_ACCEPT", "PAPER_SKIP", "PAPER_UNKNOWN"))


@dataclass(frozen=True)
class Identity:
    version: str
    profile_hash: str
    implementation_hash: str


@dataclass(frozen=True)
class SourceIdentity:
    version: str
    profile_hash: str
    implementation_hash: str
    live_profile_hash: str
    news_profile_hash: str
    news_implementation_hash: str


@dataclass(frozen=True)
class Decision:
    arm: str
    label: str
    state: str
    recorded_state: str | None
    reason: str
    reasons: tuple[str, ...] = ()
    at: datetime | None = None
    available_at: datetime | None = None
    cutoff: datetime | None = None
    used_at: tuple[datetime, ...] = ()
    verified: bool = False
    timely: bool = False
    m15_net_usd: float | None = None
    m60_net_usd: float | None = None


@dataclass(frozen=True)
class Candidate:
    key: str
    checksum: str
    ticker: str
    slot: int
    source_started_at: datetime
    source_at: datetime
    available_at: datetime
    cutoff: datetime
    original_rs: bool | None
    original_news: bool | None
    decisions: tuple[Decision, ...]
    phases: tuple[source_view.Phase, ...]


@dataclass(frozen=True)
class IgnoredCandidate:
    key: str
    ticker: str
    reason: str


@dataclass(frozen=True)
class ArmMetric:
    arm: str
    label: str
    candidates: int
    accepts: int
    skips: int
    unknown: int
    complete: bool
    net_usd: float | None
    reason: str


@dataclass(frozen=True)
class Horizon:
    horizon: str
    label: str
    candidates: int
    reference_observed: int
    reference_complete: bool
    common_complete: bool
    arms: tuple[ArmMetric, ...]


@dataclass(frozen=True)
class Daily:
    day: date
    as_of: datetime
    method: str
    identity: Identity
    source_identity: SourceIdentity
    activation_key: str | None
    activation_at: datetime | None
    status: str
    reason: str
    candidates: int = 0
    source_candidates: int | None = None
    ignored_candidates: int = 0
    invalid_candidates: int = 0
    universe_verified: bool = False
    session_closed: bool = False
    limited: bool = False
    rows: tuple[Candidate, ...] = ()
    ignored: tuple[IgnoredCandidate, ...] = ()
    horizons: tuple[Horizon, ...] = ()
    source_method: str = "shadow_price_v1"
    inference: str = "NOT_PERFORMED"
    winner: None = None


def current_identity() -> Identity:
    value = config.identity()
    return Identity(value["version"], value["profile_hash"], value["implementation_hash"])


def session_close(day: date) -> datetime | None:
    calendar = load_live_alpha_settings().calendar
    if is_session_day(day, calendar) is not True:
        return None
    return datetime.combine(day, close_time(day, calendar), ET).astimezone(UTC)


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("clock_missing")
    return aware(datetime.fromisoformat(value))


def _decision(
    engine: Engine,
    source: SourceRecord,
    active: storage.Record,
    identity: Identity,
    cutoff: datetime,
    arm: str,
    at: datetime,
) -> Decision:
    initial = Decision(
        arm,
        ARM_LABELS[arm],
        "PENDING" if at < cutoff else "PAPER_UNKNOWN",
        None,
        "evidence_pending" if at < cutoff else "decision_missing",
        cutoff=cutoff,
    )
    key = digest((config.settings().version, active.key, source.key, arm))
    record = storage.get(engine, key, at)
    if record is None:
        raw = storage.get(engine, key, at, confirmed=False)
        return (
            replace(initial, state="PAPER_UNKNOWN", reason="decision_receipt_unverified")
            if raw
            else initial
        )
    payload = record.payload
    recorded_state = payload.get("state")
    base = replace(
        initial,
        recorded_state=recorded_state if isinstance(recorded_state, str) else None,
        at=record.at,
        available_at=record.available_at,
        state="PAPER_UNKNOWN",
    )
    try:
        if (
            record.kind != "decision"
            or record.day != source.day
            or record.candidate_key != source.key
            or record.arm != arm
            or payload.get("identity") != asdict(identity)
            or payload.get("activation_key") != active.key
            or payload.get("candidate_checksum") != source.checksum
            or payload.get("ticker") != source.payload.get("ticker")
            or recorded_state not in STATES
            or _time(payload.get("cutoff")) != cutoff
            or active.available_at is None
            or source.confirmed_at is None
            or record.available_at is None
            or not active.available_at <= record.at <= record.available_at <= at
            or record.at < source.confirmed_at
        ):
            raise ValueError("decision_identity_or_clock_unverified")
        raw_used, raw_reasons = payload.get("used_at"), payload.get("reasons")
        if (
            not isinstance(raw_used, list)
            or not isinstance(raw_reasons, list)
            or len(raw_used) > config.settings().read_limit
            or len(raw_reasons) > config.settings().read_limit
            or any(not isinstance(reason, str) for reason in raw_reasons)
        ):
            raise ValueError("decision_evidence_shape_unverified")
        used = tuple(_time(value) for value in raw_used)
        if any(value > record.at for value in used) or (
            recorded_state in ("PAPER_ACCEPT", "PAPER_SKIP") and not used
        ):
            raise ValueError("decision_uses_future_or_missing_evidence")
        base = replace(base, reasons=tuple(raw_reasons), used_at=used, verified=True)
        if record.available_at > cutoff:
            return replace(base, reason="decision_after_entry_cutoff")
        assert isinstance(recorded_state, str)
        return replace(base, state=recorded_state, reason="recorded_before_cutoff", timely=True)
    except (ValueError, TypeError, KeyError):
        return replace(base, reason="decision_unverified")


def _reference(row: Candidate, horizon: str) -> float | None:
    entry = next((p for p in row.phases if p.phase == "entry"), None)
    end = next((p for p in row.phases if p.phase == horizon), None)
    if (
        entry is None
        or end is None
        or entry.state != "observed"
        or end.state != "observed"
        or end.model_net_usd is None
        or not math.isfinite(end.model_net_usd)
    ):
        return None
    return end.model_net_usd


def _metrics(rows: tuple[Candidate, ...], universe_verified: bool) -> tuple[Horizon, ...]:
    out = []
    for horizon in HORIZONS:
        references = tuple(_reference(row, horizon) for row in rows)
        reference_count = sum(value is not None for value in references)
        reference_complete = bool(rows) and universe_verified and reference_count == len(rows)
        arms = []
        for arm in config.settings().arms:
            decisions = tuple(next(d for d in row.decisions if d.arm == arm) for row in rows)
            accepts = sum(d.state == "PAPER_ACCEPT" and d.timely for d in decisions)
            skips = sum(d.state == "PAPER_SKIP" and d.timely for d in decisions)
            unknown = len(rows) - accepts - skips
            complete = reference_complete and unknown == 0
            net = (
                math.fsum(
                    value
                    for value, d in zip(references, decisions, strict=True)
                    if value is not None and d.state == "PAPER_ACCEPT"
                )
                if complete
                else None
            )
            arms.append(
                ArmMetric(
                    arm,
                    ARM_LABELS[arm],
                    len(rows),
                    accepts,
                    skips,
                    unknown,
                    complete,
                    net,
                    "complete_roster" if complete else "incomplete_roster_or_evidence",
                )
            )
        out.append(
            Horizon(
                horizon,
                "+15 dakika" if horizon == "m15" else "+60 dakika",
                len(rows),
                reference_count,
                reference_complete,
                reference_complete and all(arm.complete for arm in arms),
                tuple(arms),
            )
        )
    return tuple(out)


def _with_slot_nets(
    rows: tuple[Candidate, ...], horizons: tuple[Horizon, ...]
) -> tuple[Candidate, ...]:
    output = []
    for row in rows:
        decisions = []
        for decision in row.decisions:
            nets = {}
            for horizon in horizons:
                metric = next(a for a in horizon.arms if a.arm == decision.arm)
                net = (
                    _reference(row, horizon.horizon)
                    if decision.timely and decision.state == "PAPER_ACCEPT"
                    else None
                )
                if decision.timely and decision.state == "PAPER_SKIP" and metric.complete:
                    net = 0.0
                nets[horizon.horizon] = net
            decisions.append(replace(decision, m15_net_usd=nets["m15"], m60_net_usd=nets["m60"]))
        output.append(replace(row, decisions=tuple(decisions)))
    return tuple(output)


def build_day(engine: Engine, day: date, now: datetime) -> Daily:
    """Original source roster first, then immutable decisions and verified marks."""
    at = aware(now)
    identity = current_identity()
    initial = Daily(
        day,
        at,
        config.settings().version,
        identity,
        SourceIdentity(**source_config.identity()),
        None,
        None,
        "STORAGE_UNAVAILABLE",
        "Yeni motorun kayıtları henüz okunamıyor.",
    )
    try:
        if not storage.available(engine):
            return initial
        active = storage.active(engine, at)
        if active is None or active.available_at is None:
            return replace(
                initial,
                status="NOT_ACTIVATED",
                reason="Yeni motorun fiziksel etkinleşmesi doğrulanamadı.",
            )
        initial = replace(initial, activation_key=active.key, activation_at=active.available_at)
        if day > at.astimezone(ET).date():
            return replace(initial, status="FUTURE", reason="Seçilen seans henüz gelmedi.")
        if not source_storage.available(engine):
            return replace(initial, reason="Orijinal fiyat kayıtlarının durumu bilinmiyor.")
        source_active = source_storage.active(engine, at)
        if source_active is None:
            return replace(initial, reason="Orijinal fiyat yönteminin etkinliği doğrulanamadı.")
        source = source_view.build(engine, at, day)
        if source.candidate_count is None:
            return replace(initial, reason="Orijinal aday evreni doğrulanamadı.")
        by_key = {row.key: row for row in source.rows}
        with session_factory(engine)() as session:
            raw = (
                session.execute(
                    select(source_storage.ShadowEvent.key)
                    .where(
                        source_storage.ShadowEvent.kind == "candidate",
                        source_storage.ShadowEvent.day == day,
                    )
                    .order_by(source_storage.ShadowEvent.at, source_storage.ShadowEvent.key)
                    .limit(config.settings().candidate_limit + 1)
                )
                .scalars()
                .all()
            )
        limited = len(raw) > config.settings().candidate_limit or source.limited
        rows = []
        ignored = []
        invalid = 0
        for key in raw[: config.settings().candidate_limit]:
            record = source_storage.get(engine, key, at, require_receipt=False)
            if record is None or record.confirmed_at is None:
                invalid += 1
                continue
            ticker = record.payload.get("ticker")
            if not isinstance(ticker, str):
                invalid += 1
                continue
            if record.parent_key != source_active.key or record.confirmed_at < active.available_at:
                ignored.append(
                    IgnoredCandidate(
                        record.key, ticker, "source_before_or_outside_fusion_activation"
                    )
                )
                continue
            verified = by_key.get(record.key)
            scan_id = record.payload.get("scan_id")
            if verified is None or not isinstance(scan_id, str):
                invalid += 1
                continue
            with session_factory(engine)() as session:
                times = session.execute(
                    select(LiveScan.started_at, LiveScan.finished_at).where(
                        LiveScan.scan_id == scan_id
                    )
                ).first()
            if times is None:
                invalid += 1
                continue
            started, finished = (stored(value) for value in times)
            if started < active.available_at:
                ignored.append(
                    IgnoredCandidate(
                        record.key, ticker, "source_scan_started_before_fusion_activation"
                    )
                )
                continue
            if not started <= finished <= record.confirmed_at <= at or finished != aware(
                verified.source_at
            ):
                invalid += 1
                continue
            cutoff = record.confirmed_at + timedelta(
                seconds=source_config.settings().entry_delay_seconds
                - config.settings().entry_lead_seconds
            )
            decisions = tuple(
                _decision(engine, record, active, identity, cutoff, arm, at)
                for arm in config.settings().arms
            )
            rows.append(
                Candidate(
                    record.key,
                    record.checksum,
                    ticker,
                    verified.slot,
                    started,
                    finished,
                    record.confirmed_at,
                    cutoff,
                    verified.rs,
                    verified.news,
                    decisions,
                    verified.phases,
                )
            )
        rows.sort(key=lambda row: row.slot)
        universe = not limited and invalid == 0
        frozen_rows = tuple(rows)
        horizons = _metrics(frozen_rows, universe)
        closing = session_close(day)
        closed = closing is not None and at >= closing
        status, reason = (
            "COLLECTING",
            "Yeni motorun kararları ve 15/60 dakika referansları ayrı ölçülüyor.",
        )
        if day < active.available_at.astimezone(ET).date():
            status, reason = "BEFORE_ACTIVATION", "Bu seans yeni motor etkinleşmeden önceydi."
        elif not universe:
            status, reason = (
                "UNVERIFIED",
                "Aday evreninde eksik veya doğrulanamayan kayıt var; toplam üretilmedi.",
            )
        elif not rows:
            status, reason = (
                "NO_CANDIDATES",
                "Etkinleşmeden sonra uygun yeni aday bekleniyor; boş gün getiri sayılmaz.",
            )
        elif closed:
            status = "COMPLETE" if all(h.common_complete for h in horizons) else "INCOMPLETE"
            reason = (
                "Seansın tüm aday ve kararları ortak panelde tamamlandı."
                if status == "COMPLETE"
                else "Seans kapandı; eksik karar veya fiyatlar toplamı bilinmiyor bırakıyor."
            )
        return replace(
            initial,
            status=status,
            reason=reason,
            candidates=len(rows),
            source_candidates=len(raw),
            ignored_candidates=len(ignored),
            invalid_candidates=invalid,
            universe_verified=universe,
            session_closed=closed,
            limited=limited,
            rows=_with_slot_nets(frozen_rows, horizons),
            ignored=tuple(ignored),
            horizons=horizons,
        )
    except Exception:
        return replace(initial, reason="Doğrulanmış yeni motor kayıtları şu an okunamıyor.")
