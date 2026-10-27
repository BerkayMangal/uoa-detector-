"""Anchored European option scenarios, explicitly not forecasts or future NBBO."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.options_alpha.settings import load_settings as load_costs
from webapp.alpha_lab.flow import timestamp, utc
from webapp.alpha_lab.research import following_sessions
from webapp.desk.positions import number
from webapp.desk.settings import settings


def option_value(spot: float, strike: float, years: float, vol: float, rate: float, dividend: float, right: str) -> float:
    if spot <= 0 or strike <= 0 or vol < 0 or right not in ("call", "put") or not all(math.isfinite(v) for v in (spot, strike, years, vol, rate, dividend)):
        raise ValueError("invalid model inputs")
    sign = 1 if right == "call" else -1
    if years <= 0:
        return max(sign * (spot - strike), 0)
    s, k = spot * math.exp(-dividend * years), strike * math.exp(-rate * years)
    if vol <= 0:
        return max(sign * (s - k), 0)
    d1 = (math.log(spot / strike) + (rate - dividend + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)

    def normal(x: float) -> float:
        return (1 + math.erf(x / math.sqrt(2))) / 2

    return max(0, sign * (s * normal(sign * d1) - k * normal(sign * d2)))


def implied_vol(spot: float, strike: float, years: float, mid: float, right: str) -> float | None:
    cfg = settings()
    if years <= 0 or mid <= 0 or not math.isfinite(mid):
        return None
    low, high = cfg.scenario_iv_min, cfg.scenario_iv_max
    args = (cfg.scenario_rate, cfg.scenario_dividend_yield, right)
    if not option_value(spot, strike, years, low, *args) < mid < option_value(spot, strike, years, high, *args):
        return None
    for _ in range(cfg.scenario_solver_iterations):
        middle = (low + high) / 2
        if option_value(spot, strike, years, middle, *args) < mid:
            low = middle
        else:
            high = middle
    return (low + high) / 2


@dataclass(frozen=True)
class Case:
    kind: str
    sessions: int
    move_pct: float
    iv_points: float
    quantity: int
    pnl: float | None
    iv_basis: str


@dataclass(frozen=True)
class ScenarioView:
    cases: tuple[Case, ...]
    summary: tuple[Case, ...]
    detail: str


def build(card: dict[str, Any], now: datetime, withdrawn: bool = False) -> ScenarioView:
    """Existing persisted card JSON is the only untyped adapter input."""
    cfg, live, costs = settings(), load_live_alpha_settings(), load_costs()
    empty = ScenarioView((), (), "Güncel ve karşılaştırılabilir fiyatla senaryo henüz kurulamıyor.")
    sp = card.get("stock_plan")
    if withdrawn or not isinstance(sp, dict):
        return empty
    spot = number(card.get("spot_ref"))
    spot_at = timestamp(card.get("spot_ref_at"))
    if spot is None and card.get("recommendation") == "BUY":
        spot, spot_at = number(sp.get("entry_ref")), timestamp(sp.get("entry_ref_at"))
    if spot is None or spot <= 0 or spot_at is None or not 0 <= (utc(now) - spot_at).total_seconds() <= cfg.price_max_age_seconds:
        return empty
    day = utc(now).astimezone(ZoneInfo(live.calendar.timezone)).date()
    per_year = 365 * 86400
    slippage = live.stock_plan.slippage_bps / 10000
    latency = float(costs.costs.extra_slippage_pct) / 100
    cases = []
    for sessions in cfg.scenario_sessions:
        dates = following_sessions(day, sessions, live.calendar)
        if not dates:
            continue
        horizon = datetime.combine(dates[-1], live.calendar.regular_close, ZoneInfo(live.calendar.timezone))
        if dates[-1] in live.calendar.early_closes:
            horizon = datetime.combine(dates[-1], live.calendar.early_close, ZoneInfo(live.calendar.timezone))
        for shift in cfg.scenario_iv_shifts:
            for move in cfg.scenario_moves:
                future_spot = spot * (1 + move)
                qty = int(number(sp.get("shares")) or 0)
                if card.get("direction") == "up" and qty > 0:
                    cases.append(Case("stock", sessions, move * 100, shift * 100, qty,
                        (future_spot * (1 - slippage) - spot * (1 + slippage)) * qty, "Hisse; IV kullanılmaz"))
                for structure in card.get("options", []):
                    if structure.get("readiness") != "READY":
                        continue
                    entry = number(structure.get("entry_debit"))
                    lots = int(number(structure.get("lots")) or 0)
                    multiplier = number(structure.get("multiplier"))
                    fee = number(structure.get("commission_usd"))
                    legs = structure.get("legs", [])
                    if entry is None or entry <= 0 or lots < 1 or not multiplier or fee is None or not legs:
                        continue
                    credit = 0.0
                    sources = set()
                    available = True
                    for leg in legs:
                        bid, ask, strike = number(leg.get("bid")), number(leg.get("ask")), number(leg.get("strike"))
                        quote_at = timestamp(leg.get("fetched_at"))
                        try:
                            expiry = datetime.fromisoformat(str(leg.get("expiry"))).replace(hour=16, tzinfo=ZoneInfo(live.calendar.timezone))
                        except ValueError:
                            expiry = None
                        if (bid is None or ask is None or strike is None or bid <= 0 or ask < bid or strike <= 0
                            or quote_at is None or not 0 <= (utc(now) - quote_at).total_seconds() <= cfg.price_max_age_seconds
                            or expiry is None):
                            available = False
                            break
                        years = (expiry - utc(now)).total_seconds() / per_year
                        right = str(leg.get("right"))
                        mid, half_spread = (bid + ask) / 2, (ask - bid) / 2
                        vol, iv_at = number(leg.get("implied_volatility")), timestamp(leg.get("iv_at"))
                        if vol is not None and cfg.scenario_iv_min <= vol <= cfg.scenario_iv_max and iv_at is not None and 0 <= (utc(now) - iv_at).total_seconds() <= cfg.price_max_age_seconds:
                            sources.add("UW son işlem IV")
                        else:
                            vol = implied_vol(spot, strike, years, mid, right)
                            sources.add("Mid kotasyondan model IV")
                        if vol is None or not cfg.scenario_iv_min <= vol + shift <= cfg.scenario_iv_max:
                            available = False
                            break
                        remaining = (expiry - horizon).total_seconds() / per_year
                        if remaining <= 0:
                            future_mid = option_value(future_spot, strike, 0, vol, cfg.scenario_rate, cfg.scenario_dividend_yield, right)
                            leg_exit = future_mid  # intrinsic settlement, no hypothetical expired NBBO
                        else:
                            base_model = option_value(spot, strike, years, vol, cfg.scenario_rate, cfg.scenario_dividend_yield, right)
                            future_model = option_value(future_spot, strike, remaining, vol + shift, cfg.scenario_rate, cfg.scenario_dividend_yield, right)
                            future_mid = max(0, mid + future_model - base_model)
                            leg_exit = max(0, future_mid - half_spread) * (1 - latency) if leg.get("is_long") else (future_mid + half_spread) * (1 + latency)
                        credit += leg_exit * (1 if leg.get("is_long") else -1)
                    pnl = (credit - entry) * multiplier * lots - fee * lots if available else None
                    cases.append(Case(str(structure.get("kind")), sessions, move * 100, shift * 100,
                        lots, pnl, "; ".join(sorted(sources)) if available else "IV/fiyat model için yeterli değil"))
    return ScenarioView(tuple(cases), tuple(c for c in cases if c.sessions == 3 and c.iv_points == 0),
        "Mevcut kotasyondan varsayımsal giriş; sonraki seans kapanışları. Avrupa tipi model, %4 faiz, sıfır temettü; fiyat değişimi mevcut mid'e bağlanır. Gelecek makası bugünkü makas kabul edilir. Erken kullanım, temettü ve haber sıçramaları modellenmez. Olasılık veya dolum tahmini değildir.")
