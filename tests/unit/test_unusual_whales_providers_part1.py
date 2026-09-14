"""Phase 3.3.3.4 tests for the first three UW providers.

  - UnusualWhalesDealerGammaProvider
  - UnusualWhalesIVHistoryProvider
  - UnusualWhalesDarkPoolProvider

All tests use a ``_FakeClient`` that intercepts request_json and
returns canned responses — no real network. The smoke integration
tests (3.3.3.6) hit the real UW endpoint per provider.

Pins per provider:
  - Protocol conformance (runtime_checkable isinstance check)
  - Happy-path canned response → typed DTO
  - Cache hit on second identical call (one fetch, two reads)
  - Cache miss when ttl=0 (every call fetches)
  - Empty / malformed response → None or empty seq, no crash
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.providers.dark_pool import DarkPoolPrintProvider
from uoa_detector.providers.dealer_positioning import DealerPositioningProvider
from uoa_detector.providers.iv_history import IVHistoryProvider
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)

# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Captures (path, params) and returns scripted responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def stub(self, path: str, response: dict[str, Any]) -> None:
        self.responses[path] = response

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path in self.responses:
            return self.responses[path]
        return {"data": []}


def _settings(
    *,
    dealer_gamma_seconds: int = 300,
    iv_history_seconds: int = 600,
    dark_pool_seconds: int = 60,
) -> UnusualWhalesSettings:
    return UnusualWhalesSettings(
        cache_ttl=UnusualWhalesProviderCacheTTL(
            dealer_gamma_seconds=dealer_gamma_seconds,
            iv_history_seconds=iv_history_seconds,
            dark_pool_seconds=dark_pool_seconds,
        ),
    )


# ===========================================================================
# DealerGammaProvider
# ===========================================================================


def test_dealer_gamma_implements_protocol() -> None:
    """Runtime-checkable Protocol conformance."""
    client = _FakeClient()
    provider = UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, DealerPositioningProvider)


# Phase 3.9.5 (D10): these fixtures pinned a guessed
# ``/greek-exposure-strike`` path and a ``{strike, as_of, net_gamma,
# flow_direction}`` row shape; that path returned HTTP 404 live. They now
# use trimmed live rows of ``GET /api/stock/AAPL/greek-exposure/strike``
# (2026-09-11). UW publishes no flow direction, so the provider reports
# ``neutral``. Protocol, strike-match, caching and TTL=0 behaviour is kept.

_GEX_STRIKE_PATH = "/api/stock/AAPL/greek-exposure/strike"
_GEX_AT = datetime(2026, 9, 11, 15, 30, tzinfo=UTC)
_GEX_ROW_325: dict[str, Any] = {
    "date": "2026-09-11", "strike": "325", "call_gex": "329818.0540",
    "put_gex": "-132708.8386", "call_delta": "6182174.4550",
    "put_delta": "-1425130.6740", "call_charm": "7572612.1562",
    "put_charm": "3063665.8330", "call_vanna": "-1055985.2181",
    "put_vanna": "-541253.4954",
}
_GEX_ROW_330: dict[str, Any] = {
    "date": "2026-09-11", "strike": "330", "call_gex": "544970.2623",
    "put_gex": "-62284.1385", "call_delta": "8692332.9087",
    "put_delta": "-1620975.0500", "call_charm": "-14879958.8217",
    "put_charm": "-1232646.7571", "call_vanna": "752334.4483",
    "put_vanna": "-311991.4646",
}


@pytest.mark.asyncio
async def test_dealer_gamma_returns_positioning_for_known_strike() -> None:
    client = _FakeClient()
    client.stub(_GEX_STRIKE_PATH, {"data": [_GEX_ROW_325, _GEX_ROW_330]})
    provider = UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    pos = await provider.net_gamma_at(
        ticker="AAPL",
        strike=Decimal("325.00"),
        at=_GEX_AT,
    )
    assert pos is not None
    assert pos.ticker == "AAPL"
    assert pos.strike == Decimal("325.00")
    # call_gex + put_gex (share gamma; see the provider module docstring)
    assert pos.net_gamma_dollars == Decimal("197109.2154")
    assert pos.flow_direction == "neutral"


@pytest.mark.asyncio
async def test_dealer_gamma_returns_none_for_unknown_strike() -> None:
    client = _FakeClient()
    client.stub(_GEX_STRIKE_PATH, {"data": [_GEX_ROW_325]})
    provider = UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    pos = await provider.net_gamma_at(
        ticker="AAPL",
        strike=Decimal("999.99"),
        at=_GEX_AT,
    )
    assert pos is None


@pytest.mark.asyncio
async def test_dealer_gamma_caches_response() -> None:
    """Two calls for same ticker → one HTTP request."""
    client = _FakeClient()
    client.stub(_GEX_STRIKE_PATH, {"data": [_GEX_ROW_325]})
    provider = UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(dealer_gamma_seconds=300),
    )
    at = _GEX_AT
    await provider.net_gamma_at(
        ticker="AAPL", strike=Decimal("325.00"), at=at,
    )
    await provider.net_gamma_at(
        ticker="AAPL", strike=Decimal("325.00"), at=at,
    )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_dealer_gamma_cache_disabled_when_ttl_zero() -> None:
    client = _FakeClient()
    client.stub(_GEX_STRIKE_PATH, {"data": [_GEX_ROW_325]})
    provider = UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(dealer_gamma_seconds=0),
    )
    at = _GEX_AT
    for _ in range(3):
        await provider.net_gamma_at(
            ticker="AAPL", strike=Decimal("325.00"), at=at,
        )
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_dealer_gamma_unknown_flow_direction_falls_back_to_neutral() -> None:
    client = _FakeClient()
    client.stub(
        _GEX_STRIKE_PATH,
        {"data": [{**_GEX_ROW_325, "flow_direction": "wibble"}]},  # garbage
    )
    provider = UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    pos = await provider.net_gamma_at(
        ticker="AAPL",
        strike=Decimal("325.00"),
        at=_GEX_AT,
    )
    assert pos is not None
    assert pos.flow_direction == "neutral"


# ===========================================================================
# IVHistoryProvider
# ===========================================================================


def test_iv_history_implements_protocol() -> None:
    client = _FakeClient()
    provider = UnusualWhalesIVHistoryProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, IVHistoryProvider)


@pytest.mark.asyncio
async def test_iv_history_returns_nearest_snapshot() -> None:
    client = _FakeClient()
    expected_path = "/api/option-contract/AAPL240216C00150000/iv-rank"
    client.stub(
        expected_path,
        {
            "data": [
                {"as_of": "2024-01-15T15:00:00Z",
                 "implied_volatility": 0.40,
                 "iv_rank_252d": 60.0, "iv_percentile_252d": 65.0,
                 "iv_change_intraday_pct": 5.0},
                {"as_of": "2024-01-15T15:30:00Z",
                 "implied_volatility": 0.42,
                 "iv_rank_252d": 67.5, "iv_percentile_252d": 72.1,
                 "iv_change_intraday_pct": 12.3},
                {"as_of": "2024-01-15T16:00:00Z",
                 "implied_volatility": 0.45,
                 "iv_rank_252d": 70.0, "iv_percentile_252d": 75.0,
                 "iv_change_intraday_pct": 18.0},
            ],
        },
    )
    provider = UnusualWhalesIVHistoryProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    snap = await provider.iv_rank_at(
        ticker="AAPL",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        option_type="call",
        at=datetime(2024, 1, 15, 15, 32, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.implied_volatility == 0.42  # nearest is 15:30
    assert snap.iv_rank_252d == 67.5


@pytest.mark.asyncio
async def test_iv_history_returns_none_on_empty() -> None:
    client = _FakeClient()
    provider = UnusualWhalesIVHistoryProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    snap = await provider.iv_rank_at(
        ticker="AAPL", strike=Decimal("150.00"),
        expiry=date(2024, 2, 16), option_type="call",
        at=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    assert snap is None


@pytest.mark.asyncio
async def test_iv_history_caches_response() -> None:
    client = _FakeClient()
    expected_path = "/api/option-contract/AAPL240216C00150000/iv-rank"
    client.stub(
        expected_path,
        {"data": [{"as_of": "2024-01-15T15:30:00Z",
                    "implied_volatility": 0.42,
                    "iv_rank_252d": 67.5}]},
    )
    provider = UnusualWhalesIVHistoryProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    args: dict[str, Any] = {
        "ticker": "AAPL", "strike": Decimal("150.00"),
        "expiry": date(2024, 2, 16), "option_type": "call",
        "at": datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    }
    await provider.iv_rank_at(**args)
    await provider.iv_rank_at(**args)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_iv_history_put_uses_p_in_occ_symbol() -> None:
    """option_type='put' encodes as 'P' in the OCC symbol."""
    client = _FakeClient()
    provider = UnusualWhalesIVHistoryProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    await provider.iv_rank_at(
        ticker="AAPL", strike=Decimal("150.00"),
        expiry=date(2024, 2, 16), option_type="put",
        at=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
    )
    assert len(client.calls) == 1
    path = client.calls[0][0]
    assert "AAPL240216P00150000" in path


# ===========================================================================
# DarkPoolPrintProvider
# ===========================================================================


def test_dark_pool_implements_protocol() -> None:
    client = _FakeClient()
    provider = UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    assert isinstance(provider, DarkPoolPrintProvider)


@pytest.mark.asyncio
async def test_dark_pool_returns_prints_in_window() -> None:
    client = _FakeClient()
    client.stub(
        "/api/darkpool/AAPL/prints",
        {
            "data": [
                {"executed_at": "2024-01-15T14:25:30Z",
                 "price": "150.42", "size": 50000,
                 "side_estimate": "midpoint"},
                {"executed_at": "2024-01-15T14:55:00Z",
                 "price": "150.55", "size": 30000,
                 "side_estimate": "above_ask"},
                # Outside window:
                {"executed_at": "2024-01-15T13:00:00Z",
                 "price": "150.10", "size": 10000,
                 "side_estimate": "midpoint"},
            ],
        },
    )
    provider = UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    prints = await provider.recent_prints(
        ticker="AAPL",
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=45),
    )
    # Two prints within 14:15-15:00; the 13:00 one is outside.
    assert len(prints) == 2
    assert all(p.ticker == "AAPL" for p in prints)


@pytest.mark.asyncio
async def test_dark_pool_unknown_side_falls_back() -> None:
    client = _FakeClient()
    client.stub(
        "/api/darkpool/AAPL/prints",
        {"data": [{"executed_at": "2024-01-15T14:30:00Z",
                    "price": "150.0", "size": 100,
                    "side_estimate": "weird_value"}]},
    )
    provider = UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    prints = await provider.recent_prints(
        ticker="AAPL",
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=60),
    )
    assert len(prints) == 1
    assert prints[0].side_estimate == "unknown"


@pytest.mark.asyncio
async def test_dark_pool_caches_per_ticker() -> None:
    client = _FakeClient()
    client.stub(
        "/api/darkpool/AAPL/prints",
        {"data": []},
    )
    provider = UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    before = datetime(2024, 1, 15, 15, 0, tzinfo=UTC)
    await provider.recent_prints(
        ticker="AAPL", before=before, window=timedelta(minutes=10),
    )
    await provider.recent_prints(
        ticker="AAPL", before=before, window=timedelta(minutes=20),
    )
    # Same ticker → one fetch (window filtering happens in-memory).
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_dark_pool_handles_empty_response() -> None:
    client = _FakeClient()
    client.stub("/api/darkpool/AAPL/prints", {"data": []})
    provider = UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    prints = await provider.recent_prints(
        ticker="AAPL",
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=30),
    )
    assert prints == ()


@pytest.mark.asyncio
async def test_dark_pool_malformed_row_skipped() -> None:
    """Bad row dropped, good row emitted."""
    client = _FakeClient()
    client.stub(
        "/api/darkpool/AAPL/prints",
        {
            "data": [
                {"executed_at": "garbage_timestamp", "price": "1.0", "size": 1,
                 "side_estimate": "midpoint"},
                {"executed_at": "2024-01-15T14:30:00Z", "price": "150.0",
                 "size": 100, "side_estimate": "midpoint"},
            ],
        },
    )
    provider = UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )
    prints = await provider.recent_prints(
        ticker="AAPL",
        before=datetime(2024, 1, 15, 15, 0, tzinfo=UTC),
        window=timedelta(minutes=60),
    )
    assert len(prints) == 1
