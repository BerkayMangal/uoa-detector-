"""Phase 3.9.6 tests: UW IV rank on ``/api/stock/{ticker}/iv-rank``.

Contract: ``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.3.
Fixtures are the live AAPL rows captured on 2026-09-14 (request
``date=2026-09-11``; last session Friday 2026-09-11). Every row is
published at 22:35Z, after the 16:00 ET close.

Pins:
  - request shape: ticker-level path, ``date`` = ET date of ``at``
  - as-of selection: prior-day rows eligible, a same-day row only once
    ``updated_at <= at``, future-dated rows never, order-independent
  - field mapping: string parsing, rank passthrough (0..100, no
    scaling), ``as_of = updated_at``, percentile/intraday change None
  - malformed rows: unparseable volatility/date/updated_at skipped,
    unparseable rank kept as None
  - cache key ``(ticker, ET date)``: call and put share one fetch
  - 404/422 NotFound maps to None; auth errors still raise
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.providers.iv_history import IVRankSnapshot
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesNotFoundError,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)

_PATH = "/api/stock/AAPL/iv-rank"

_ROW_0908: dict[str, Any] = {
    "close": "316.22", "date": "2026-09-08",
    "updated_at": "2026-09-08T22:35:01.988566Z",
    "volatility": "0.265", "iv_rank_1y": "55.7067",
}
_ROW_0909: dict[str, Any] = {
    "close": "315.34", "date": "2026-09-09",
    "updated_at": "2026-09-09T22:35:00.940875Z",
    "volatility": "0.259", "iv_rank_1y": "52.1585",
}
_ROW_0910: dict[str, Any] = {
    "close": "326.57", "date": "2026-09-10",
    "updated_at": "2026-09-10T22:35:01.459306Z",
    "volatility": "0.258", "iv_rank_1y": "51.5671",
}
_ROW_0911: dict[str, Any] = {
    "close": "332.27", "date": "2026-09-11",
    "updated_at": "2026-09-11T22:35:01.825674Z",
    "volatility": "0.239", "iv_rank_1y": "40.3312",
}
_LIVE_ROWS: list[dict[str, Any]] = [_ROW_0908, _ROW_0909, _ROW_0910, _ROW_0911]

_UPDATED_0910 = datetime(2026, 9, 10, 22, 35, 1, 459306, tzinfo=UTC)
_UPDATED_0911 = datetime(2026, 9, 11, 22, 35, 1, 825674, tzinfo=UTC)
# Friday 2026-09-11 14:00 ET (EDT), mid-session: the 09-11 row is not out yet.
_INTRADAY = datetime(2026, 9, 11, 18, 0, tzinfo=UTC)
# Friday 2026-09-11 19:00 ET, after the 22:35Z publication.
_EVENING = datetime(2026, 9, 11, 23, 0, tzinfo=UTC)


class _FakeClient:
    """Records ``(path, params)``; returns one payload or raises one error."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._payload: dict[str, Any] = payload if payload is not None else {"data": []}
        self._error = error

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._payload


def _provider(client: _FakeClient, *, ttl: int = 600) -> UnusualWhalesIVHistoryProvider:
    return UnusualWhalesIVHistoryProvider(
        client=client,  # type: ignore[arg-type]
        settings=UnusualWhalesSettings(
            cache_ttl=UnusualWhalesProviderCacheTTL(iv_history_seconds=ttl),
        ),
    )


async def _snap(
    provider: UnusualWhalesIVHistoryProvider,
    at: datetime,
    *,
    ticker: str = "AAPL",
    strike: Decimal = Decimal("335"),
    expiry: date = date(2026, 10, 16),
    option_type: Literal["call", "put"] = "call",
) -> IVRankSnapshot | None:
    return await provider.iv_rank_at(
        ticker=ticker,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        at=at,
    )


# ---------------------------------------------------------------------------
# Live rows: selection and mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intraday_event_uses_prior_session_row() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    snap = await _snap(_provider(client), _INTRADAY)
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671
    assert snap.implied_volatility == 0.258
    assert snap.as_of == _UPDATED_0910
    assert snap.iv_percentile_252d is None
    assert snap.iv_change_intraday_pct is None
    assert (snap.ticker, snap.strike, snap.expiry, snap.option_type) == (
        "AAPL", Decimal("335"), date(2026, 10, 16), "call",
    )


