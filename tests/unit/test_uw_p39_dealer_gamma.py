"""Phase 3.9.5 tests for ``UnusualWhalesDealerGammaProvider`` (M21 data).

Contract: docs/phase-3.9-uw-endpoint-correction-acceptance.md §3.2.

Fixture rows are trimmed real responses captured live on 2026-09-14:
  - ``GET /api/stock/AAPL/spot-exposures?date=2026-09-11`` (per-minute rows)
  - ``GET /api/stock/SPY/spot-exposures?date=2026-09-10`` (last row)
  - ``GET /api/stock/AAPL/greek-exposure/strike?date=2026-09-11``

Pins:
  - net_gamma_dollars = ``gamma_per_one_percent_move_oi`` (USD per 1%) of
    the latest spot-exposure row with ``time <= at``; as_of = that time
  - rows after ``at`` are ignored and response order is not trusted
  - no row at or before ``at`` -> None, without calling the strike endpoint
  - both endpoints receive ``params={"date": <ET date of at>}``
  - a late-UTC-night event maps to the previous ET date
  - cache per (ticker, ET date): same date reuses, another date or ticker
    refetches; TTL 0 disables it
  - NotFound on spot-exposures -> None; NotFound or empty strike rows ->
    flip_strike None with net gamma kept
  - other client errors propagate and are not cached
  - flip_strike = ``_find_flip_strike`` over ``call_gex + put_gex`` by strike
  - net_gamma_at: ``call_gex + put_gex`` (share gamma) of the matching
    strike, as_of 00:00 ET of the row date, flow_direction neutral
  - strike rows dated after the ET date are dropped; mixed dates keep the
    newest; a row without ``date`` takes the requested date
  - malformed rows skipped; naive ``at`` raises ValueError
  - M21 wired to the real provider reads the USD/1% net gamma
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)

# ---------------------------------------------------------------------------
# Live-shaped fixtures (trimmed real rows, 2026-09-14 capture)
# ---------------------------------------------------------------------------

_AAPL_SPOT = "/api/stock/AAPL/spot-exposures"
_AAPL_STRIKE = "/api/stock/AAPL/greek-exposure/strike"
_SPY_SPOT = "/api/stock/SPY/spot-exposures"
_SPY_STRIKE = "/api/stock/SPY/greek-exposure/strike"
_DAY = "2026-09-11"

_SPOT_1030: dict[str, Any] = {
    "time": "2026-09-11T10:30:26.000000Z", "ticker": "AAPL",
    "start_time": "2026-09-11T10:30:00.000000Z", "price": "325.68",
    "ticker_id": 6274,
    "charm_per_one_percent_move_dir": "0",
    "charm_per_one_percent_move_oi": "-47049824470.56",
    "charm_per_one_percent_move_vol": "0",
    "gamma_per_one_percent_move_dir": "0",
    "gamma_per_one_percent_move_oi": "781047027.92",
    "gamma_per_one_percent_move_vol": "0",
    "vanna_per_one_percent_move_dir": "0",
    "vanna_per_one_percent_move_oi": "30576045.04",
    "vanna_per_one_percent_move_vol": "0",
}
_SPOT_1459: dict[str, Any] = {
    "time": "2026-09-11T14:59:58.000000Z", "ticker": "AAPL",
    "start_time": "2026-09-11T14:59:00.000000Z", "price": "335.04",
    "ticker_id": 6274,
    "charm_per_one_percent_move_dir": "31989738971.7",
    "charm_per_one_percent_move_oi": "-145532662781.72",
    "charm_per_one_percent_move_vol": "-371145657212.23",
    "gamma_per_one_percent_move_dir": "52371942.04",
    "gamma_per_one_percent_move_oi": "2347484289.53",
    "gamma_per_one_percent_move_vol": "5985905323.28",
    "vanna_per_one_percent_move_dir": "-1653269.88",
    "vanna_per_one_percent_move_oi": "64069034.77",
    "vanna_per_one_percent_move_vol": "13980521.59",
}
_SPOT_1500: dict[str, Any] = {
    "time": "2026-09-11T15:00:58.000000Z", "ticker": "AAPL",
    "start_time": "2026-09-11T15:00:00.000000Z", "price": "335.47",
    "ticker_id": 6274,
    "charm_per_one_percent_move_dir": "31978990257.8",
    "charm_per_one_percent_move_oi": "-133334213013.74",
    "charm_per_one_percent_move_vol": "-363934419895.51",
    "gamma_per_one_percent_move_dir": "52617740.85",
    "gamma_per_one_percent_move_oi": "2353669284.5",
    "gamma_per_one_percent_move_vol": "6104949858.75",
    "vanna_per_one_percent_move_dir": "-1632125.16",
    "vanna_per_one_percent_move_oi": "64628201.46",
    "vanna_per_one_percent_move_vol": "13616997.21",
}
_SPOT_1959: dict[str, Any] = {
    "time": "2026-09-11T19:59:59.000000Z", "ticker": "AAPL",
    "start_time": "2026-09-11T19:59:00.000000Z", "price": "332.24",
    "ticker_id": 6274,
    "charm_per_one_percent_move_dir": "-26885931557.77",
    "charm_per_one_percent_move_oi": "-9671648536751.4",
    "charm_per_one_percent_move_vol": "-504459569036264.64",
    "gamma_per_one_percent_move_dir": "-2598623831.81",
    "gamma_per_one_percent_move_oi": "3740943365.4",
    "gamma_per_one_percent_move_vol": "-1870428301.98",
    "vanna_per_one_percent_move_dir": "-945486.73",
    "vanna_per_one_percent_move_oi": "66914946.9",
    "vanna_per_one_percent_move_vol": "29843108.31",
}
_SPY_SPOT_LAST_0910: dict[str, Any] = {
    "time": "2026-09-10T20:00:58.000000Z", "ticker": "SPY",
    "start_time": "2026-09-10T20:00:00.000000Z", "price": "757.74",
    "ticker_id": 2447,
    "charm_per_one_percent_move_dir": "-22409117466282.06",
    "charm_per_one_percent_move_oi": "-11984080596413.69",
    "charm_per_one_percent_move_vol": "-1556997513712719.36",
    "gamma_per_one_percent_move_dir": "-11383441617.61",
    "gamma_per_one_percent_move_oi": "-16707476323.88",
    "gamma_per_one_percent_move_vol": "-96327298371.59",
    "vanna_per_one_percent_move_dir": "-6653804.61",
    "vanna_per_one_percent_move_oi": "643861891.58",
    "vanna_per_one_percent_move_vol": "352578702.24",
}

_STRIKE_5: dict[str, Any] = {
    "date": "2026-09-11", "strike": "5", "call_gex": "0.0102",
    "put_gex": "-0.0110", "call_delta": "19198.6743", "put_delta": "-0.9140",
    "call_charm": "29.9741", "put_charm": "46.9783",
    "call_vanna": "-8.5434", "put_vanna": "-487.4638",
}
_STRIKE_10: dict[str, Any] = {
    "date": "2026-09-11", "strike": "10", "call_gex": "0.0178",
    "put_gex": "-0.0037", "call_delta": "4797.5042", "put_delta": "-0.3558",
    "call_charm": "10.7218", "put_charm": "63.2192",
    "call_vanna": "9.6972", "put_vanna": "-710.1654",
}
_STRIKE_320: dict[str, Any] = {
    "date": "2026-09-11", "strike": "320", "call_gex": "213039.6983",
    "put_gex": "-117781.5590", "call_delta": "12357878.8395",
    "put_delta": "-2242719.1957", "call_charm": "23280011.2661",
    "put_charm": "13351309.8139", "call_vanna": "-4497631.6808",
    "put_vanna": "-1890696.6047",
}
_STRIKE_325: dict[str, Any] = {
    "date": "2026-09-11", "strike": "325", "call_gex": "329818.0540",
    "put_gex": "-132708.8386", "call_delta": "6182174.4550",
    "put_delta": "-1425130.6740", "call_charm": "7572612.1562",
    "put_charm": "3063665.8330", "call_vanna": "-1055985.2181",
    "put_vanna": "-541253.4954",
}
_STRIKE_330: dict[str, Any] = {
    "date": "2026-09-11", "strike": "330", "call_gex": "544970.2623",
    "put_gex": "-62284.1385", "call_delta": "8692332.9087",
    "put_delta": "-1620975.0500", "call_charm": "-14879958.8217",
    "put_charm": "-1232646.7571", "call_vanna": "752334.4483",
    "put_vanna": "-311991.4646",
}

# Near-ATM strikes only: every per-strike net is positive -> no crossing.
_ATM_STRIKES = [_STRIKE_320, _STRIKE_325, _STRIKE_330]


# ---------------------------------------------------------------------------
# Fake client + builders
# ---------------------------------------------------------------------------


class _FakeClient:
    """Serves canned responses keyed by (path, date param); records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._responses: dict[tuple[str, str], dict[str, Any] | Exception] = {}

    def stub(
        self, path: str, day: str, response: dict[str, Any] | Exception,
    ) -> None:
        self._responses[(path, day)] = response

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        day = str((params or {}).get("date"))
        response = self._responses.get((path, day), {"data": []})
        if isinstance(response, Exception):
            raise response
        return response


