"""Fixed sixty-minute stock reference for the first READY cohort."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from math import isfinite
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from uoa_detector.live_alpha.calendar import close_time
from webapp.paper_calibration.model import Quote

if TYPE_CHECKING:
    from uoa_detector.live_alpha.settings import CalendarSettings
    from webapp.paper_calibration.model import Seed
    from webapp.paper_calibration.settings import Settings

ENTRY_DELAY = timedelta(seconds=60)
HOLD_TIME = timedelta(minutes=60)
COSTS_BPS = (5, 10, 20)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Pair(_Frozen):
    stock: Quote
    spy: Quote


class Mark(_Frozen):
    pair: Pair
    scan_id: str
    observed_at: AwareDatetime
    recovered: bool

    @model_validator(mode="after")
    def _clock(self) -> Mark:
        if max(self.pair.stock.at, self.pair.spy.at) > self.observed_at:
            raise ValueError("quote follows the reference observation")
        return self


class Reference(_Frozen):
    state: Literal["entry_pending", "exit_pending", "priced", "unknown"] = "entry_pending"
    entry: Mark | None = None
    exit: Mark | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _state(self) -> Reference:
        if self.state in ("exit_pending", "priced") and self.entry is None:
            raise ValueError("the reference entry is missing")
        if self.state == "entry_pending" and self.entry is not None:
            raise ValueError("the reference entry is already present")
        if (self.state == "priced") != (self.exit is not None):
            raise ValueError("the reference exit is inconsistent")
        if (self.state == "unknown") != (self.reason is not None):
            raise ValueError("an unavailable reference needs a reason")
        if self.entry is not None and self.exit is not None:
            exit_due = self.entry.observed_at + HOLD_TIME
            if min(self.exit.pair.stock.at, self.exit.pair.spy.at) < exit_due:
                raise ValueError("the reference exit is early")
        return self

    @property
    def pending(self) -> bool:
        return self.state in ("entry_pending", "exit_pending")


def _rate(cost_bps: int) -> Decimal:
    return Decimal(cost_bps) / 10_000


def _paid(quote: Quote, cost_bps: int) -> Decimal:
    return Decimal(str(quote.price)) * (1 + _rate(cost_bps))


def net_usd(entry: Quote, exit_: Quote, notional: float, cost_bps: int) -> float:
    paid = _paid(entry, cost_bps)
    shares = (Decimal(str(notional)) / paid).to_integral_value(rounding=ROUND_FLOOR)
    received = Decimal(str(exit_.price)) * (1 - _rate(cost_bps))
    return float(shares * (received - paid))


def advance_reference(
    reference: Reference, seed: Seed, cfg: Settings, calendar: CalendarSettings,
    now: datetime, mark: Mark | None,
) -> Reference:
    if not reference.pending:
        return reference
    closing = datetime.combine(seed.session, close_time(seed.session, calendar), ZoneInfo(calendar.timezone))
    entry = reference.entry
    target = seed.published_at + ENTRY_DELAY if entry is None else entry.observed_at + HOLD_TIME
    deadline = min(closing, target + timedelta(seconds=cfg.markout_grace_seconds))
    if target >= closing:
        return Reference(state="unknown", entry=entry, reason="reference_outside_session")
    if now >= closing or now > deadline:
        reason = "reference_entry_missing" if entry is None else "reference_exit_missing"
        return Reference(state="unknown", entry=entry, reason=reason)
    if mark is None or mark.observed_at != now or min(mark.pair.stock.at, mark.pair.spy.at) < target:
        return reference
    if entry is not None:
        if not all(isfinite(net_usd(first, last, cfg.max_notional_usd, cost))
                   for first, last in ((entry.pair.stock, mark.pair.stock), (entry.pair.spy, mark.pair.spy))
                   for cost in COSTS_BPS):
            return Reference(state="unknown", entry=entry, reason="reference_invalid_return")
        return Reference(state="priced", entry=entry, exit=mark)
    budget = Decimal(str(cfg.max_notional_usd))
    if max(_paid(mark.pair.stock, COSTS_BPS[-1]), _paid(mark.pair.spy, COSTS_BPS[-1])) > budget:
        return Reference(state="unknown", reason="reference_notional")
    if now + HOLD_TIME >= closing:
        return Reference(state="unknown", entry=mark, reason="reference_outside_session")
    return Reference(state="exit_pending", entry=mark)
