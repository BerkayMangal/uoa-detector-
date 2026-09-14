"""Phase 3.4.1.2 tests for DealerExposureAggregate + Protocol extension.

Pins:
  - DealerExposureAggregate frozen + extra=forbid
  - flip_strike: Decimal | None (None when curve doesn't cross zero)
  - DealerPositioningProvider.aggregate_for_ticker present on Protocol
  - NoOpDealerPositioningProvider returns None
  - UW provider _aggregate happy path: net gamma from the spot-exposure
    row + flip-strike identification from the strike rows
  - UW provider _aggregate empty rows → None
  - UW provider _aggregate malformed rows skipped
  - UW provider _aggregate single row → flip_strike=None (curve monotone)
  - UW provider _aggregate monotone-positive → flip_strike=None
  - UW provider _aggregate flip detected on sign change ascending
  - UW provider _aggregate flip detected on exact zero crossing

Phase 3.9.5 (D10): the ``_aggregate`` fixtures used a guessed per-strike
``{strike, net_gamma, as_of}`` shape from an endpoint that never existed.
They are rewritten to live-shaped rows: net gamma (USD per 1% move) from
``/spot-exposures`` ``gamma_per_one_percent_move_oi``, the flip from
``/greek-exposure/strike`` ``call_gex + put_gex``. The ``_find_flip_strike``
tests are unchanged.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    DealerPositioningProvider,
    NoOpDealerPositioningProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    _aggregate,
    _decode_spot_rows,
    _decode_strike_rows,
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
# UW provider _aggregate (pure, over decoded live-shaped rows)
# ---------------------------------------------------------------------------

_ET_DATE = date(2026, 9, 11)
_AT = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)


def _spot_row(time: str, gamma_usd: str) -> dict[str, Any]:
    """Live-shaped /spot-exposures row (trimmed to the fields read)."""
    return {
        "time": time,
        "ticker": "AAPL",
        "start_time": time,
        "price": "332.24",
        "gamma_per_one_percent_move_oi": gamma_usd,
    }


def _strike_row(strike: str, call_gex: str, put_gex: str) -> dict[str, Any]:
    """Live-shaped /greek-exposure/strike row (trimmed to the fields read)."""
    return {
        "date": "2026-09-11",
        "strike": strike,
        "call_gex": call_gex,
        "put_gex": put_gex,
    }


def _agg(
    spot_rows: list[dict[str, Any]],
    strike_rows: list[dict[str, Any]],
    *,
    ticker: str = "AAPL",
) -> DealerExposureAggregate | None:
    return _aggregate(
        ticker=ticker,
        at=_AT,
        spot=_decode_spot_rows(spot_rows),
        strikes=_decode_strike_rows(strike_rows, et_date=_ET_DATE),
    )


def test_uw_aggregate_empty_rows_returns_none() -> None:
    assert _agg([], []) is None


def test_uw_aggregate_happy_path() -> None:
    """Net gamma from the spot-exposure row; flip from the strike rows."""
    spot = [_spot_row("2026-09-11T19:59:59.000000Z", "-16707476323.88")]
    strikes = [
        _strike_row("140", "100", "0"),      # net 100, cumulative 100
        _strike_row("150", "80", "-30"),     # net 50, cumulative 150
        _strike_row("160", "20", "-220"),    # net -200, cumulative -50
    ]
    agg = _agg(spot, strikes, ticker="aapl")
    assert agg is not None
    assert agg.ticker == "AAPL"  # uppercased
    assert agg.net_gamma_dollars == Decimal("-16707476323.88")
    assert agg.flip_strike == Decimal("160")


def test_uw_aggregate_skips_malformed_row() -> None:
    """A row missing 'strike' (or 'time') is skipped; the rest still count."""
    spot = [
        {"ticker": "AAPL", "gamma_per_one_percent_move_oi": "999"},  # bad
        _spot_row("2026-09-11T19:59:59.000000Z", "3740943365.4"),
    ]
    strikes = [
        _strike_row("150", "150", "-50"),                     # net 100
        {"date": "2026-09-11", "call_gex": "50", "put_gex": "0"},  # bad
        _strike_row("160", "0", "-200"),                      # net -200
    ]
    agg = _agg(spot, strikes)
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("3740943365.4")
    assert agg.flip_strike == Decimal("160")  # 100 → -100; bad row ignored


def test_uw_aggregate_all_malformed_returns_none() -> None:
    spot = [
        {"time": "bad", "gamma_per_one_percent_move_oi": "100"},
        {"weird": "field"},
    ]
    strikes = [{"strike": "garbage", "call_gex": "1", "put_gex": "0"}]
    assert _agg(spot, strikes) is None


def test_uw_aggregate_uses_latest_as_of_across_rows() -> None:
    spot = [
        _spot_row("2026-09-11T14:00:58.000000Z", "100"),
        _spot_row("2026-09-11T19:30:58.000000Z", "50"),
        _spot_row("2026-09-11T17:00:58.000000Z", "-200"),
    ]
    agg = _agg(spot, [])
    assert agg is not None
    # Latest at or before _AT (20:00Z) is 19:30:58, whatever the row order
    assert agg.as_of == datetime(2026, 9, 11, 19, 30, 58, tzinfo=UTC)
    assert agg.net_gamma_dollars == Decimal("50")


def test_uw_aggregate_monotone_curve_flip_strike_none() -> None:
    """All-positive cumulative ⇒ flip_strike=None."""
    spot = [_spot_row("2026-09-11T19:59:59.000000Z", "300")]
    strikes = [
        _strike_row("140", "150", "-50"),
        _strike_row("150", "250", "-50"),
    ]
    agg = _agg(spot, strikes)
    assert agg is not None
    assert agg.flip_strike is None
    assert agg.net_gamma_dollars == Decimal("300")