def _provider(
    client: _FakeClient, *, ttl_seconds: int = 300,
) -> UnusualWhalesDealerGammaProvider:
    return UnusualWhalesDealerGammaProvider(
        client=client,  # type: ignore[arg-type]
        settings=UnusualWhalesSettings(
            cache_ttl=UnusualWhalesProviderCacheTTL(
                dealer_gamma_seconds=ttl_seconds,
            ),
        ),
    )


def _aapl_client(
    *,
    spot_rows: list[dict[str, Any]] | None = None,
    strike_rows: list[dict[str, Any]] | None = None,
) -> _FakeClient:
    client = _FakeClient()
    client.stub(
        _AAPL_SPOT, _DAY,
        {"data": spot_rows if spot_rows is not None
         else [_SPOT_1959, _SPOT_1030, _SPOT_1500, _SPOT_1459]},  # shuffled
    )
    client.stub(
        _AAPL_STRIKE, _DAY,
        {"data": strike_rows if strike_rows is not None else _ATM_STRIKES},
    )
    return client


def _at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 11, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------
# aggregate_for_ticker: as-of selection on spot-exposures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_uses_latest_spot_row_at_or_before_at() -> None:
    """15:00:30Z picks the 14:59:58 row; the 15:00:58 row is after ``at``."""
    provider = _provider(_aapl_client())
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.ticker == "AAPL"
    assert agg.net_gamma_dollars == Decimal("2347484289.53")
    assert agg.as_of == datetime(2026, 9, 11, 14, 59, 58, tzinfo=UTC)


