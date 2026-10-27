"""Withdraw presentation when a newer cached price breaks the frozen entry plan."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.flow import utc
from webapp.desk.positions import number
from webapp.live_alpha.inputs import SpotRead


def reason(card: dict[str, Any], spot: SpotRead | None, now: datetime, cache_present: bool) -> str:
    if card.get("recommendation") != "BUY" or card.get("readiness") != "READY":
        return ""
    if not cache_present:
        return ""  # legacy snapshot-only installation: existing time/session guard still applies
    cfg = load_live_alpha_settings()
    day = utc(now).astimezone(ZoneInfo(cfg.calendar.timezone)).date()
    if spot is None or spot.spot is None or spot.trade_date != day or not 0 <= (utc(now) - utc(spot.fetched_at)).total_seconds() <= cfg.price.spot_max_age_seconds:
        return "Güncel seans fiyatı doğrulanamadı; giriş geri çekildi."
    plan = card.get("stock_plan") or {}
    stop, chase = number(plan.get("stop")), number(plan.get("chase_limit"))
    if stop is not None and spot.spot <= stop:
        return "Son fiyat plandaki stopa ulaştı; önceki ALIM girişi geri çekildi."
    if chase is not None and spot.spot > chase:
        return "Son fiyat giriş sınırını aştı; önceki ALIM girişi geri çekildi."
    return ""
