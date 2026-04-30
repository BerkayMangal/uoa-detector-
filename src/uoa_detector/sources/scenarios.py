"""Hand-crafted synthetic scenarios for the CLI smoke run and integration test.

The default scenario is a sequence of ``OptionsPrint`` plus pre-computed sub-score
overrides. Because the Phase 1 stages are stubs, we can't yet drive the labeler
into specific labels by feeding raw market data — instead we provide a thin
override layer that the synthetic source applies as it emits. Phase 2 swaps this
for genuine sub-score derivation in the stages themselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext


@dataclass(frozen=True)
class ScenarioStep:
    """One scenario step: a print plus expected outcomes for assertions.

    Sub-score overrides are applied by the scenario harness in tests/CLI to
    pre-populate fields the stub stages can't yet compute. They mimic what
    the fully-implemented stages eventually produce.
    """

    print_: OptionsPrint
    expected_label: str
    overrides: dict[str, object]


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Helper: construct a UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_print(
    *,
    event_id: str,
    ts: datetime,
    ticker: str,
    option_type: Literal["call", "put"] = "call",
    strike: str = "200",
    dte: int = 14,
    spot: str = "198",
    premium: str = "250000",
    option_price: str = "1.50",
    iv: float = 0.45,
    bid: str = "1.45",
    ask: str = "1.55",
    fill_side: Literal[
        "above_ask", "at_ask", "midpoint", "at_bid", "below_bid", "unknown"
    ] = "above_ask",
    is_iso: bool = False,
    open_interest: int = 1500,
    exchange: str = "CBOE",
    source_id: str = "synthetic",
) -> OptionsPrint:
    expiry = ts.date() + timedelta(days=dte)
    return OptionsPrint(
        event_id=event_id,
        timestamp=ts,
        ticker=ticker,
        option_type=option_type,
        strike=Decimal(strike),
        expiry=expiry,
        dte=dte,
        spot_price=Decimal(spot),
        premium_paid=Decimal(premium),
        option_price=Decimal(option_price),
        implied_volatility=iv,
        bid=Decimal(bid),
        ask=Decimal(ask),
        fill_side=fill_side,
        exchange=exchange,
        is_iso=is_iso,
        open_interest=open_interest,
        source_agreement=single_source_agreement(source_id, exchange),
    )


# Reference timestamp: a Wednesday in prime session (15:30 UTC = 11:30 NY EDT) so
# the time-of-day weight is 1.00 by default. We bump to other windows where a
# scenario step needs it.
_BASE_TS = _utc(2025, 6, 11, 15, 30)


def default_scenario_prints() -> list[OptionsPrint]:
    """Return only the prints (used by the CLI; full overrides via ``default_scenario``)."""
    return [step.print_ for step in default_scenario()]


def default_scenario() -> list[ScenarioStep]:
    """The canonical scenario covering ≥ 8 distinct labels.

    Sequence (in emission order):
      1. IGNORE_NOISE              — neutral midpoint fill, low convexity
      2. CONVEXITY_WATCH           — convexity present, no cluster
      3. CONVEXITY_CLUSTER         — second print same strike/expiry
      4. STANDARD_UOA              — above-ask + relative_premium ≥ 0.5
      5. SWEEP_UOA                 — ISO-tagged sweep
      6. PENALIZED_BELOW_THRESHOLD — thin OI + wide spread + post-gap
      7. LEAP_POSITIONING          — DTE > 90 short-circuit
      8. HIGH_CONVICTION_SEQUENCE  — cluster + sweep + price confirmation
    """
    steps: list[ScenarioStep] = []

    # 1) IGNORE_NOISE
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-001",
                ts=_BASE_TS,
                ticker="AAPL",
                fill_side="midpoint",
            ),
            expected_label="IGNORE_NOISE",
            overrides={
                "convexity_score": 0.30,
                "uoa_score": 0.30,
                "relative_premium_score": 0.30,
                "cluster_density_score": 0.0,
            },
        )
    )

    # 2) CONVEXITY_WATCH — convexity present (≥0.6), no cluster yet, midpoint fill.
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-002",
                ts=_BASE_TS + timedelta(minutes=5),
                ticker="MSFT",
                fill_side="midpoint",
            ),
            expected_label="CONVEXITY_WATCH",
            overrides={
                "convexity_score": 0.75,
                "uoa_score": 0.40,
                "relative_premium_score": 0.30,
                "cluster_density_score": 0.0,
            },
        )
    )

    # 3) CONVEXITY_CLUSTER — second print same strike/expiry, midpoint (no UOA upgrade).
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-003",
                ts=_BASE_TS + timedelta(minutes=10),
                ticker="MSFT",
                fill_side="midpoint",
            ),
            expected_label="CONVEXITY_CLUSTER",
            overrides={
                "convexity_score": 0.75,
                "uoa_score": 0.40,
                "relative_premium_score": 0.30,
                "cluster_density_score": 0.5,  # ≥ _CLUSTER_MIN (0.4), < _CLUSTER_BURST (0.8)
            },
        )
    )

    # 4) STANDARD_UOA — above-ask + relative_premium ≥ 0.5, no cluster, no sweep.
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-004",
                ts=_BASE_TS + timedelta(minutes=15),
                ticker="NVDA",
                fill_side="above_ask",
                premium="800000",
            ),
            expected_label="STANDARD_UOA",
            overrides={
                "convexity_score": 0.55,
                "uoa_score": 0.65,
                "relative_premium_score": 0.6,  # ≥ 0.5 STANDARD_UOA threshold
                "cluster_density_score": 0.0,
            },
        )
    )

    # 5) SWEEP_UOA — ISO classified.
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-005",
                ts=_BASE_TS + timedelta(minutes=20),
                ticker="TSLA",
                fill_side="above_ask",
                premium="1500000",
                is_iso=True,
            ),
            expected_label="SWEEP_UOA",
            overrides={
                "convexity_score": 0.7,
                "uoa_score": 0.75,
                "relative_premium_score": 0.8,
                "cluster_density_score": 0.0,
            },
        )
    )

    # 6) PENALIZED_BELOW_THRESHOLD — pile up enough penalties to drop post-score < 0.30.
    # Thin OI (-0.20) + wide spread (-0.15) + post-gap (-0.20) + isolated (-0.10) = -0.65
    # All sub-scores low → pre-score around 0.25 → post around -0.40 → PENALIZED.
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-006",
                ts=_BASE_TS + timedelta(minutes=25),
                ticker="XYZ",
                strike="50",
                spot="49",
                premium="50000",
                option_price="0.20",
                bid="0.10",  # 0.30 mid, 0.20 spread → 66.7% of mid → wide
                ask="0.50",
                fill_side="above_ask",
                open_interest=20,  # < 100 → thin OI penalty
            ),
            expected_label="PENALIZED_BELOW_THRESHOLD",
            overrides={
                "convexity_score": 0.3,
                "uoa_score": 0.3,
                "relative_premium_score": 0.2,
                "cluster_density_score": 0.0,
                "is_post_gap": True,
                "is_isolated_print": True,
            },
        )
    )

    # 7) LEAP_POSITIONING — DTE > 90 short-circuits regardless of other state.
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-007",
                ts=_BASE_TS + timedelta(minutes=30),
                ticker="SPY",
                strike="500",
                spot="505",
                premium="2000000",
                option_price="15.00",
                bid="14.90",
                ask="15.10",
                dte=180,
                fill_side="above_ask",
            ),
            expected_label="LEAP_POSITIONING",
            overrides={
                "convexity_score": 0.6,
                "uoa_score": 0.7,
                "relative_premium_score": 0.7,
                "cluster_density_score": 0.0,
            },
        )
    )

    # 8) HIGH_CONVICTION_SEQUENCE — cluster + sweep/large UOA + price confirmation.
    steps.append(
        ScenarioStep(
            print_=_make_print(
                event_id="evt-008",
                ts=_BASE_TS + timedelta(minutes=35),
                ticker="GOOGL",
                strike="180",
                spot="178",
                premium="3000000",
                option_price="2.50",
                bid="2.45",
                ask="2.55",
                fill_side="above_ask",
                is_iso=True,
                open_interest=5000,
            ),
            expected_label="HIGH_CONVICTION_SEQUENCE",
            overrides={
                "convexity_score": 0.85,
                "uoa_score": 0.85,
                "relative_premium_score": 0.95,
                "cluster_density_score": 0.85,  # ≥ 0.4 cluster_present
                "has_price_confirmation": True,
                "price_direction": "up",
            },
        )
    )

    return steps


class ScenarioOverrideStage:
    """Pseudo-stage that applies scripted ``overrides`` from a scenario.

    Phase 1 only — exists so the Phase 1 stub stages don't fight against the
    expected labels we want to demonstrate. Runs FIRST in the Phase 1 demo
    pipeline; the stub stages then fill in only the fields it didn't touch
    (because each stub uses ``if event.<field> is None: ... = default``).
    """

    name = "phase1_scenario_overrides"

    def __init__(self, lookup: dict[str, dict[str, object]]) -> None:
        self._lookup = lookup

    async def enrich(
        self,
        event: EnrichedEvent,
        ctx: PipelineContext,
    ) -> EnrichedEvent:
        """Apply per-event overrides keyed by ``print_.event_id`` (ctx unused in Phase 1)."""
        del ctx  # explicitly mark unused
        overrides = self._lookup.get(event.print_.event_id, {})
        for k, v in overrides.items():
            setattr(event, k, v)
        return event


def overrides_lookup(steps: Iterator[ScenarioStep]) -> dict[str, dict[str, object]]:
    """Build an event_id → overrides lookup for ``ScenarioOverrideStage``."""
    return {s.print_.event_id: dict(s.overrides) for s in steps}