@pytest.mark.asyncio
async def test_aggregate_row_exactly_at_at_is_included() -> None:
    provider = _provider(_aapl_client())
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 58))
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("2353669284.5")
    assert agg.as_of == _at(15, 0, 58)


@pytest.mark.asyncio
async def test_aggregate_after_last_row_uses_last_row() -> None:
    provider = _provider(_aapl_client())
    agg = await provider.aggregate_for_ticker("AAPL", _at(21, 0))
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("3740943365.4")
    assert agg.as_of == _at(19, 59, 59)


@pytest.mark.asyncio
async def test_aggregate_before_first_snapshot_returns_none() -> None:
    """06:00 ET, before the first 06:30 ET row: None, strikes not requested."""
    client = _aapl_client()
    provider = _provider(client)
    assert await provider.aggregate_for_ticker("AAPL", _at(10, 0)) is None
    assert client.calls == [(_AAPL_SPOT, {"date": _DAY})]


@pytest.mark.asyncio
async def test_aggregate_sends_et_date_param_to_both_endpoints() -> None:
    client = _aapl_client()
    provider = _provider(client)
    agg = await provider.aggregate_for_ticker("aapl", _at(15, 0, 30))
    assert agg is not None
    assert agg.ticker == "AAPL"
    assert client.calls == [
        (_AAPL_SPOT, {"date": _DAY}),
        (_AAPL_STRIKE, {"date": _DAY}),
    ]


