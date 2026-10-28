"""A conservative projection of committed source JSON, never a new alpha score.

Any is limited to the immutable source-snapshot deserialization boundary.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.db import digest
from webapp.alpha_lab.flow import timestamp
from webapp.desk.positions import Position
from webapp.live_alpha.store import ScanRow
from webapp.opening.model import Advice, Clock, Evidence, Plan, settings, utc


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def fresh(at: datetime | None, now: datetime, seconds: int) -> bool:
    return at is not None and 0 <= (now - at).total_seconds() <= seconds


def point(raw: object, now: datetime, c: Clock) -> tuple[float, datetime] | None:
    if not isinstance(raw, dict) or c.opening is None or c.closing is None:
        return None
    value, at = number(raw.get("spot")), timestamp(raw.get("source_at"))
    if (
        value is None
        or value <= 0
        or at is None
        or not fresh(at, now, settings().source_max_age_seconds)
    ):
        return None
    if not c.opening <= at < c.closing or raw.get("session") != c.day.isoformat():
        return None
    return value, at


def _plan(card: dict[str, Any], now: datetime, c: Clock) -> Plan | None:
    cfg = load_live_alpha_settings()
    raw, instrument = card.get("stock_plan"), card.get("instrument")
    chosen = instrument.get("preferred") if isinstance(instrument, dict) else None
    if not isinstance(raw, dict):
        return None
    levels = [number(raw.get(k)) for k in ("entry_ref", "stop", "target")]
    if any(v is None or v <= 0 for v in levels):
        return None
    entry, stop, target = levels
    assert entry is not None and stop is not None and target is not None
    up = card.get("direction") == "up"
    if not (stop < entry < target if up else target < entry < stop):
        return None
    qty, risk = number(raw.get("shares")), number(raw.get("planned_risk_usd"))
    if chosen == "stock":
        if not up or qty is None or qty <= 0 or not qty.is_integer() or risk is None or risk <= 0:
            return None
        return Plan(
            entry=entry,
            stop=stop,
            target=target,
            quantity=int(qty),
            risk_usd=risk,
            instrument="stock",
        )
    allowed = ("long_call", "bull_call_debit") if up else ("long_put", "bear_put_debit")
    if chosen not in allowed:
        return None
    options = card.get("options", [])
    selected = next((o for o in options if isinstance(o, dict) and o.get("kind") == chosen), None)
    if selected is None or selected.get("readiness") != "READY":
        return None
    lots, loss, debit, cost = (
        number(selected.get(k))
        for k in ("lots", "max_loss_usd", "entry_debit", "roundtrip_cost_pct")
    )
    dte = number(selected.get("dte"))
    if (
        lots is None
        or lots <= 0
        or not lots.is_integer()
        or loss is None
        or loss <= 0
        or debit is None
        or debit <= 0
        or cost is None
        or not 0 <= cost <= cfg.option_plan.max_roundtrip_cost_pct
        or dte is None
        or not cfg.option_plan.min_dte <= dte <= cfg.option_plan.max_dte
    ):
        return None
    legs = selected.get("legs", [])
    if not isinstance(legs, list) or len(legs) != (1 if chosen in ("long_call", "long_put") else 2):
        return None
    symbols = []
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        at, symbol = timestamp(leg.get("fetched_at")), leg.get("option_symbol")
        bid, ask = number(leg.get("bid")), number(leg.get("ask"))
        if (
            not fresh(at, now, cfg.option_plan.quote_max_age_seconds)
            or at is None
            or c.opening is None
            or at < c.opening
            or not isinstance(symbol, str)
            or not symbol
            or bid is None
            or ask is None
            or not 0 < bid <= ask
        ):
            return None
        symbols.append(symbol)
    return Plan(
        entry=entry,
        stop=stop,
        target=target,
        quantity=int(lots),
        risk_usd=loss,
        instrument=chosen,
        contracts=tuple(symbols),
        debit=debit,
        roundtrip_cost_pct=cost,
    )


def project(
    scan: ScanRow,
    card: dict[str, Any],
    now: datetime,
    c: Clock,
    conflicts: set[str],
    *,
    superseded: bool = False,
) -> Advice:
    cfg = settings()
    ticker = str(card.get("ticker", "?"))
    reasons = []
    data = scan.snapshot
    if superseded:
        reasons.append("Daha yeni tarama mevcut; bu kaynak yeni giriş için kullanılamaz.")
    if c.phase != "assessment":
        reasons.append(c.reason)
    if (
        scan.market_mode != "LIVE"
        or scan.status not in ("ok", "degraded")
        or not fresh(utc(scan.started_at), now, cfg.source_max_age_seconds)
        or not utc(scan.started_at) <= utc(scan.finished_at) <= now
    ):
        reasons.append("Tarama güncel ve tamamlanmış değil.")
    if c.assessment is None or utc(scan.started_at) < c.assessment:
        reasons.append("Açılış +15 sonrasında üretilmiş tarama bekleniyor.")
    if (
        card.get("recommendation") not in ("BUY", "BEARISH_SETUP")
        or card.get("readiness") != "READY"
        or card.get("market_mode") != "LIVE"
    ):
        reasons.append("Kaynak strateji girişe hazır değil.")
    decision_at = timestamp(card.get("decision_as_of"))
    if decision_at is None or not utc(scan.started_at) <= decision_at <= now:
        reasons.append("Kararın kaynak zamanı doğrulanamıyor.")
    base = load_live_alpha_settings()
    if (
        card.get("policy_version") != base.policy_version
        or card.get("profile_sha256") != base.profile_sha256
        or data.get("policy_version") != base.policy_version
        or data.get("profile_sha256") != base.profile_sha256
    ):
        reasons.append("Kaynak politika / profil kimliği doğrulanamıyor.")
    direction = card.get("direction")
    if (
        direction not in ("up", "down")
        or card.get("opportunity_id") != f"{c.day}:{ticker}:{direction}"
    ):
        reasons.append("Fırsat bugünkü seansa ait değil.")
    raw_prices = data.get("price_observations", {})
    p = point(raw_prices.get(ticker), now, c)
    flow = data.get("flow_observations", {}).get(ticker, {})
    flow_at = timestamp(flow.get("last_ts"))
    if (
        not data.get("flow_current")
        or not fresh(flow_at, now, cfg.flow_max_age_seconds)
        or c.opening is None
        or flow_at is None
        or flow_at < c.opening
    ):
        reasons.append("Bu hisseye ait güncel seans içi opsiyon akışı bekleniyor.")
    news = data.get("news_observations", {}).get(ticker, {})
    news_at = timestamp(news.get("checked_at"))
    if news.get("state") not in ("checked_found", "checked_none") or not fresh(
        news_at, now, load_live_alpha_settings().news.refresh_seconds
    ):
        reasons.append("Başarılı ve güncel haber kontrolü bekleniyor.")
    plan = _plan(card, now, c)
    if plan is None:
        reasons.append("İşleme hazır, pozitif adetli risk/maliyet planı yok.")
    if p is None:
        reasons.append("Güncel seans içi dayanak fiyatı yok.")
    elif plan and (
        plan.entry != round(p[0], 2)
        or timestamp((card.get("stock_plan") or {}).get("entry_ref_at")) != p[1]
    ):
        reasons.append("Plan ile fiyat gözleminin zamanı/değeri eşleşmiyor.")
    if ticker in conflicts:
        reasons.append("Aynı hissede açık pozisyon çıkış incelemesi var; yeni giriş bekler.")
    source_at = p[1] if p else None
    expires = (
        min(
            utc(scan.started_at) + timedelta(seconds=cfg.source_max_age_seconds),
            source_at + timedelta(seconds=cfg.source_max_age_seconds),
        )
        if source_at
        else None
    )
    if not reasons and plan and plan.instrument != "stock":
        selected = next(o for o in card["options"] if o["kind"] == plan.instrument)
        for leg in selected["legs"]:
            quote_at = timestamp(leg["fetched_at"])
            assert quote_at is not None
            expires = min(
                expires or now, quote_at + timedelta(seconds=base.option_plan.quote_max_age_seconds)
            )
    if expires and c.closing and flow_at and news_at:
        expires = min(
            expires,
            c.closing,
            flow_at + timedelta(seconds=cfg.flow_max_age_seconds),
            news_at + timedelta(seconds=base.news.refresh_seconds),
        )
    if expires is None or expires <= now:
        reasons.append("Planın geçerli tazelik süresi doldu.")
    label = {
        "stock": "HİSSE AL",
        "long_call": "CALL AL",
        "bull_call_debit": "CALL SPREAD AL",
        "long_put": "PUT AL",
        "bear_put_debit": "PUT SPREAD AL",
    }.get(plan.instrument if plan else "", "BEKLE")
    evidence = (
        Evidence(
            name="Opsiyon akışı",
            text=str(card.get("option_evidence", "Gözlem yok")),
            source_at=flow_at,
        ),
        Evidence(
            name="Fiyat / SPY",
            text=str(card.get("price_evidence", "Gözlem yok")),
            source_at=source_at,
        ),
        Evidence(
            name="Haber", text=str(card.get("news_evidence", "Kontrol yok")), source_at=news_at
        ),
    )
    return Advice(
        ticker=ticker,
        action="WAIT" if reasons else "BUY",
        instrument=plan.instrument if plan else "none",
        direction=str(direction or "up"),
        headline=f"{ticker} · {'BEKLE' if reasons else label}",
        reasons=tuple(reasons)
        if reasons
        else (
            str(
                card.get("plain_reason")
                or card.get("why_today")
                or "Kaynak giriş koşulları ve açılış kontrolleri sağlandı."
            ),
        ),
        evidence=evidence,
        counter_argument=str(card.get("counter_argument", "")),
        invalidation=str(card.get("invalidation", "")),
        plan=plan,
        source_card_hash=digest(card),
        source_policy=str(card.get("policy_version", "")),
        source_profile_hash=str(card.get("profile_sha256", "")),
        source_at=source_at,
        expires_at=expires,
    )


def exits(rows: tuple[Position, ...], now: datetime, c: Clock) -> tuple[Advice, ...]:
    result = []
    for p in rows:
        if p.action != "EXIT_REVIEW":
            continue
        current = (
            c.opening is not None
            and c.closing is not None
            and c.opening <= now < c.closing
            and fresh(p.spot_at, now, settings().source_max_age_seconds)
            and p.spot_at is not None
            and p.spot_at >= c.opening
            and p.spot is not None
        )
        action = ("SELL" if p.scope == "manual" else "PAPER_EXIT") if current else "REVIEW"
        label = {
            "SELL": "KAYITLI POZİSYONU KAPAT / AZALT",
            "PAPER_EXIT": "SANAL ÇIKIŞ İNCELE",
            "REVIEW": "ÇIKIŞI İNCELE · TAZE VERİ BEKLE",
        }[action]
        result.append(
            Advice(
                ticker=p.ticker,
                action=action,
                scope=p.scope,
                position_id=p.id,
                instrument=p.instrument,
                direction=p.direction,
                headline=f"{p.ticker} · {label}",
                reasons=(p.reason,),
                source_at=p.spot_at,
                expires_at=p.spot_at + timedelta(seconds=settings().source_max_age_seconds)
                if current and p.spot_at
                else None,
                counter_argument="Yalnız günlükte kayıtlı pozisyon; broker bakiyesi ve gerçekleşen satış bilinmiyor."
                if p.scope == "manual"
                else "PAPER kaydıdır, gerçek pozisyon değildir.",
            )
        )
    return tuple(result)
