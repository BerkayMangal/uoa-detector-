"""Decision rules for an independent prospective stock PAPER comparison.

The initial plan fixes the entry range and absolute target for every arm. All
execution prices come from sampled quotes. Session gaps remain observable gaps.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from math import floor
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# The source Live Alpha profile uses this multiplier. At this width the model
# preserves the published stock plan exactly, including its whole-share size.
SOURCE_STOP_ATR = 1.5
_ET = ZoneInfo("America/New_York")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Arm(_Frozen):
    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    confirm_entry: bool
    stop_atr: float = Field(gt=0)


class Seed(_Frozen):
    opportunity_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    session: date
    published_at: AwareDatetime
    source_scan_id: str = Field(min_length=1)
    rec_id: str = Field(min_length=1)
    source_path: str
    headline: str
    reference: float = Field(gt=0)
    stop: float
    target: float = Field(gt=0)
    chase: float = Field(gt=0)
    atr: float = Field(gt=0)
    shares: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered_plan(self) -> Seed:
        if not self.stop < self.reference < self.target or self.chase < self.reference:
            raise ValueError("inconsistent initial stock plan")
        return self


class Quote(_Frozen):
    at: AwareDatetime
    price: float = Field(gt=0, strict=True)


class Tick(_Frozen):
    at: AwareDatetime
    session: date
    market_open: bool
    quote: Quote | None
    entry_check: str
    opposite_flow: bool
    horizon_reached: bool
    horizon_missed: bool = False


class Rules(_Frozen):
    risk_usd: float = Field(gt=0)
    max_notional_usd: float = Field(gt=0)
    slippage_bps: float = Field(ge=0, lt=10_000)
    max_quote_age_seconds: int = Field(gt=0)


class State(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    CLOSED = "closed"
    UNFILLED = "unfilled"
    UNKNOWN = "unknown"


class Position(_Frozen):
    arm_key: str
    state: State
    stop: float
    target: float
    quantity: int
    planned_risk: float
    entry_at: AwareDatetime | None = None
    entry_quote: Quote | None = None
    entry_fill: float | None = None
    fill_risk: float | None = None
    exit_signal_at: AwareDatetime | None = None
    exit_signal_session: date | None = None
    exit_at: AwareDatetime | None = None
    exit_quote: Quote | None = None
    exit_fill: float | None = None
    exit_reason: str | None = None
    pnl_usd: float | None = None
    net_r: float | None = None
    mae_r: float | None = None
    mfe_r: float | None = None
    last_quote: Quote | None = None


class Step(_Frozen):
    position: Position
    action: str
    reason: str


def start(seed: Seed, arm: Arm, rules: Rules) -> Position:
    """Freeze one entry reference and target while resizing for stop risk."""
    original_width = arm.stop_atr == SOURCE_STOP_ATR
    raw_risk = arm.stop_atr * seed.atr
    stop = seed.stop if original_width else round(seed.reference - raw_risk, 2)
    plan_risk = round(seed.reference - stop, 8)
    shares = 0
    if plan_risk > 0:
        # Honor the risk ceiling at the stored stop, including source rounding.
        risk_per_share = plan_risk if original_width else max(raw_risk, plan_risk)
        shares = min(floor(rules.risk_usd / risk_per_share), floor(rules.max_notional_usd / seed.reference))
        if original_width:
            shares = min(shares, seed.shares)
    return Position(
        arm_key=arm.key,
        state=State.PENDING if shares else State.UNFILLED,
        stop=stop,
        target=seed.target,
        quantity=shares,
        planned_risk=round(plan_risk * shares, 6),
        exit_reason=None if shares else "risk_blocked",
    )


def _quote(position: Position, tick: Tick, rules: Rules) -> Quote | None:
    quote = tick.quote
    if not tick.market_open or quote is None:
        return None
    if (quote.at.astimezone(_ET).date() != tick.session
            or not 0 <= (tick.at - quote.at).total_seconds() <= rules.max_quote_age_seconds):
        return None
    if position.entry_at is not None and quote.at <= position.entry_at:
        return None
    last = position.last_quote
    if last is not None and (quote.at < last.at or (quote.at == last.at and quote.price != last.price)):
        return None
    return quote


def _fill(quote: Quote, rules: Rules, *, buying: bool) -> float:
    slip = rules.slippage_bps / 10_000
    return round(quote.price * (1 + slip if buying else 1 - slip), 4)


def _pending(seed: Seed, arm: Arm, rules: Rules, position: Position, tick: Tick, quote: Quote | None) -> Step:
    if tick.session > seed.session:
        unfilled = position.model_copy(update={"state": State.UNFILLED, "exit_reason": "session_expired"})
        return Step(position=unfilled, action="unfilled", reason="session_expired")
    if not tick.market_open or tick.session != seed.session:
        return Step(position=position, action="waiting", reason="market_closed")
    if quote is None:
        return Step(position=position, action="waiting", reason="quote_unavailable")
    if quote.at <= seed.published_at:
        return Step(position=position, action="waiting", reason="quote_prepublication")
    observed = position.model_copy(update={"last_quote": quote})
    if not seed.stop < quote.price <= seed.chase:
        return Step(position=observed, action="waiting", reason="entry_price_outside_plan")
    if arm.confirm_entry and tick.entry_check != "ready":
        reason = {
            "watch": "entry_watch", "missing": "entry_missing",
        }.get(tick.entry_check, "entry_other")
        return Step(position=observed, action="waiting", reason=reason)
    entry = _fill(quote, rules, buying=True)
    opened = observed.model_copy(update={
        "state": State.OPEN, "entry_at": tick.at, "entry_quote": quote, "entry_fill": entry,
        "mae_r": 0.0, "mfe_r": 0.0, "fill_risk": round((entry - position.stop) * position.quantity, 6),
    })
    marked = _mark(opened, quote, rules)
    return Step(position=marked, action="filled", reason="entry_confirmed" if arm.confirm_entry else "entry_legacy")


def _quoted_pnl(position: Position, quote: Quote, rules: Rules) -> tuple[float, float]:
    assert position.entry_fill is not None
    exit_fill = _fill(quote, rules, buying=False)
    return exit_fill, (exit_fill - position.entry_fill) * position.quantity


def _mark(position: Position, quote: Quote, rules: Rules) -> Position:
    _, pnl = _quoted_pnl(position, quote, rules)
    r = round(pnl / position.planned_risk, 6)
    return position.model_copy(update={
        "last_quote": quote, "mae_r": min(position.mae_r or 0.0, r), "mfe_r": max(position.mfe_r or 0.0, r),
    })


def _closed(position: Position, tick: Tick, quote: Quote, reason: str, rules: Rules) -> Step:
    exit_fill, pnl = _quoted_pnl(position, quote, rules)
    priced = _mark(position, quote, rules).model_copy(update={
        "state": State.CLOSED, "exit_at": tick.at, "exit_quote": quote, "exit_fill": exit_fill,
        "exit_reason": reason, "pnl_usd": round(pnl, 2), "net_r": round(pnl / position.planned_risk, 6),
    })
    return Step(position=priced, action="closed", reason=reason)


def _exit_reason(position: Position, tick: Tick, quote: Quote | None) -> str | None:
    if quote is not None:
        if quote.price <= position.stop:
            return "stop"
        if quote.price >= position.target:
            return "target"
    if tick.market_open and tick.opposite_flow:
        return "opposite_flow"
    if tick.horizon_reached:
        return "horizon"
    return None


def advance(seed: Seed, arm: Arm, rules: Rules, position: Position, tick: Tick) -> Step:
    """Apply a single as-of observation without retiming an unpriced event."""
    if arm.key != position.arm_key:
        raise ValueError("position belongs to a different arm")
    if position.state in (State.CLOSED, State.UNFILLED, State.UNKNOWN):
        return Step(position=position, action="noop", reason="terminal")
    cutoff = position.exit_signal_at or position.entry_at or seed.published_at
    if tick.at <= cutoff or tick.session < seed.session:
        return Step(position=position, action="noop", reason="nonforward_observation")
    quote = _quote(position, tick, rules)
    if position.state is State.PENDING:
        return _pending(seed, arm, rules, position, tick, quote)

    if position.state is State.EXIT_PENDING:
        assert position.exit_signal_at is not None and position.exit_signal_session is not None
        assert position.exit_reason is not None
        if tick.session > position.exit_signal_session:
            unknown = position.model_copy(update={"state": State.UNKNOWN})
            return Step(position=unknown, action="unknown", reason="exit_quote_unavailable")
        if quote is not None and quote.at > position.exit_signal_at:
            return _closed(position, tick, quote, position.exit_reason, rules)
        return Step(position=position, action="waiting", reason="exit_quote_unavailable")

    if tick.horizon_missed:
        missed = "horizon_session_unobserved"
        unknown = position.model_copy(update={"state": State.UNKNOWN, "exit_reason": missed})
        return Step(position=unknown, action="unknown", reason=missed)
    reason = _exit_reason(position, tick, quote)
    if reason is not None:
        signalled = position.model_copy(update={
            "exit_reason": reason, "exit_signal_at": tick.at, "exit_signal_session": tick.session,
        })
        if quote is not None:
            return _closed(signalled, tick, quote, reason, rules)
        return Step(position=signalled.model_copy(update={"state": State.EXIT_PENDING}),
                    action="exit_pending", reason=reason)
    if quote is None:
        return Step(position=position, action="waiting", reason="quote_unavailable")
    if quote == position.last_quote:
        return Step(position=position, action="waiting", reason="quote_unchanged")
    return Step(position=_mark(position, quote, rules), action="marked", reason="quote_observed")