@pytest.mark.asyncio
async def test_event_after_publication_uses_same_day_row() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    snap = await _snap(_provider(client), _EVENING)
    assert snap is not None
    assert snap.iv_rank_252d == 40.3312  # 0..100 passthrough, not 0.403312
    assert snap.implied_volatility == 0.239
    assert snap.as_of == _UPDATED_0911


@pytest.mark.asyncio
async def test_request_path_and_et_date_param() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    await _snap(_provider(client), _INTRADAY)
    assert client.calls == [(_PATH, {"date": "2026-09-11"})]


@pytest.mark.asyncio
async def test_lowercase_ticker_is_uppercased() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    snap = await _snap(_provider(client), _INTRADAY, ticker="aapl")
    assert client.calls[0][0] == _PATH
    assert snap is not None
    assert snap.ticker == "AAPL"


@pytest.mark.asyncio
async def test_et_date_used_not_utc_date() -> None:
    """02:00Z Saturday is 22:00 ET Friday: date=2026-09-11, 09-11 row."""
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    snap = await _snap(_provider(client), datetime(2026, 9, 12, 2, 0, tzinfo=UTC))
    assert client.calls == [(_PATH, {"date": "2026-09-11"})]
    assert snap is not None
    assert snap.iv_rank_252d == 40.3312


@pytest.mark.asyncio
async def test_early_utc_morning_maps_to_previous_et_day() -> None:
    """03:00Z 09-11 is 23:00 ET 09-10: date=2026-09-10, 09-10 row eligible."""
    client = _FakeClient({"data": [_ROW_0909, _ROW_0910]})
    snap = await _snap(_provider(client), datetime(2026, 9, 11, 3, 0, tzinfo=UTC))
    assert client.calls == [(_PATH, {"date": "2026-09-10"})]
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671


@pytest.mark.asyncio
async def test_same_day_row_boundary_is_inclusive_at_updated_at() -> None:
    """Row D visible at exactly updated_at, not one microsecond before.

    Both calls share the ET date, so one fetch serves both: selection
    runs per call after the cache.
    """
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    provider = _provider(client)
    before = await _snap(provider, _UPDATED_0911 - timedelta(microseconds=1))
    exact = await _snap(provider, _UPDATED_0911)
    assert before is not None
    assert exact is not None
    assert before.iv_rank_252d == 51.5671
    assert exact.iv_rank_252d == 40.3312
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_future_dated_row_never_eligible() -> None:
    """A row dated after the ET date is ignored even if updated_at <= at."""
    future = {
        "close": "340.00", "date": "2026-09-14",
        "updated_at": "2026-09-11T10:00:00Z",
        "volatility": "0.300", "iv_rank_1y": "90.0",
    }
    client = _FakeClient({"data": [_ROW_0910, future]})
    snap = await _snap(_provider(client), _INTRADAY)
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671


@pytest.mark.asyncio
async def test_only_future_or_unpublished_rows_returns_none() -> None:
    client = _FakeClient({"data": [_ROW_0911]})
    assert await _snap(_provider(client), _INTRADAY) is None


@pytest.mark.asyncio
async def test_selection_is_row_order_independent() -> None:
    client = _FakeClient({"data": list(reversed(_LIVE_ROWS))})
    provider = _provider(client)
    intraday = await _snap(provider, _INTRADAY)
    evening = await _snap(provider, _EVENING)
    assert intraday is not None
    assert evening is not None
    assert intraday.iv_rank_252d == 51.5671
    assert evening.iv_rank_252d == 40.3312


@pytest.mark.asyncio
async def test_duplicate_date_rows_pick_latest_updated_at() -> None:
    republished = {**_ROW_0910, "updated_at": "2026-09-10T23:00:00Z", "iv_rank_1y": "51.9"}
    client = _FakeClient({"data": [republished, _ROW_0910]})
    snap = await _snap(_provider(client), _INTRADAY)
    assert snap is not None
    assert snap.iv_rank_252d == 51.9


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_numbers_also_parsed() -> None:
    row = {**_ROW_0910, "volatility": 0.258, "iv_rank_1y": 51}
    client = _FakeClient({"data": [row]})
    snap = await _snap(_provider(client), _INTRADAY)
    assert snap is not None
    assert snap.implied_volatility == 0.258
    assert snap.iv_rank_252d == 51.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "n/a", "NaN", True])
