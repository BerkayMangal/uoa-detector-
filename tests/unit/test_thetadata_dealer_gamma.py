"""Phase 3.6.1 — self-derived GEX provider tests.

Pins the dealer-gamma math (BS gamma, SqueezeMetrics sign convention,
zero-GEX flip), the point-in-time guard (source returns None ⇒ provider
None), and conformance to the DealerPositioningProvider Protocol M21
consumes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.providers.dealer_positioning import DealerPositioningProvider
from uoa_detector.sources.thetadata_derived.chain import (
    ChainAsOf,
    ChainContract,
)
from uoa_detector.sources.thetadata_derived.dealer_gamma import (
    ThetaDataDealerPositioningProvider,
    _bs_gamma,
)

_SNAP_DATE = date(2025, 7, 1)
_AT = datetime(2025, 7, 1, 15, 0, tzinfo=UTC)
_EXPIRY = date(2025, 8, 1)


class _FixedSource:
    """A ChainSnapshotSource returning one fixed chain (or None)."""

    def __init__(self, chain: ChainAsOf | None) -> None:
        self._chain = chain
        self.calls = 0

    def as_of(self, ticker: str, at: datetime) -> ChainAsOf | None:
        self.calls += 1
        return self._chain


def _c(
    strike: float, kind: str, oi: int, iv: float = 0.5,
) -> ChainContract:
    return ChainContract(
        strike=Decimal(str(strike)),
        option_type=kind,  # type: ignore[arg-type]
        expiry=_EXPIRY,
        open_interest=oi,
        implied_volatility=iv,
    )


def _chain(spot: float, contracts: tuple[ChainContract, ...]) -> ChainAsOf:
    return ChainAsOf(
        ticker="TSLA",
        snapshot_date=_SNAP_DATE,
        spot=Decimal(str(spot)),
        contracts=contracts,
    )


# ---------------------------------------------------------------------------
# BS gamma
# ---------------------------------------------------------------------------


def test_bs_gamma_atm_positive_and_peaks_near_atm() -> None:
    atm = _bs_gamma(100.0, 100.0, 0.1, 0.5)
    otm = _bs_gamma(100.0, 130.0, 0.1, 0.5)
    assert atm > 0.0
    assert atm > otm  # gamma peaks near the money


def test_bs_gamma_degenerate_inputs_zero() -> None:
    assert _bs_gamma(100.0, 100.0, 0.0, 0.5) == 0.0   # expired
    assert _bs_gamma(100.0, 100.0, 0.1, 0.0) == 0.0   # no vol
    assert _bs_gamma(0.0, 100.0, 0.1, 0.5) == 0.0     # no spot


# ---------------------------------------------------------------------------
# Sign convention: dealers long calls (+), short puts (−)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_heavy_chain_is_net_short() -> None:
    src = _FixedSource(_chain(100.0, (_c(100, "put", 5000),)))
    p = ThetaDataDealerPositioningProvider(src)
    agg = await p.aggregate_for_ticker("TSLA", _AT)
    assert agg is not None
    assert agg.net_gamma_dollars < 0  # puts → dealers net short


@pytest.mark.asyncio
async def test_call_heavy_chain_is_net_long() -> None:
    src = _FixedSource(_chain(100.0, (_c(100, "call", 5000),)))
    p = ThetaDataDealerPositioningProvider(src)
    agg = await p.aggregate_for_ticker("TSLA", _AT)
    assert agg is not None
    assert agg.net_gamma_dollars > 0


# ---------------------------------------------------------------------------
# Flip strike
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_found_between_call_and_put_concentrations() -> None:
    # Call OI low-strike, put OI high-strike: GEX is + near 80, − near 120,
    # so it must cross zero in between.
    src = _FixedSource(_chain(100.0, (
        _c(80, "call", 2000),
        _c(120, "put", 2000),
    )))
    p = ThetaDataDealerPositioningProvider(src)
    agg = await p.aggregate_for_ticker("TSLA", _AT)
    assert agg is not None
    assert agg.flip_strike is not None
    assert Decimal("80") < agg.flip_strike < Decimal("120")


@pytest.mark.asyncio
async def test_monotone_chain_has_no_flip() -> None:
    # All calls → GEX positive at every spot → no zero crossing.
    src = _FixedSource(_chain(100.0, (
        _c(90, "call", 1000),
        _c(100, "call", 1000),
        _c(110, "call", 1000),
    )))
    p = ThetaDataDealerPositioningProvider(src)
    agg = await p.aggregate_for_ticker("TSLA", _AT)
    assert agg is not None
    assert agg.flip_strike is None


# ---------------------------------------------------------------------------
# Point-in-time guard + edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_snapshot_returns_none() -> None:
    p = ThetaDataDealerPositioningProvider(_FixedSource(None))
    assert await p.aggregate_for_ticker("TSLA", _AT) is None


@pytest.mark.asyncio
async def test_empty_chain_returns_none() -> None:
    p = ThetaDataDealerPositioningProvider(_FixedSource(_chain(100.0, ())))
    assert await p.aggregate_for_ticker("TSLA", _AT) is None


@pytest.mark.asyncio
async def test_aggregate_as_of_reflects_query_instant() -> None:
    src = _FixedSource(_chain(100.0, (_c(100, "put", 1000),)))
    p = ThetaDataDealerPositioningProvider(src)
    agg = await p.aggregate_for_ticker("TSLA", _AT)
    assert agg is not None
    assert agg.as_of == _AT
    assert agg.ticker == "TSLA"


@pytest.mark.asyncio
async def test_gex_cached_per_ticker_day() -> None:
    src = _FixedSource(_chain(100.0, (_c(120, "put", 2000), _c(80, "call", 2000))))
    p = ThetaDataDealerPositioningProvider(src)
    later = datetime(2025, 7, 1, 19, 30, tzinfo=UTC)  # same snapshot day
    a = await p.aggregate_for_ticker("TSLA", _AT)
    b = await p.aggregate_for_ticker("TSLA", later)
    assert a is not None and b is not None
    # Same ticker-day → identical computed gamma/flip, reused from cache.
    assert a.net_gamma_dollars == b.net_gamma_dollars
    assert a.flip_strike == b.flip_strike
    assert len(p._cache) == 1


@pytest.mark.asyncio
async def test_net_gamma_at_strike() -> None:
    src = _FixedSource(_chain(100.0, (_c(100, "put", 3000), _c(110, "call", 1000))))
    p = ThetaDataDealerPositioningProvider(src)
    pos = await p.net_gamma_at("TSLA", Decimal("100"), _AT)
    assert pos is not None
    assert pos.net_gamma_dollars < 0  # the 100-strike leg is a put
    assert pos.strike == Decimal("100")
    # No contract at this strike → None.
    assert await p.net_gamma_at("TSLA", Decimal("999"), _AT) is None


def test_provider_satisfies_protocol() -> None:
    p = ThetaDataDealerPositioningProvider(_FixedSource(None))
    assert isinstance(p, DealerPositioningProvider)
