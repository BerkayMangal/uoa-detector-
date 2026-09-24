"""Numeric stock plan (contract §6). Pure arithmetic, checkable by hand.

Entry is the fresh spot; the chase limit is ``prev_close ± chase_max_atr × ATR``;
the stop is ``stop_atr × ATR`` beyond the entry; the target is ``target_r`` times
the risk, labelled as a policy level and never as a forecast. Shares come from the
labelled PAPER unit risk, capped by the PAPER notional ceiling.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from uoa_detector.live_alpha.model import Direction, PriceContext, StockPlan

if TYPE_CHECKING:
    from uoa_detector.live_alpha.settings import PriceSettings, StockPlanSettings

TARGET_LABEL = "politika hedefi ({r:g}R) — tahmin değil"


def chase_limit(direction: Direction, prev_close: float, atr: float, chase_max_atr: float) -> float:
    return prev_close + chase_max_atr * atr if direction == "up" else prev_close - chase_max_atr * atr


def build_stock_plan(
    direction: Direction,
    price: PriceContext,
    price_settings: PriceSettings,
    plan: StockPlanSettings,
    *,
    entry_override: float | None = None,
) -> StockPlan | None:
    """The plan around ``entry_override`` (a pullback level) or the current spot.

    None when spot, previous close or ATR is missing — no plan is ever built on a
    guessed level.
    """
    if price.spot is None or price.prev_close is None or not price.atr or price.atr <= 0:
        return None
    atr = price.atr
    limit = chase_limit(direction, price.prev_close, atr, price_settings.chase_max_atr)
    entry = entry_override if entry_override is not None else price.spot
    risk = plan.stop_atr * atr
    if direction == "up":
        stop = entry - risk
        target = entry + plan.target_r * risk
        zone_low, zone_high = min(price.prev_close, limit), limit
    else:
        stop = entry + risk
        target = entry - plan.target_r * risk
        zone_low, zone_high = limit, max(price.prev_close, limit)
    by_risk = math.floor(plan.r_usd / risk) if risk > 0 else 0
    by_cash = math.floor(plan.max_notional_usd / entry) if entry > 0 else 0
    shares = max(0, min(by_risk, by_cash))
    note = ""
    if shares == 0:
        note = f"1 hisse bile PAPER limitlerine sığmıyor (hisse başı risk ${risk:.2f}, fiyat ${entry:.2f})"
    elif by_cash < by_risk:
        note = f"adet nakit tavanıyla sınırlı (${plan.max_notional_usd:,.0f})"
    return StockPlan(
        direction=direction,
        entry_ref=round(entry, 2),
        entry_ref_at=price.spot_fetched_at if entry_override is None else None,
        chase_limit=round(limit, 2),
        entry_zone_low=round(zone_low, 2),
        entry_zone_high=round(zone_high, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        target_label=TARGET_LABEL.format(r=plan.target_r),
        horizon_sessions=plan.horizon_sessions,
        risk_per_share=round(risk, 2),
        shares=shares,
        notional_usd=round(shares * entry, 2),
        planned_risk_usd=round(shares * risk, 2),
        r_usd=plan.r_usd,
        note=note,
    )