@pytest.mark.asyncio
async def test_late_utc_night_event_maps_to_previous_et_date() -> None:
    """02:30Z on 09-12 is 22:30 EDT on 09-11: date=2026-09-11, last row."""
    client = _aapl_client()
    provider = _provider(client)
    at = datetime(2026, 9, 12, 2, 30, tzinfo=UTC)
    agg = await provider.aggregate_for_ticker("AAPL", at)
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("3740943365.4")
    assert {params["date"] for _, params in client.calls if params} == {_DAY}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_is_per_ticker_and_et_date() -> None:
    client = _aapl_client()
    client.stub(_SPY_SPOT, "2026-09-10", {"data": [_SPY_SPOT_LAST_0910]})
    provider = _provider(client, ttl_seconds=300)

    first = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert first is not None
    assert len(client.calls) == 2

    # Same ticker, same ET date, later time: cached rows, as-of re-applied.
    later = await provider.aggregate_for_ticker("AAPL", _at(19, 30))
    assert later is not None
    assert later.net_gamma_dollars == Decimal("2353669284.5")
    assert len(client.calls) == 2

    # Different ET date refetches (nothing published -> None).
    other_day = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    assert await provider.aggregate_for_ticker("AAPL", other_day) is None
    assert client.calls[2] == (_AAPL_SPOT, {"date": "2026-09-12"})

    # Different ticker refetches.
    spy = await provider.aggregate_for_ticker(
        "SPY", datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
    )
    assert spy is not None
    assert spy.net_gamma_dollars == Decimal("-16707476323.88")
    assert client.calls[3:] == [
        (_SPY_SPOT, {"date": "2026-09-10"}),
        (_SPY_STRIKE, {"date": "2026-09-10"}),
    ]


@pytest.mark.asyncio
async def test_cache_disabled_when_ttl_zero() -> None:
    client = _aapl_client()
    provider = _provider(client, ttl_seconds=0)
    for _ in range(2):
        await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert len(client.calls) == 4


# ---------------------------------------------------------------------------
# NotFound and other errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 422])
async def test_spot_not_found_returns_none(status: int) -> None:
    client = _aapl_client()
    client.stub(
        _AAPL_SPOT, _DAY,
        UnusualWhalesNotFoundError(f"HTTP {status}", status_code=status),
    )
    provider = _provider(client)
    assert await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30)) is None
    assert client.calls == [(_AAPL_SPOT, {"date": _DAY})]


@pytest.mark.asyncio
async def test_strike_not_found_keeps_net_gamma_with_flip_none() -> None:
    client = _aapl_client(strike_rows=[_STRIKE_5, _STRIKE_10])
    client.stub(
        _AAPL_STRIKE, _DAY,
        UnusualWhalesNotFoundError("HTTP 404", status_code=404),
    )
    provider = _provider(client)
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("2347484289.53")
    assert agg.flip_strike is None


@pytest.mark.asyncio
async def test_empty_strike_rows_flip_none() -> None:
    provider = _provider(_aapl_client(strike_rows=[]))
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.flip_strike is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        UnusualWhalesAuthError("HTTP 401: invalid token"),
        UnusualWhalesRateLimitError("failed after 3 attempts (HTTP 429)"),
        UnusualWhalesTransientError("HTTP 503"),
        CircuitBreakerOpenError("circuit breaker is open"),
    ],
)
async def test_other_client_errors_propagate_and_are_not_cached(
    exc: Exception,
) -> None:
    """A bad key or an outage must never look like "no data" (§3.9, §4)."""
    client = _aapl_client()
    client.stub(_AAPL_SPOT, _DAY, exc)
    provider = _provider(client)
    with pytest.raises(type(exc)):
        await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))

    client.stub(_AAPL_SPOT, _DAY, {"data": [_SPOT_1459]})
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("2347484289.53")


@pytest.mark.asyncio
async def test_net_gamma_at_propagates_auth_error() -> None:
    client = _aapl_client()
    client.stub(_AAPL_STRIKE, _DAY, UnusualWhalesAuthError("HTTP 403"))
    provider = _provider(client)
    with pytest.raises(UnusualWhalesAuthError):
        await provider.net_gamma_at("AAPL", Decimal("325"), _at(15, 0))


