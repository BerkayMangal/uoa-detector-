"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from uoa_detector.config import AppConfig, default_config
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint


@pytest.fixture
def cfg() -> AppConfig:
    """Default v5 config."""
    return default_config()


@pytest.fixture
def base_ts() -> datetime:
    """Mid-prime-session UTC timestamp = 11:30 NY = weight 1.00."""
    return datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def build_print(
    *,
    event_id: str = "evt-test",
    ts: datetime | None = None,
    ticker: str = "AAPL",
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
) -> OptionsPrint:
    """Build a default options print for tests; override fields as needed."""
    if ts is None:
        ts = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)
    return OptionsPrint(
        event_id=event_id,
        timestamp=ts,
        ticker=ticker,
        option_type=option_type,
        strike=Decimal(strike),
        expiry=ts.date() + timedelta(days=dte),
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
    )


def build_enriched(
    *,
    print_: OptionsPrint | None = None,
    uoa: float = 0.5,
    convexity: float = 0.5,
    event_score: float = 0.5,
    gamma: float = 0.5,
    price: float = 0.5,
    sector: float = 0.5,
    tod: float = 1.0,
    cluster: float = 0.0,
    relative_premium: float = 0.5,
    dte_multiplier: float = 1.0,
    sweep: Literal["block", "sweep", "iso"] = "block",
    **extra: object,
) -> EnrichedEvent:
    """Build an enriched event with all sub-scores populated to known values."""
    if print_ is None:
        print_ = build_print()
    return EnrichedEvent(
        print=print_,
        uoa_score=uoa,
        convexity_score=convexity,
        event_score=event_score,
        gamma_score=gamma,
        price_confirmation_score=price,
        sector_confirmation_score=sector,
        time_of_day_weight=tod,
        cluster_density_score=cluster,
        relative_premium_score=relative_premium,
        dte_multiplier_applied=dte_multiplier,
        sweep_classification=sweep,
        **extra,
    )
