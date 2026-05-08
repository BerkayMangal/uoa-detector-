"""Phase 3.4.1.2 tests for DealerExposureAggregate + Protocol extension.

Pins:
  - DealerExposureAggregate frozen + extra=forbid
  - flip_strike: Decimal | None (None when curve doesn't cross zero)
  - DealerPositioningProvider.aggregate_for_ticker present on Protocol
  - NoOpDealerPositioningProvider returns None
  - UW provider _aggregate happy path: sum + flip-strike identification
  - UW provider _aggregate empty rows → None
  - UW provider _aggregate malformed rows skipped
  - UW provider _aggregate single row → flip_strike=None (curve monotone)
  - UW provider _aggregate monotone-positive → flip_strike=None
  - UW provider _aggregate flip detected on sign change ascending
  - UW provider _aggregate flip detected on exact zero crossing
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    DealerPositioningProvider,
    NoOpDealerPositioningProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
    _find_flip_strike,
)

# ---------------------------------------------------------------------------
# DTO shape
# ---------------------------------------------------------------------------


def test_aggregate_frozen() -> None:
    a = DealerExposureAggregate(
        ticker="AAPL",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        net_gamma_dollars=Decimal("-12345"),
        flip_strike=Decimal("150.00"),
    )
    with pytest.raises(Exception):
        a.ticker = "MSFT"  # type: ignore[misc]


def test_aggregate_extra_forbid() -> None:
    with pytest.raises(ValueError):
        DealerExposureAggregate(  # type: ignore[call-arg]
            ticker="AAPL",
            as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
            net_gamma_dollars=Decimal("0"),
            flip_strike=None,
            unknown_field=42,
        )


def test_aggregate_flip_strike_none_allowed() -> None:
    """When the gamma curve doesn't cross zero, flip_strike is None."""
    a = DealerExposureAggregate(
        ticker="AAPL",
        as_of=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        net_gamma_dollars=Decimal("100_000_000"),  # all positive
        flip_strike=None,
    )
    assert a.flip_strike is None


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_protocol_has_aggregate_method() -> None:
    """The DealerPositioningProvider Protocol declares aggregate_for_ticker."""
    assert hasattr(DealerPositioningProvider, "aggregate_for_ticker")


def test_noop_aggregate_returns_none() -> None:
    """NoOp returns None for both methods (M21 falls back to neutral)."""
    import asyncio
    p = NoOpDealerPositioningProvider()
    result = asyncio.run(p.aggregate_for_ticker(
        "AAPL",
        datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    ))
    assert result is None


def test_noop_satisfies_protocol() -> None:
    """NoOp implements the full Protocol surface."""
    p = NoOpDealerPositioningProvider()
    assert isinstance(p, DealerPositioningProvider)


# ---------------------------------------------------------------------------
# _find_flip_strike helper — pure function
# ---------------------------------------------------------------------------


def _decoded(*items: tuple[str, str]) -> list[tuple[Decimal, Decimal, datetime]]:
    """Build decoded rows: list of (strike, net_gamma, as_of) tuples."""
    as_of = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    return [(Decimal(s), Decimal(n), as_of) for s, n in items]


def test_flip_strike_empty_returns_none() -> None:
    assert _find_flip_strike([]) is None


def test_flip_strike_single_row_returns_none() -> None:
    assert _find_flip_strike(_decoded(("150", "1000"))) is None


def test_flip_strike_monotone_positive_returns_none() -> None:
    """All cumulative values positive → no crossing."""
    rows = _decoded(("140", "100"), ("150", "200"), ("160", "300"))
    assert _find_flip_strike(rows) is None


def test_flip_strike_monotone_negative_returns_none() -> None:
    rows = _decoded(("140", "-100"), ("150", "-200"), ("160", "-300"))
    assert _find_flip_strike(rows) is None


def test_flip_strike_detects_positive_to_negative_crossing() -> None:
    """Cumulative: 100, 100+50=150, 150-200=-50 — flip at strike 160."""
    rows = _decoded(("140", "100"), ("150", "50"), ("160", "-200"))
    assert _find_flip_strike(rows) == Decimal("160")


def test_flip_strike_detects_negative_to_positive_crossing() -> None:
    """Cumulative: -100, -100-50=-150, -150+300=150 — flip at strike 160."""
    rows = _decoded(("140", "-100"), ("150", "-50"), ("160", "300"))
    assert _find_flip_strike(rows) == Decimal("160")


