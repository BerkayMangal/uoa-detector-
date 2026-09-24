"""View model for ``GET /`` — reads the newest ``alfa_live_scan`` and nothing slow.

The page never re-computes a decision. It does apply one guard at render time: a
snapshot that is older than three cycles, or a BUY card whose session has ended,
is shown with its entry withdrawn ("bayat — giriş yok"), so a stale card can never
stay a green ALIM.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.decide import REC_TR
from uoa_detector.live_alpha.model import Recommendation
from uoa_detector.live_alpha.settings import LiveAlphaSettings, load_live_alpha_settings
from webapp.live_alpha import store
from webapp.live_alpha.inputs import as_utc

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_TR = ZoneInfo("Europe/Istanbul")
_ET = ZoneInfo("America/New_York")
_ready_lock = threading.Lock()
_ready_engines: set[int] = set()
_SETTINGS: LiveAlphaSettings | None = None

READINESS_TR = {
    "READY": "giriş hazır",
    "TRIGGER_PENDING": "tetik bekleniyor",
    "QUOTE_PENDING": "fiyatlama bekliyor",
    "RISK_BLOCKED": "risk bütçesine sığmıyor",
    "INVALID": "uygulanamaz",
}
TRACKING_TR = {
    "PAPER_PENDING": "PAPER: giriş bekleniyor",
    "PAPER_OPEN": "PAPER: açık",
    "EXIT_SIGNALLED": "PAPER: çıkış sinyali, fiyat bekleniyor",
    "CLOSED_SIMULATED": "PAPER: kapandı (simülasyon)",
    "UNRESOLVED": "PAPER: çözülmedi",
}
MODE_TR = {
    "LIVE": "Piyasa açık",
    "PREMARKET": "Açılış öncesi",
    "CLOSED": "Piyasa kapalı",
    "DEGRADED": "Takvim/veri belirsiz",
}


def settings() -> LiveAlphaSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_live_alpha_settings()
    return _SETTINGS


def ensure_ready(engine: Engine) -> None:
    key = id(engine)
    with _ready_lock:
        if key in _ready_engines:
            return
        store.ensure_live_tables(engine)
        _ready_engines.add(key)


@dataclass
class CardView:
    data: dict[str, Any]
    label: str
    tone: str                   # green / amber / red / gray
    readiness_text: str
    withdrawn: str = ""         # non-empty when the page withdrew the entry at render time
    tracking: str = ""


@dataclass
class LiveView:
    has_scan: bool
    mode_text: str
    mode: str
    session: dict[str, Any]
    now_et: str
    now_tr: str
    scan_age_seconds: int | None
    scan_stale: bool
    status: str
    status_text: str
    summary: str
    actionable: list[CardView] = field(default_factory=list)
    watching: list[CardView] = field(default_factory=list)
    funnel: dict[str, int] = field(default_factory=dict)
    universe: list[str] = field(default_factory=list)
    flow_run: str | None = None
    flow_last_print: str | None = None
    requests: dict[str, Any] = field(default_factory=dict)
    papers_open: list[dict[str, Any]] = field(default_factory=list)
    papers_closed: list[dict[str, Any]] = field(default_factory=list)
    paper_stats: dict[str, Any] = field(default_factory=dict)
    manuals: list[dict[str, Any]] = field(default_factory=list)
    beats: list[dict[str, Any]] = field(default_factory=list)
    spot_age: dict[str, int] = field(default_factory=dict)
    news_failed: list[str] = field(default_factory=list)
    policy_version: str = ""


def _tone(rec: str, readiness: str) -> str:
    if rec == Recommendation.BUY.value and readiness == "READY":
        return "green"
    if rec in (Recommendation.BUY.value, Recommendation.CONDITIONAL_BUY.value):
        return "amber"
    if rec in (Recommendation.AVOID.value, Recommendation.BEARISH_SETUP.value, Recommendation.EXIT_REVIEW.value):
        return "red"
    return "gray"


def _paper_dict(p: store.LivePaper, now: datetime) -> dict[str, Any]:
    plan = json.loads(p.plan_json)
    return {
        "paper_id": p.paper_id, "opportunity_id": p.opportunity_id, "ticker": p.ticker,
        "direction": p.direction, "instrument": p.instrument, "state": p.state,
        "state_text": TRACKING_TR.get(p.state, p.state),
        "created_at": as_utc(p.created_at).astimezone(_ET).strftime("%d.%m %H:%M ET"),
        "entry_price": p.entry_price, "quantity": p.quantity, "exit_price": p.exit_price,
        "exit_reason": p.exit_reason, "pnl_usd": p.pnl_usd, "last_mark": p.last_mark,
        "stop": (plan.get("stock_plan") or {}).get("stop"),
        "target": (plan.get("stock_plan") or {}).get("target"),
        "chase_limit": (plan.get("stock_plan") or {}).get("chase_limit"),
    }


def build_view(engine: Engine, now: datetime) -> LiveView:
    ensure_ready(engine)
    cfg = settings()
    session_now = classify(now, cfg.calendar)
    scan = store.latest_scan(engine)
    now_et = now.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")
    now_tr = now.astimezone(_TR).strftime("%H:%M TR")
    papers = store.papers(engine)
    open_states = {"PAPER_PENDING", "PAPER_OPEN", "EXIT_SIGNALLED"}
    papers_open = [_paper_dict(p, now) for p in papers if p.state in open_states]
    papers_closed = [_paper_dict(p, now) for p in papers if p.state not in open_states][:20]
    realised = [p["pnl_usd"] for p in papers_closed if p["pnl_usd"] is not None]
    paper_stats = {
        "open": len(papers_open),
        "closed": len(realised),
        "unfilled": sum(1 for p in papers_closed if p["pnl_usd"] is None),
        "pnl_usd": round(sum(realised), 2) if realised else 0.0,
        "wins": sum(1 for x in realised if x > 0),
    }
    manual_rows = [
        {"at": as_utc(m.at).astimezone(_ET).strftime("%d.%m %H:%M ET"), "ticker": m.ticker,
         "instrument": m.instrument, "side": m.side, "quantity": m.quantity, "price": m.price, "note": m.note}
        for m in store.manuals(engine, 20)
    ]
    beats = [
        {"at": as_utc(at).astimezone(_ET).strftime("%d.%m %H:%M:%S ET"), "status": st, "detail": d}
        for at, st, d in store.last_beats(engine, 3)
    ]
    if scan is None:
        return LiveView(
            has_scan=False, mode_text=MODE_TR.get(session_now.mode.value, session_now.mode.value),
            mode=session_now.mode.value, session={}, now_et=now_et, now_tr=now_tr, scan_age_seconds=None,
            scan_stale=True, status="none", status_text="Henüz tarama kaydı yok: iş ilk döngüsünü bitirmedi.",
            summary="Henüz tarama yok.", papers_open=papers_open, papers_closed=papers_closed,
            paper_stats=paper_stats, manuals=manual_rows, beats=beats, policy_version=cfg.policy_version,
        )
    snap = scan.snapshot
    age = int((now - as_utc(scan.started_at)).total_seconds())
    max_age = 3 * (cfg.cycle.live_seconds if session_now.mode is MarketMode.LIVE else cfg.cycle.closed_seconds)
    stale = age > max_age
    by_opp = {p["opportunity_id"]: p for p in papers_open + papers_closed}
    actionable: list[CardView] = []
    watching: list[CardView] = []
    for card in snap.get("cards", []):
        rec, ready = card["recommendation"], card["readiness"]
        withdrawn = ""
        if ready == "READY" and (stale or session_now.mode is not MarketMode.LIVE):
            withdrawn = (
                "Tarama bayat; bu giriş geri çekildi, yeni tarama bekleniyor."
                if stale else
                f"Karar {MODE_TR.get(card['market_mode'], card['market_mode']).lower()} anına ait; "
                "piyasa şu an açık değil, giriş geri çekildi."
            )
        view = CardView(
            data=card, label=REC_TR[Recommendation(rec)],
            tone="gray" if withdrawn else _tone(rec, ready),
            readiness_text=READINESS_TR.get(ready, ready), withdrawn=withdrawn,
            tracking=by_opp.get(card["opportunity_id"], {}).get("state_text", ""),
        )
        if rec == Recommendation.WATCH.value:
            watching.append(view)
        else:
            actionable.append(view)
    actionable = actionable[: cfg.cycle.max_cards]
    buys = [c for c in actionable if c.data["recommendation"] == "BUY" and not c.withdrawn]
    conds = [c for c in actionable if c.data["recommendation"] == "CONDITIONAL_BUY"]
    if buys:
        summary = "Bugün öne çıkan alım: " + ", ".join(c.data["ticker"] for c in buys) + "."
    elif conds:
        summary = (
            "Şu an hazır alım yok; koşullu alım planları: "
            + ", ".join(c.data["ticker"] for c in conds) + "."
        )
    elif actionable:
        summary = "Alım önermiyorum; kaçın/aşağı yönlü görüşler aşağıda."
    else:
        summary = "Bugün alım önermiyorum: hiçbir hisse akış + fiyat + haber/göreli güç koşullarını birlikte geçmedi."
    return LiveView(
        has_scan=True,
        mode_text=MODE_TR.get(session_now.mode.value, session_now.mode.value),
        mode=session_now.mode.value,
        session=snap.get("session", {}),
        now_et=now_et, now_tr=now_tr,
        scan_age_seconds=age, scan_stale=stale,
        status=scan.status, status_text=snap.get("status_text", ""),
        summary=summary,
        actionable=actionable, watching=watching,
        funnel=snap.get("funnel", {}), universe=snap.get("universe", []),
        flow_run=snap.get("flow_run"), flow_last_print=snap.get("flow_last_print"),
        requests=snap.get("requests", {}),
        papers_open=papers_open, papers_closed=papers_closed, paper_stats=paper_stats,
        manuals=manual_rows, beats=beats, spot_age=snap.get("spot_age_seconds", {}),
        news_failed=snap.get("news_failed", []), policy_version=snap.get("policy_version", cfg.policy_version),
    )


def card_from_latest_scan(engine: Engine, opportunity_id: str) -> dict[str, Any] | None:
    scan = store.latest_scan(engine)
    if scan is None:
        return None
    for card in scan.snapshot.get("cards", []):
        if card.get("opportunity_id") == opportunity_id:
            found: dict[str, Any] = card
            return found
    return None