# ---------------------------------------------------------------------------
# Flip strike and row decoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_strike_from_call_plus_put_gex_sorted_by_strike() -> None:
    """Rows out of order. Strike 5 nets -0.0008, strike 10 nets +0.0141:
    the cumulative first changes sign at 10 (the unchanged first-crossing
    rule, which lands on deep-OTM strikes of a full live chain)."""
    rows = [_STRIKE_325, _STRIKE_10, _STRIKE_330, _STRIKE_5, _STRIKE_320]
    provider = _provider(_aapl_client(strike_rows=rows))
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.flip_strike == Decimal("10")


@pytest.mark.asyncio
async def test_monotone_strike_curve_flip_none() -> None:
    provider = _provider(_aapl_client(strike_rows=_ATM_STRIKES))
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.flip_strike is None


@pytest.mark.asyncio
async def test_malformed_spot_rows_are_skipped() -> None:
    rows = [
        _SPOT_1459,
        {**_SPOT_1500, "gamma_per_one_percent_move_oi": None},
        {k: v for k, v in _SPOT_1959.items() if k != "time"},
        {**_SPOT_1959, "gamma_per_one_percent_move_oi": "garbage"},
        {**_SPOT_1959, "time": "2026-09-11T15:10:00Z",
         "gamma_per_one_percent_move_oi": "NaN"},
        {**_SPOT_1959, "time": "not-a-time"},
    ]
    provider = _provider(_aapl_client(spot_rows=rows))
    agg = await provider.aggregate_for_ticker("AAPL", _at(21, 0))
    assert agg is not None
    assert agg.net_gamma_dollars == Decimal("2347484289.53")
    assert agg.as_of == _at(14, 59, 58)


@pytest.mark.asyncio
async def test_all_spot_rows_malformed_returns_none() -> None:
    rows = [{"time": "bad", "gamma_per_one_percent_move_oi": "1"}, {"x": 1}]
    provider = _provider(_aapl_client(spot_rows=rows))
    assert await provider.aggregate_for_ticker("AAPL", _at(21, 0)) is None


@pytest.mark.asyncio
async def test_malformed_strike_rows_are_skipped() -> None:
    rows = [
        _STRIKE_5,
        {k: v for k, v in _STRIKE_325.items() if k != "put_gex"},
        {**_STRIKE_330, "strike": "garbage"},
        {**_STRIKE_320, "call_gex": None},
        _STRIKE_10,
    ]
    provider = _provider(_aapl_client(strike_rows=rows))
    agg = await provider.aggregate_for_ticker("AAPL", _at(15, 0, 30))
    assert agg is not None
    assert agg.flip_strike == Decimal("10")
    assert await provider.net_gamma_at("AAPL", Decimal("325"), _at(15)) is None


# ---------------------------------------------------------------------------
# net_gamma_at (per-strike share gamma)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_net_gamma_at_returns_call_plus_put_gex_for_strike() -> None:
    client = _aapl_client()
    provider = _provider(client)
    pos = await provider.net_gamma_at("aapl", Decimal("325.00"), _at(15, 0))
    assert pos is not None
    assert pos.ticker == "AAPL"
    assert pos.strike == Decimal("325")
    assert pos.net_gamma_dollars == Decimal("197109.2154")
    assert pos.as_of == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)  # 00:00 EDT
    assert pos.flow_direction == "neutral"
    assert client.calls == [(_AAPL_STRIKE, {"date": _DAY})]


@pytest.mark.asyncio
async def test_net_gamma_at_unknown_strike_returns_none() -> None:
    provider = _provider(_aapl_client())
    assert await provider.net_gamma_at("AAPL", Decimal("327.5"), _at(15)) is None


@pytest.mark.asyncio
async def test_net_gamma_at_not_found_returns_none() -> None:
    client = _aapl_client()
    client.stub(
        _AAPL_STRIKE, _DAY,
        UnusualWhalesNotFoundError("HTTP 422", status_code=422),
    )
    provider = _provider(client)
    assert await provider.net_gamma_at("AAPL", Decimal("325"), _at(15)) is None