def test_flip_strike_detects_exact_zero_crossing() -> None:
    """Cumulative reaches exactly zero → that strike is the flip."""
    # Cumulative: 100, 100-100 = 0 — flip at strike 150
    rows = _decoded(("140", "100"), ("150", "-100"))
    assert _find_flip_strike(rows) == Decimal("150")


def test_flip_strike_first_crossing_wins() -> None:
    """If the curve oscillates, return the first sign change."""
    # Cumulative: 100, 100+50=150, 150-300=-150, -150+50=-100
    # First crossing at 160 (150 → -150)
    rows = _decoded(
        ("140", "100"), ("150", "50"),
        ("160", "-300"), ("170", "50"),
    )
    assert _find_flip_strike(rows) == Decimal("160")


# ---------------------------------------------------------------------------
# UnusualWhalesDealerGammaProvider._aggregate
# ---------------------------------------------------------------------------


def _provider() -> UnusualWhalesDealerGammaProvider:
    """Build a provider with no real client — _aggregate doesn't use it."""
    from unittest.mock import MagicMock
    return UnusualWhalesDealerGammaProvider(
        client=MagicMock(),
        settings=MagicMock(cache_ttl=MagicMock(dealer_gamma_seconds=300)),
    )


def test_uw_aggregate_empty_rows_returns_none() -> None:
    p = _provider()
    assert p._aggregate([], ticker="AAPL") is None


def test_uw_aggregate_happy_path() -> None:
    """3 rows → sum + flip-strike identified."""
    p = _provider()
    rows = [
        {"strike": "140", "net_gamma": "100", "as_of": "2024-01-15T15:30:00Z"},
        {"strike": "150", "net_gamma": "50", "as_of": "2024-01-15T15:30:00Z"},
        {"strike": "160", "net_gamma": "-200", "as_of": "2024-01-15T15:30:00Z"},
    ]
    agg = p._aggregate(rows, ticker="aapl")
    assert agg is not None
    assert agg.ticker == "AAPL"  # uppercased
    assert agg.net_gamma_dollars == Decimal("-50")  # 100 + 50 - 200
    assert agg.flip_strike == Decimal("160")


def test_uw_aggregate_skips_malformed_row() -> None:
    """A row missing 'strike' is skipped; remaining rows still processed."""
    p = _provider()
    rows = [
        {"strike": "150", "net_gamma": "100", "as_of": "2024-01-15T15:30:00Z"},
        {"net_gamma": "50", "as_of": "2024-01-15T15:30:00Z"},  # bad
        {"strike": "160", "net_gamma": "-200", "as_of": "2024-01-15T15:30:00Z"},
    ]
    agg = p._aggregate(rows, ticker="AAPL")
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("-100")  # 100 - 200


def test_uw_aggregate_all_malformed_returns_none() -> None:
    p = _provider()
    rows = [
        {"strike": "garbage", "net_gamma": "100", "as_of": "bad"},
        {"weird": "field"},
    ]
    agg = p._aggregate(rows, ticker="AAPL")
    assert agg is None


def test_uw_aggregate_uses_latest_as_of_across_rows() -> None:
    p = _provider()
    rows = [
        {"strike": "140", "net_gamma": "100", "as_of": "2024-01-15T10:00:00Z"},
        {"strike": "150", "net_gamma": "50", "as_of": "2024-01-15T15:30:00Z"},
        {"strike": "160", "net_gamma": "-200", "as_of": "2024-01-15T13:00:00Z"},
    ]
    agg = p._aggregate(rows, ticker="AAPL")
    assert agg is not None
    # Latest is 15:30
    assert agg.as_of == datetime(2024, 1, 15, 15, 30, tzinfo=UTC)


def test_uw_aggregate_monotone_curve_flip_strike_none() -> None:
    """All-positive cumulative ⇒ flip_strike=None."""
    p = _provider()
    rows = [
        {"strike": "140", "net_gamma": "100", "as_of": "2024-01-15T15:30:00Z"},
        {"strike": "150", "net_gamma": "200", "as_of": "2024-01-15T15:30:00Z"},
    ]
    agg = p._aggregate(rows, ticker="AAPL")
    assert agg is not None
    assert agg.flip_strike is None
    assert agg.net_gamma_dollars == Decimal("300")
