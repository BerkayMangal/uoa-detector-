"""Value types of the Live Alpha engine (contract §2, §3).

Everything here is frozen and JSON-serialisable through :func:`to_jsonable`, because
a recommendation is stored as an immutable record and the page renders the stored
JSON, never a re-computation.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Literal

Direction = Literal["up", "down"]


class Recommendation(StrEnum):
    BUY = "BUY"
    CONDITIONAL_BUY = "CONDITIONAL_BUY"
    WATCH = "WATCH"
    AVOID = "AVOID"
    BEARISH_SETUP = "BEARISH_SETUP"
    EXIT_REVIEW = "EXIT_REVIEW"


class Readiness(StrEnum):
    READY = "READY"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    QUOTE_PENDING = "QUOTE_PENDING"
    RISK_BLOCKED = "RISK_BLOCKED"
    INVALID = "INVALID"


class EvidenceStatus(StrEnum):
    EXPERIMENTAL_RULES = "EXPERIMENTAL_RULES"
    EXPLORATORY = "EXPLORATORY"
    OOS_SUPPORTED = "OOS_SUPPORTED"
    FORWARD_SUPPORTED = "FORWARD_SUPPORTED"


class Tracking(StrEnum):
    OBSERVED = "OBSERVED"
    PAPER_PENDING = "PAPER_PENDING"
    PAPER_OPEN = "PAPER_OPEN"
    EXIT_SIGNALLED = "EXIT_SIGNALLED"
    CLOSED_SIMULATED = "CLOSED_SIMULATED"
    MANUAL_FILL_RECORDED = "MANUAL_FILL_RECORDED"
    UNRESOLVED = "UNRESOLVED"


class CheckState(StrEnum):
    """What a data read produced. A failure is never neutral or supportive evidence."""

    NOT_CHECKED = "not_checked"
    FAILED = "failed"
    STALE = "stale"
    CHECKED_NONE = "checked_none"
    CHECKED_FOUND = "checked_found"


class Path(StrEnum):
    P1_NEWS_CONTINUATION = "P1_news_continuation"
    P2_MARKET_RELATIVE = "P2_market_relative_flow"
    P3_PULLBACK = "P3_pullback_entry"
    P4_BEARISH = "P4_bearish_setup"
    NONE = "none"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowPrint:
    """One stored flow print, as the engine needs it."""

    event_id: str
    ticker: str
    ts: datetime
    option_type: str          # "call" / "put"
    strike: Decimal
    expiry: date
    premium: Decimal
    fill_side: str | None     # alfa_print_meta.fill_side; None = legacy row without meta
    option_chain: str | None


@dataclass(frozen=True)
class FlowSummary:
    ticker: str
    run_id: str
    prints_raw: int
    prints_deduped: int
    premium_up: float
    premium_down: float
    premium_side_unknown: float
    distinct_contracts: int
    first_ts: datetime | None
    last_ts: datetime | None
    largest_premium: float
    largest_contract: str | None
    top_contracts: tuple[tuple[str, float, str], ...]   # (contract, premium, direction)
    strikes_seen: tuple[float, ...]

    @property
    def side_aware_premium(self) -> float:
        return self.premium_up + self.premium_down

    @property
    def total_premium(self) -> float:
        return self.side_aware_premium + self.premium_side_unknown

    def share(self, direction: Direction) -> float:
        total = self.side_aware_premium
        if total <= 0:
            return 0.0
        return (self.premium_up if direction == "up" else self.premium_down) / total

    @property
    def side_aware_share(self) -> float:
        total = self.total_premium
        return self.side_aware_premium / total if total > 0 else 0.0


@dataclass(frozen=True)
class PriceContext:
    ticker: str
    spot: float | None
    spot_fetched_at: datetime | None
    prev_close: float | None
    prev_close_day: date | None
    atr: float | None
    atr_sessions: int
    benchmark_move: float | None
    state: CheckState          # CHECKED_FOUND when spot, prev close and ATR are all present
    blocker: str = ""

    @property
    def move(self) -> float | None:
        if self.spot is None or self.prev_close is None or self.prev_close <= 0:
            return None
        return self.spot / self.prev_close - 1

    @property
    def move_atr(self) -> float | None:
        if self.spot is None or self.prev_close is None or not self.atr:
            return None
        return (self.spot - self.prev_close) / self.atr

    @property
    def relative(self) -> float | None:
        if self.move is None or self.benchmark_move is None:
            return None
        return self.move - self.benchmark_move


@dataclass(frozen=True)
class NewsItem:
    headline: str
    source: str
    provider_created_at: datetime
    first_seen_at: datetime
    sentiment: str | None
    is_major: bool
    tags: tuple[str, ...]
    tickers: tuple[str, ...]
    ref: str                  # our content hash; the provider gives no URL or id


@dataclass(frozen=True)
class NewsCheck:
    ticker: str
    state: CheckState
    checked_at: datetime | None
    items: tuple[NewsItem, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class OptionQuote:
    """A live contract quote from ``option-contracts`` (no exchange quote time exists)."""

    option_symbol: str
    underlying: str
    right: str                # "call" / "put"
    strike: Decimal
    expiry: date
    bid: Decimal | None
    ask: Decimal | None
    fetched_at: datetime
    in_session: bool          # fetched inside a LIVE session
    volume: int | None = None
    open_interest: int | None = None
    multiplier: int = 100


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StockPlan:
    direction: Direction
    entry_ref: float
    entry_ref_at: datetime | None
    chase_limit: float        # do not buy above (up) / below (down)
    entry_zone_low: float
    entry_zone_high: float
    stop: float
    target: float
    target_label: str
    horizon_sessions: int
    risk_per_share: float
    shares: int
    notional_usd: float
    planned_risk_usd: float
    r_usd: float
    note: str


@dataclass(frozen=True)
class OptionLegView:
    option_symbol: str
    right: str
    strike: float
    expiry: date
    is_long: bool
    bid: float | None
    ask: float | None
    fetched_at: datetime | None


@dataclass(frozen=True)
class OptionStructureView:
    kind: str                 # long_call / bull_call_debit / long_put / bear_put_debit
    readiness: Readiness
    legs: tuple[OptionLegView, ...]
    entry_debit: float | None = None
    exit_credit: float | None = None
    entry_cost_usd: float | None = None
    commission_usd: float | None = None
    max_loss_usd: float | None = None
    max_profit_usd: float | None = None
    breakeven: float | None = None
    roundtrip_cost_pct: float | None = None
    lots: int = 0
    multiplier: int = 100
    dte: int | None = None
    blocker: str = ""


@dataclass(frozen=True)
class InstrumentChoice:
    preferred: str            # "stock" / structure kind / "none"
    reason: str


@dataclass(frozen=True)
class Card:
    opportunity_id: str
    ticker: str
    direction: Direction
    recommendation: Recommendation
    readiness: Readiness
    evidence_status: EvidenceStatus
    path: Path
    market_mode: str
    decision_as_of: datetime
    headline: str             # the one-sentence view
    why_today: str
    option_evidence: str
    news_evidence: str
    price_evidence: str
    stock_plan: StockPlan | None
    stock_plan_text: str
    options: tuple[OptionStructureView, ...]
    option_text: str
    instrument: InstrumentChoice
    invalidation: str
    counter_argument: str
    validation_text: str
    blockers: tuple[str, ...]
    sources: tuple[str, ...]
    derived_from: tuple[str, ...]
    policy_version: str
    profile_sha256: str
    rank_key: tuple[int, float] = (0, 0.0)
    dimensions: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Plain JSON types for a frozen dataclass tree (Decimal→str, datetime→ISO)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    return value
