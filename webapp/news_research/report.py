"""Cheap, bounded research readiness counts; GET never runs the experiment."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, inspect, select
from sqlalchemy.engine import Engine
from webapp.board.db import session_factory
from webapp.live_alpha.store import LiveScan
from webapp.news_research.config import profile_hash, settings
from webapp.news_research.storage import Packet, Receipt
from webapp.news_research.transport import STATE as CAPTURE_STATE
from webapp.opening.model import utc

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings


@dataclass(frozen=True)
class ArmDescription:
    key: str
    label: str


@dataclass(frozen=True)
class Summary:
    status: str
    reason: str
    as_of: datetime
    profile_hash: str
    total_scans: int | None
    sampled_scans: int
    new_context_scans: int
    legacy_scans: int
    packet_count: int | None
    receipt_count: int | None
    limited: bool
    causal_opportunities: int | None
    prospective_sessions: int
    minimum_common_sessions: int
    arms: tuple[ArmDescription, ...]
    strict_historical_news_available: bool = False
    vendor_calls_added: int = 0
    inference: str = "NOT_PERFORMED"
    winner: str | None = None
    capture_status: str = "unknown"
    capture_error: str | None = None


def build(engine: Engine, asof: datetime) -> Summary:
    if asof.tzinfo is None:
        raise ValueError("aware readiness asof required")
    asof = utc(asof)
    cfg = settings()
    names = set(inspect(engine).get_table_names())
    total: int | None = None
    packets: int | None = None
    receipts: int | None = None
    observed = fresh = legacy = 0
    dates = set()
    limited = False
    with session_factory(engine)() as session:
        if Packet.__tablename__ in names:
            packets = session.scalar(select(func.count()).select_from(Packet).where(Packet.received_at <= asof)) or 0
        if Receipt.__tablename__ in names:
            receipts = session.scalar(select(func.count()).select_from(Receipt).where(Receipt.available_at <= asof)) or 0
        if LiveScan.__tablename__ in names:
            total = session.scalar(select(func.count()).select_from(LiveScan).where(LiveScan.finished_at <= asof)) or 0
            # Project only narrow metadata, not thousands of full JSON payloads.
            rows = session.execute(select(
                LiveScan.finished_at,
                case((LiveScan.snapshot_json.contains('"news_research_context":'), 1), else_=0),
            ).where(LiveScan.started_at <= asof, LiveScan.finished_at <= asof)
              .order_by(LiveScan.started_at.desc(), LiveScan.scan_id.desc())
              .limit(cfg.row_limit + 1)).all()
            limited = len(rows) > cfg.row_limit
            for started, has_context in rows[:cfg.row_limit]:
                observed += 1
                if has_context:
                    fresh += 1
                    market = classify(utc(started), load_live_alpha_settings().calendar)
                    if market.mode is MarketMode.LIVE:
                        dates.add(market.et_now.date())
                else:
                    legacy += 1
    if total is None or packets is None or receipts is None:
        status, reason = "STORAGE_UNAVAILABLE", "Yeni fiziksel haber arşivi henüz hazır değil; eski kayıtlar geriye dönük doğrulanmaz."
    elif limited:
        status, reason = "LIMITED", "Tarama sayımları sınırlandırılmış örneklemdir; kalibrasyon hazır sayılmaz."
    elif fresh == 0:
        status, reason = "LEGACY_ONLY", "Yeni araştırma bağlamı taşıyan tarama yok; geçmiş uygulama saatleri fiziksel haber erişimi kanıtı değildir."
    else:
        status, reason = "COLLECTING", "Fiziksel haber ve yeni tarama kanıtı toplanıyor; sonuçlar ve ortak tam seanslar ayrı çevrimdışı deneyde ölçülür."
    return Summary(status, reason, asof, profile_hash(), total, observed, fresh, legacy,
                   packets, receipts, limited, None, len(dates), cfg.minimum_common_sessions,
                   (ArmDescription("A", "Akış + fiyat"), ArmDescription("B", "Akış + fiyat + göreli güç"),
                    ArmDescription("C", "Akış + fiyat + haber"), ArmDescription("D", "Akış + fiyat + göreli güç VE haber"),
                    ArmDescription("E", "Akış + fiyat + göreli güç VEYA haber")),
                   capture_status=str(CAPTURE_STATE.get("status", "unknown")),
                   capture_error=str(CAPTURE_STATE["error"]) if CAPTURE_STATE.get("error") else None)