@pytest.mark.asyncio
async def test_net_gamma_at_as_of_uses_est_offset_in_winter() -> None:
    client = _FakeClient()
    client.stub(
        _AAPL_STRIKE, "2026-01-15", {"data": [{**_STRIKE_325, "date": "2026-01-15"}]},
    )
    provider = _provider(client)
    pos = await provider.net_gamma_at(
        "AAPL", Decimal("325"), datetime(2026, 1, 15, 15, 0, tzinfo=UTC),
    )
    assert pos is not None
    assert pos.as_of == datetime(2026, 1, 15, 5, 0, tzinfo=UTC)  # 00:00 EST


@pytest.mark.asyncio
async def test_strike_rows_after_et_date_dropped_and_newest_date_kept() -> None:
    rows = [
        {**_STRIKE_320, "date": "2026-09-10"},  # older snapshot: superseded
        _STRIKE_325,  # the ET date's snapshot
        {**_STRIKE_330, "date": "2026-09-12"},  # after the ET date: dropped
    ]
    provider = _provider(_aapl_client(strike_rows=rows))
    at = _at(15)
    assert await provider.net_gamma_at("AAPL", Decimal("330"), at) is None
    assert await provider.net_gamma_at("AAPL", Decimal("320"), at) is None
    pos = await provider.net_gamma_at("AAPL", Decimal("325"), at)
    assert pos is not None
    assert pos.as_of == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_older_strike_snapshot_used_when_it_is_the_newest() -> None:
    rows = [{**_STRIKE_325, "date": "2026-09-10"}]
    provider = _provider(_aapl_client(strike_rows=rows))
    pos = await provider.net_gamma_at("AAPL", Decimal("325"), _at(15))
    assert pos is not None
    assert pos.as_of == datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_strike_row_without_date_takes_requested_date() -> None:
    row = {k: v for k, v in _STRIKE_325.items() if k != "date"}
    provider = _provider(_aapl_client(strike_rows=[row]))
    pos = await provider.net_gamma_at("AAPL", Decimal("325"), _at(15))
    assert pos is not None
    assert pos.as_of == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_naive_at_raises_value_error() -> None:
    client = _aapl_client()
    provider = _provider(client)
    naive = datetime(2026, 9, 11, 15, 0)  # deliberately naive
    with pytest.raises(ValueError, match="tz-aware"):
        await provider.aggregate_for_ticker("AAPL", naive)
    with pytest.raises(ValueError, match="tz-aware"):
        await provider.net_gamma_at("AAPL", Decimal("325"), naive)
    assert client.calls == []


# ---------------------------------------------------------------------------
# M21 wiring with the real provider
# ---------------------------------------------------------------------------


def _spy_event(timestamp: datetime) -> EnrichedEvent:
    op = OptionsPrint(
        event_id="p39-m21",
        timestamp=timestamp,
        ticker="SPY",
        option_type="call",
        strike=Decimal("760"),
        expiry=date(2026, 9, 18),
        dte=8,
        spot_price=Decimal("757.74"),
        premium_paid=Decimal("250000"),
        option_price=Decimal("5.00"),
        implied_volatility=0.18,
        bid=Decimal("4.95"),
        ask=Decimal("5.05"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("synthetic",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


@pytest.mark.asyncio
async def test_m21_stage_reads_usd_net_gamma_from_real_provider() -> None:
    """SPY 2026-09-10 close: -16.7B USD/1% is net short under the default
    profile; no strike rows -> no proximity -> partial_one_condition."""
    client = _FakeClient()
    client.stub(_SPY_SPOT, "2026-09-10", {"data": [_SPY_SPOT_LAST_0910]})
    client.stub(
        _SPY_STRIKE, "2026-09-10",
        UnusualWhalesNotFoundError("HTTP 404", status_code=404),
    )
    stage = DealerGammaStage(provider=_provider(client))
    event = _spy_event(datetime(2026, 9, 10, 20, 30, tzinfo=UTC))
    await stage.enrich(event, PipelineContext(profile=load_default_profile()))
    assert stage.last_execution_metadata == {
        "branch": "partial_one_condition",
        "provider_returned": "yes",
    }
    assert event.gamma_score == 0.5