async def test_unparseable_volatility_row_skipped(bad: object) -> None:
    """The 09-11 row has no usable IV, so the 09-10 row is used."""
    client = _FakeClient({"data": [_ROW_0910, {**_ROW_0911, "volatility": bad}]})
    snap = await _snap(_provider(client), _EVENING)
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671
    assert snap.implied_volatility == 0.258


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "n/a"])
async def test_unparseable_rank_keeps_row_with_none_rank(bad: object) -> None:
    client = _FakeClient({"data": [_ROW_0910, {**_ROW_0911, "iv_rank_1y": bad}]})
    snap = await _snap(_provider(client), _EVENING)
    assert snap is not None
    assert snap.implied_volatility == 0.239
    assert snap.iv_rank_252d is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"date": "not-a-date"},
        {"date": None},
        {"updated_at": "not-a-timestamp"},
        {"updated_at": None},
    ],
)
async def test_unparseable_date_or_updated_at_row_skipped(
    override: dict[str, Any],
) -> None:
    client = _FakeClient({"data": [_ROW_0910, {**_ROW_0911, **override}]})
    snap = await _snap(_provider(client), _EVENING)
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671


@pytest.mark.asyncio
async def test_non_list_data_returns_none() -> None:
    client = _FakeClient({"data": {"date": "2026-09-10"}})
    assert await _snap(_provider(client), _INTRADAY) is None


@pytest.mark.asyncio
async def test_non_dict_rows_ignored() -> None:
    client = _FakeClient({"data": ["junk", 3, None, _ROW_0910]})
    snap = await _snap(_provider(client), _INTRADAY)
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671


@pytest.mark.asyncio
async def test_naive_at_treated_as_utc() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    snap = await _snap(_provider(client), datetime(2026, 9, 11, 18, 0))
    assert client.calls == [(_PATH, {"date": "2026-09-11"})]
    assert snap is not None
    assert snap.iv_rank_252d == 51.5671


# ---------------------------------------------------------------------------
# Cache key (ticker, ET date)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_and_put_same_ticker_and_day_share_one_fetch() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    provider = _provider(client)
    call = await _snap(provider, _INTRADAY)
    put = await _snap(
        provider, _EVENING, ticker="aapl", strike=Decimal("300"),
        expiry=date(2026, 11, 20), option_type="put",
    )
    assert len(client.calls) == 1
    assert call is not None
    assert put is not None
    assert call.iv_rank_252d == 51.5671
    assert put.iv_rank_252d == 40.3312
    assert (put.ticker, put.strike, put.expiry, put.option_type) == (
        "AAPL", Decimal("300"), date(2026, 11, 20), "put",
    )


@pytest.mark.asyncio
async def test_different_et_date_fetches_again() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    provider = _provider(client)
    await _snap(provider, _INTRADAY)
    await _snap(provider, datetime(2026, 9, 14, 15, 0, tzinfo=UTC))
    assert client.calls == [
        (_PATH, {"date": "2026-09-11"}),
        (_PATH, {"date": "2026-09-14"}),
    ]


@pytest.mark.asyncio
async def test_different_ticker_fetches_again() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    provider = _provider(client)
    await _snap(provider, _INTRADAY)
    await _snap(provider, _INTRADAY, ticker="MSFT")
    assert [path for path, _ in client.calls] == [_PATH, "/api/stock/MSFT/iv-rank"]


@pytest.mark.asyncio
async def test_ttl_zero_fetches_every_call() -> None:
    client = _FakeClient({"data": list(_LIVE_ROWS)})
    provider = _provider(client, ttl=0)
    await _snap(provider, _INTRADAY)
    await _snap(provider, _INTRADAY)
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_returns_none_and_is_cached() -> None:
    client = _FakeClient(
        error=UnusualWhalesNotFoundError("HTTP 404", status_code=404),
    )
    provider = _provider(client)
    assert await _snap(provider, _INTRADAY) is None
    assert await _snap(provider, _EVENING) is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_auth_error_propagates() -> None:
    """A bad key must never read as no data (contract §3.9)."""
    client = _FakeClient(error=UnusualWhalesAuthError("HTTP 401"))
    with pytest.raises(UnusualWhalesAuthError):
        await _snap(_provider(client), _INTRADAY)
