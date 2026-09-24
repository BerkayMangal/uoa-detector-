"""Option alternative: contract discovery, pricing and instrument choice (contract §7).

Discovery. Candidate OCC symbols are built on several strike grids around the spot
and the stock plan's target, for one listed expiry. Unusual Whales drops a symbol
it does not know from the ``option-contracts`` answer, so only returned symbols
exist; nothing here assumes a strike is listed because it is round.

Pricing reuses ``options_alpha.structures.price_structure`` with the frozen cost
model of ``options_alpha_v1.yaml`` — the same arithmetic the research path used,
so the live card and the research verdicts cannot drift apart. A long leg opens at
the ask and closes at the bid, a short leg the reverse; the spread is not charged a
second time.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from uoa_detector.live_alpha.model import (
    Direction,
    InstrumentChoice,
    OptionLegView,
    OptionQuote,
    OptionStructureView,
    Readiness,
)
from uoa_detector.options_alpha.structures import (
    ContractQuote,
    Leg,
    Right,
    UnpriceableError,
    price_structure,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from uoa_detector.live_alpha.settings import OptionPlanSettings
    from uoa_detector.options_alpha.settings import OptionsAlphaSettings


def occ_symbol(ticker: str, expiry: date, right: str, strike: float) -> str:
    """OCC-style symbol as UW prints it, e.g. ``NVDA261016C00185000``."""
    cp = "C" if right == "call" else "P"
    thousandths = round(strike * 1000)
    return f"{ticker.upper()}{expiry:%y%m%d}{cp}{thousandths:08d}"


def pick_expiry(expiries: Sequence[date], today: date, settings: OptionPlanSettings) -> date | None:
    for exp in sorted(expiries):
        dte = (exp - today).days
        if settings.min_dte <= dte <= settings.max_dte:
            return exp
    return None


def candidate_strikes(spot: float, target: float, grid: Sequence[float], cap: int) -> list[float]:
    """Strikes near spot and target on each grid, nearest first, at most ``cap``."""
    found: dict[float, float] = {}
    for inc in grid:
        if inc <= 0:
            continue
        for anchor, span in ((spot, 2), (target, 1)):
            base = round(anchor / inc) * inc
            for k in range(-span, span + 1):
                strike = round(base + k * inc, 2)
                if strike > 0:
                    dist = min(abs(strike - spot), abs(strike - target))
                    found[strike] = min(found.get(strike, dist), dist)
    ordered = sorted(found, key=lambda s: (found[s], s))
    return sorted(ordered[:cap])


def candidate_symbols(
    ticker: str, expiry: date, right: str, spot: float, target: float, settings: OptionPlanSettings,
) -> list[str]:
    strikes = candidate_strikes(
        spot, target, settings.strike_grid_candidates, settings.max_symbols_per_request,
    )
    return [occ_symbol(ticker, expiry, right, s) for s in strikes]


def quote_blocker(q: OptionQuote, now: datetime, settings: OptionPlanSettings) -> str:
    """Why ``q`` cannot price an executable entry, or empty when it can."""
    if not q.in_session:
        return "kotasyon seans dışında alındı (son seansın NBBO'su)"
    if (now - q.fetched_at).total_seconds() > settings.quote_max_age_seconds:
        return "kotasyon bayat"
    if q.bid is None or q.ask is None:
        return "bugün işlem yok, NBBO boş"
    if q.ask < q.bid:
        return "çapraz kotasyon (ask < bid)"
    if q.bid <= 0 or q.ask <= 0:
        return "sıfır kotasyon"
    return ""


def _contract(q: OptionQuote) -> ContractQuote:
    return ContractQuote(
        option_symbol=q.option_symbol, underlying=q.underlying,
        right=Right.CALL if q.right == "call" else Right.PUT,
        strike=q.strike, expiry=q.expiry, bid=q.bid, ask=q.ask,
        as_of=q.fetched_at.date(), multiplier=q.multiplier,
        open_interest=q.open_interest, volume=q.volume,
    )


def _leg_view(q: OptionQuote, is_long: bool) -> OptionLegView:
    return OptionLegView(
        option_symbol=q.option_symbol, right=q.right, strike=float(q.strike), expiry=q.expiry,
        is_long=is_long, bid=float(q.bid) if q.bid is not None else None,
        ask=float(q.ask) if q.ask is not None else None, fetched_at=q.fetched_at,
    )


def price_view(
    kind: str,
    legs: Sequence[tuple[OptionQuote, bool]],
    now: datetime,
    today: date,
    settings: OptionPlanSettings,
    costs: OptionsAlphaSettings,
    r_usd: float,
) -> OptionStructureView:
    views = tuple(_leg_view(q, is_long) for q, is_long in legs)
    dte = (legs[0][0].expiry - today).days
    multiplier = legs[0][0].multiplier
    if len({q.multiplier for q, _ in legs}) > 1:
        return OptionStructureView(
            kind=kind, readiness=Readiness.INVALID, legs=views, dte=dte, multiplier=multiplier,
            blocker="bacakların çarpanı farklı",
        )
    blockers = [f"{q.option_symbol}: {b}" for q, _ in legs if (b := quote_blocker(q, now, settings))]
    if blockers:
        return OptionStructureView(
            kind=kind, readiness=Readiness.QUOTE_PENDING, legs=views, dte=dte,
            multiplier=multiplier, blocker="; ".join(blockers),
        )
    try:
        _kind, price = price_structure(
            tuple(Leg(quote=_contract(q), is_long=is_long) for q, is_long in legs), costs,
        )
    except UnpriceableError as exc:
        return OptionStructureView(
            kind=kind, readiness=Readiness.INVALID, legs=views, dte=dte, multiplier=multiplier,
            blocker=str(exc),
        )
    entry_cost = float(price.entry_cost_usd)
    roundtrip = (
        (float(price.entry_debit - price.exit_credit) * multiplier + float(price.commission_usd))
        / entry_cost * 100 if entry_cost > 0 else math.inf
    )
    max_loss = float(price.max_loss_usd)
    lots = math.floor(r_usd / max_loss) if max_loss > 0 else 0
    readiness = Readiness.READY if lots >= 1 else Readiness.RISK_BLOCKED
    blocker = "" if lots >= 1 else f"1 lot azami zarar ${max_loss:,.2f} > R ${r_usd:,.0f}"
    return OptionStructureView(
        kind=kind, readiness=readiness, legs=views,
        entry_debit=float(price.entry_debit), exit_credit=float(price.exit_credit),
        entry_cost_usd=entry_cost, commission_usd=float(price.commission_usd),
        max_loss_usd=max_loss,
        max_profit_usd=float(price.max_profit_usd) if price.max_profit_usd is not None else None,
        breakeven=float(price.breakeven_underlying) if price.breakeven_underlying is not None else None,
        roundtrip_cost_pct=round(roundtrip, 1), lots=lots, multiplier=multiplier, dte=dte,
        blocker=blocker,
    )


def build_structures(
    direction: Direction,
    quotes: Mapping[str, OptionQuote],
    spot: float,
    target: float,
    now: datetime,
    today: date,
    settings: OptionPlanSettings,
    costs: OptionsAlphaSettings,
    r_usd: float,
) -> tuple[OptionStructureView, ...]:
    """Single-leg and debit-spread views on the returned (= listed) contracts."""
    right = "call" if direction == "up" else "put"
    listed = sorted((q for q in quotes.values() if q.right == right), key=lambda q: q.strike)
    if not listed:
        return (OptionStructureView(
            kind="long_call" if right == "call" else "long_put", readiness=Readiness.INVALID,
            legs=(), blocker="UW bu vadede aday strike'lardan hiçbirini döndürmedi",
        ),)
    long_q = min(listed, key=lambda q: (abs(float(q.strike) - spot), float(q.strike)))
    single_kind = "long_call" if right == "call" else "long_put"
    views = [price_view(single_kind, [(long_q, True)], now, today, settings, costs, r_usd)]
    if right == "call":
        beyond = [q for q in listed if q.strike > long_q.strike]
    else:
        beyond = [q for q in listed if q.strike < long_q.strike]
    spread_kind = "bull_call_debit" if right == "call" else "bear_put_debit"
    if beyond:
        short_q = min(beyond, key=lambda q: (abs(float(q.strike) - target), float(q.strike)))
        views.append(price_view(
            spread_kind, [(long_q, True), (short_q, False)], now, today, settings, costs, r_usd,
        ))
    else:
        views.append(OptionStructureView(
            kind=spread_kind, readiness=Readiness.INVALID, legs=(_leg_view(long_q, True),),
            blocker="hedef tarafında listelenmiş kısa bacak bulunamadı",
        ))
    return tuple(views)


_KIND_TR = {
    "long_call": "long call",
    "long_put": "long put",
    "bull_call_debit": "bull call debit spread",
    "bear_put_debit": "bear put debit spread",
}


def kind_tr(kind: str) -> str:
    return _KIND_TR.get(kind, kind)


def choose_instrument(
    structures: Sequence[OptionStructureView],
    settings: OptionPlanSettings,
    *,
    stock_available: bool,
) -> InstrumentChoice:
    """The preference rule of contract §7, with the reason written out."""
    cost_cap = settings.max_roundtrip_cost_pct

    def _passes(v: OptionStructureView) -> bool:
        return (
            v.readiness is Readiness.READY
            and v.roundtrip_cost_pct is not None
            and v.roundtrip_cost_pct <= cost_cap
        )

    single = next((v for v in structures if v.kind in ("long_call", "long_put")), None)
    spread = next((v for v in structures if v.kind in ("bull_call_debit", "bear_put_debit")), None)
    if single is not None and _passes(single):
        return InstrumentChoice(
            preferred=single.kind,
            reason=(
                f"{kind_tr(single.kind)} bütçeye sığıyor ve gidiş-dönüş maliyeti "
                f"%{single.roundtrip_cost_pct:.0f} (≤ %{cost_cap:.0f}); risk ödenen primle sınırlı"
            ),
        )
    if spread is not None and _passes(spread):
        why_single = _why_not(single, cost_cap)
        return InstrumentChoice(
            preferred=spread.kind,
            reason=(
                f"{kind_tr(spread.kind)} tercih: gidiş-dönüş maliyeti %{spread.roundtrip_cost_pct:.0f}, "
                f"1 lot azami zarar ${spread.max_loss_usd:,.2f}; tek bacak uygun değil ({why_single})"
            ),
        )
    reasons = "; ".join(r for r in (_why_not(single, cost_cap), _why_not(spread, cost_cap)) if r)
    if stock_available:
        return InstrumentChoice(preferred="stock", reason=f"hisse tercih: opsiyon uygun değil ({reasons})")
    return InstrumentChoice(preferred="none", reason=f"uygulanabilir araç yok ({reasons})")


def _why_not(v: OptionStructureView | None, cost_cap: float) -> str:
    if v is None:
        return "yapı kurulamadı"
    if v.readiness is not Readiness.READY:
        return f"{kind_tr(v.kind)}: {v.readiness.value} — {v.blocker}"
    if v.roundtrip_cost_pct is None or v.roundtrip_cost_pct > cost_cap:
        return f"{kind_tr(v.kind)}: gidiş-dönüş maliyeti %{v.roundtrip_cost_pct:.0f} > %{cost_cap:.0f}"
    return ""


def to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    return d if d.is_finite() else None
