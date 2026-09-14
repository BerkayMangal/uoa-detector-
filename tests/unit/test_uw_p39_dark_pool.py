"""Phase 3.9.7 tests: M26 dark pool provider on ``/api/darkpool/{ticker}``.

Contract: ``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.4.

Fixture rows are trimmed from live responses captured 2026-09-14: the SPY
2026-09-11 18:00-19:00Z window (``order_by=premium``) and an AAPL
extended-hours page. Pins:
  - request path and params (ET date, ISO UTC hour-bucket cursors, page size, order)
  - hour-bucket caching keyed by (ticker, bucket, window)
  - client-side look-ahead filter: executed_at window, trf_executed_at <= before,
    canceled prints skipped
  - NBBO side rule and the condition-code exemptions
  - truncation WARNING on a full page, NotFound -> (), other errors propagate
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.providers.dark_pool import DarkPoolPrintProvider
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesNotFoundError,
)
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)

_LOGGER_NAME = "uoa_detector.sources.unusual_whales.providers.dark_pool"
_SPY_PATH = "/api/darkpool/SPY"
_HOUR = timedelta(minutes=60)
# 14:45 EDT on Friday 2026-09-11.
_BEFORE = datetime(2026, 9, 11, 18, 45, tzinfo=UTC)
# End of the captured window: every live SPY row below is <= this.
_WINDOW_END = datetime(2026, 9, 11, 19, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Live rows (trimmed)
# ---------------------------------------------------------------------------

# Largest print of the hour: QCT / contingent, priced 7 dollars below the bid.
_QCT_CONTINGENT: dict[str, Any] = {
    "executed_at": "2026-09-11T18:44:54Z",
    "trf_executed_at": "2026-09-11T18:44:54Z",
    "ticker": "SPY",
    "price": "757.8492",
    "size": 125583,
    "premium": "95172976.0836",
    "nbbo_bid": "765.14",
    "nbbo_ask": "765.17",
    "canceled": False,
    "sale_cond_codes": "contingent_trade",
    "trade_code": "qualified_contingent_trade",
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509852467661,
}
# Average-price / derivative-priced, priced above the ask.
_AVG_PRICE_DERIVATIVE: dict[str, Any] = {
    "executed_at": "2026-09-11T18:46:27Z",
    "trf_executed_at": "2026-09-11T18:46:27Z",
    "ticker": "SPY",
    "price": "765.1736",
    "size": 680,
    "premium": "520318.0480",
    "nbbo_bid": "765.13",
    "nbbo_ask": "765.15",
    "canceled": False,
    "sale_cond_codes": "average_price_trade",
    "trade_code": "derivative_priced",
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509852560501,
}
# price == nbbo_bid.
_AT_BID: dict[str, Any] = {
    "executed_at": "2026-09-11T18:48:54Z",
    "trf_executed_at": "2026-09-11T18:48:54Z",
    "ticker": "SPY",
    "price": "765.06",
    "size": 20000,
    "premium": "15301200.00",
    "nbbo_bid": "765.06",
    "nbbo_ask": "765.08",
    "canceled": False,
    "sale_cond_codes": None,
    "trade_code": None,
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509852707815,
}
# nbbo_bid < price < nbbo_ask.
_MIDPOINT: dict[str, Any] = {
    "executed_at": "2026-09-11T18:56:38Z",
    "trf_executed_at": "2026-09-11T18:56:38Z",
    "ticker": "SPY",
    "price": "765.24",
    "size": 9466,
    "premium": "7243761.84",
    "nbbo_bid": "765.22",
    "nbbo_ask": "765.25",
    "canceled": False,
    "sale_cond_codes": None,
    "trade_code": None,
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509853172148,
}
# price == nbbo_ask.
_AT_ASK: dict[str, Any] = {
    "executed_at": "2026-09-11T18:56:38Z",
    "trf_executed_at": "2026-09-11T18:56:38Z",
    "ticker": "SPY",
    "price": "765.25",
    "size": 7440,
    "premium": "5693460.00",
    "nbbo_bid": "765.22",
    "nbbo_ask": "765.25",
    "canceled": False,
    "sale_cond_codes": None,
    "trade_code": None,
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509853172149,
}
# Intermarket sweep (not exempt), priced below the bid.
_SWEEP_BELOW_BID: dict[str, Any] = {
    "executed_at": "2026-09-11T18:21:27Z",
    "trf_executed_at": "2026-09-11T18:21:27Z",
    "ticker": "SPY",
    "price": "765.24",
    "size": 720,
    "premium": "550972.80",
    "nbbo_bid": "765.25",
    "nbbo_ask": "765.27",
    "canceled": False,
    "sale_cond_codes": None,
    "trade_code": "intermarket_sweep",
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509851060863,
}
# trf_executed_at one second BEFORE executed_at (occurs live).
_TRF_BEFORE_EXEC: dict[str, Any] = {
    "executed_at": "2026-09-11T18:52:02Z",
    "trf_executed_at": "2026-09-11T18:52:01Z",
    "ticker": "SPY",
    "price": "765.11",
    "size": 500,
    "premium": "382555.00",
    "nbbo_bid": "765.1",
    "nbbo_ask": "765.12",
    "canceled": False,
    "sale_cond_codes": None,
    "trade_code": None,
    "ext_hour_sold_codes": None,
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 10509852895324,
}
# AAPL after-hours print (19:59:18 EDT), inside the NBBO but extended-hours.
_AAPL_EXT_HOURS: dict[str, Any] = {
    "executed_at": "2026-09-11T23:59:18Z",
    "trf_executed_at": "2026-09-11T23:59:18Z",
    "ticker": "AAPL",
    "price": "332.5796",
    "size": 1111,
    "premium": "369495.9356",
    "nbbo_bid": "332.55",
    "nbbo_ask": "332.6",
    "canceled": False,
    "sale_cond_codes": None,
    "trade_code": None,
    "ext_hour_sold_codes": "extended_hours_trade",
    "market_center": "L",
    "trade_settlement": "regular",
    "tracking_id": 26946711173854,
}


def _row(base: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    out = dict(base)
    out.update(overrides)
    return out


def _without(base: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in base.items() if k != key}


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Captures (path, params); returns one scripted body or raises."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._response = response if response is not None else {"data": []}
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
        return self._response


def _provider(client: _FakeClient, *, ttl: int = 60) -> UnusualWhalesDarkPoolProvider:
    return UnusualWhalesDarkPoolProvider(
        client=client,  # type: ignore[arg-type]
        settings=UnusualWhalesSettings(
            cache_ttl=UnusualWhalesProviderCacheTTL(dark_pool_seconds=ttl),
        ),
    )


def _params(
    *, date: str, newer_than: str, older_than: str,
) -> dict[str, Any]:
    return {
        "date": date,
        "newer_than": newer_than,
        "older_than": older_than,
        "limit": 500,
        "order_by": "premium",
        "order": "desc",
    }


# ===========================================================================
# Request shape
# ===========================================================================


def test_implements_protocol() -> None:
    assert isinstance(_provider(_FakeClient()), DarkPoolPrintProvider)


@pytest.mark.asyncio
async def test_request_path_and_params_for_hour_bucket() -> None:
    client = _FakeClient()
    await _provider(client).recent_prints("SPY", _BEFORE, _HOUR)
    assert client.calls == [
        (
            _SPY_PATH,
            _params(
                date="2026-09-11",
                newer_than="2026-09-11T17:00:00Z",
                older_than="2026-09-11T19:00:00Z",
            ),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "window", "expected"),
    [
        # 21:30 EDT on 09-11: UTC date is 09-12, ET date is 09-11.
        (
            datetime(2026, 9, 12, 1, 30, tzinfo=UTC),
            timedelta(minutes=30),
            _params(
                date="2026-09-11",
                newer_than="2026-09-12T00:30:00Z",
                older_than="2026-09-12T02:00:00Z",
            ),
        ),
        # 23:30 EST on 01-15 (winter offset).
        (
            datetime(2026, 1, 16, 4, 30, tzinfo=UTC),
            _HOUR,
            _params(
                date="2026-01-15",
                newer_than="2026-01-16T03:00:00Z",
                older_than="2026-01-16T05:00:00Z",
            ),
        ),
        # 00:00 EST on 01-16 exactly.
        (
            datetime(2026, 1, 16, 5, 0, tzinfo=UTC),
            _HOUR,
            _params(
                date="2026-01-16",
                newer_than="2026-01-16T04:00:00Z",
                older_than="2026-01-16T06:00:00Z",
            ),
        ),
        # ET-aware input floors to the UTC hour.
        (
            datetime(2026, 9, 11, 14, 45, tzinfo=ZoneInfo("America/New_York")),
            timedelta(minutes=90),
            _params(
                date="2026-09-11",
                newer_than="2026-09-11T16:30:00Z",
                older_than="2026-09-11T19:00:00Z",
            ),
        ),
        # Naive input is read as UTC.
        (
            datetime(2026, 9, 11, 18, 45),
            _HOUR,
            _params(
                date="2026-09-11",
                newer_than="2026-09-11T17:00:00Z",
                older_than="2026-09-11T19:00:00Z",
            ),
        ),
    ],
    ids=["edt_evening_et_date", "est_late_evening", "est_midnight", "et_aware", "naive_utc"],
)
async def test_date_is_et_and_cursors_are_utc_hour_bucket(
    before: datetime, window: timedelta, expected: dict[str, Any],
) -> None:
    client = _FakeClient()
    await _provider(client).recent_prints("SPY", before, window)
    assert client.calls == [(_SPY_PATH, expected)]


@pytest.mark.asyncio
async def test_ticker_is_uppercased_in_path_and_dto() -> None:
    client = _FakeClient({"data": [_QCT_CONTINGENT]})
    prints = await _provider(client).recent_prints("spy", _BEFORE, _HOUR)
    assert client.calls[0][0] == _SPY_PATH
    assert [p.ticker for p in prints] == ["SPY"]


# ===========================================================================
# Parsing and side_estimate
# ===========================================================================


@pytest.mark.asyncio
async def test_live_row_parses_decimal_price_int_size_utc_time() -> None:
    client = _FakeClient({"data": [_QCT_CONTINGENT]})
    prints = await _provider(client).recent_prints("SPY", _BEFORE, _HOUR)
    assert isinstance(prints, tuple)
    assert len(prints) == 1
    p = prints[0]
    assert p.price == Decimal("757.8492")
    assert p.size == 125583
    assert p.when == datetime(2026, 9, 11, 18, 44, 54, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_AT_ASK, "above_ask"),
        (_AT_BID, "at_or_below_bid"),
        (_MIDPOINT, "midpoint"),
        (_SWEEP_BELOW_BID, "at_or_below_bid"),
        (_TRF_BEFORE_EXEC, "midpoint"),
        # Below the bid by $7, but a QCT print is not priced off the NBBO.
        (_QCT_CONTINGENT, "unknown"),
        # Above the ask, but average-price / derivative-priced.
        (_AVG_PRICE_DERIVATIVE, "unknown"),
        (_row(_MIDPOINT, price="765.30"), "above_ask"),
        (_row(_MIDPOINT, price="765.20"), "at_or_below_bid"),
        # JSON numbers instead of strings still parse.
        (_row(_MIDPOINT, price=765.24, nbbo_bid=765.22, nbbo_ask=765.25), "midpoint"),
    ],
    ids=[
        "price_eq_ask", "price_eq_bid", "inside", "sweep_below_bid",
        "trf_before_exec", "qct_contingent", "avg_price_derivative",
        "above_ask_strict", "below_bid_strict", "numeric_fields",
    ],
)
async def test_side_estimate_on_live_rows(row: dict[str, Any], expected: str) -> None:
    client = _FakeClient({"data": [row]})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert [p.side_estimate for p in prints] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"sale_cond_codes": "contingent_trade"}, "unknown"),
        ({"sale_cond_codes": "average_price_trade"}, "unknown"),
        ({"sale_cond_codes": "prior_reference_price"}, "unknown"),
        ({"trade_code": "qualified_contingent_trade"}, "unknown"),
        ({"trade_code": "derivative_priced"}, "unknown"),
        ({"ext_hour_sold_codes": "sold_out_of_sequence"}, "unknown"),
        ({"ext_hour_sold_codes": "extended_hours_trade"}, "unknown"),
        # Codes outside the exemption sets keep the NBBO rule.
        ({"sale_cond_codes": "odd_lot_execution"}, "above_ask"),
        ({"trade_code": "intermarket_sweep"}, "above_ask"),
    ],
)
async def test_condition_codes_force_unknown(
    overrides: dict[str, Any], expected: str,
) -> None:
    client = _FakeClient({"data": [_row(_AT_ASK, **overrides)]})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert [p.side_estimate for p in prints] == [expected]


@pytest.mark.asyncio
async def test_extended_hours_live_row_is_unknown() -> None:
    client = _FakeClient({"data": [_AAPL_EXT_HOURS]})
    before = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
    prints = await _provider(client).recent_prints("AAPL", before, _HOUR)
    assert client.calls[0] == (
        "/api/darkpool/AAPL",
        _params(
            date="2026-09-11",
            newer_than="2026-09-11T23:00:00Z",
            older_than="2026-09-12T01:00:00Z",
        ),
    )
    assert [p.side_estimate for p in prints] == ["unknown"]
    assert prints[0].price == Decimal("332.5796")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        _row(_MIDPOINT, nbbo_bid=None),
        _row(_MIDPOINT, nbbo_ask=None),
        _without(_MIDPOINT, "nbbo_bid"),
        _without(_MIDPOINT, "nbbo_ask"),
        _row(_MIDPOINT, nbbo_bid="0"),
        _row(_MIDPOINT, nbbo_ask="0"),
        _row(_MIDPOINT, nbbo_bid="-1"),
        _row(_MIDPOINT, nbbo_bid="765.26", nbbo_ask="765.22"),
        _row(_MIDPOINT, nbbo_ask="abc"),
        _row(_MIDPOINT, nbbo_ask=""),
        _row(_MIDPOINT, nbbo_bid="NaN"),
        _row(_MIDPOINT, nbbo_ask="Infinity"),
    ],
    ids=[
        "bid_null", "ask_null", "bid_absent", "ask_absent", "bid_zero",
        "ask_zero", "bid_negative", "crossed", "ask_garbage", "ask_empty",
        "bid_nan", "ask_inf",
    ],
)
async def test_invalid_nbbo_is_unknown_but_print_kept(row: dict[str, Any]) -> None:
    client = _FakeClient({"data": [row]})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert len(prints) == 1
    assert prints[0].side_estimate == "unknown"
    assert prints[0].size == 9466


@pytest.mark.asyncio
async def test_malformed_and_non_finite_rows_skipped() -> None:
    rows: list[Any] = [
        _row(_MIDPOINT, price="NaN"),
        _row(_MIDPOINT, price="Infinity"),
        _row(_MIDPOINT, price="abc"),
        _row(_MIDPOINT, size=None),
        _row(_MIDPOINT, executed_at="garbage"),
        _without(_MIDPOINT, "executed_at"),
        "not-a-row",
        _MIDPOINT,
    ]
    client = _FakeClient({"data": rows})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert len(prints) == 1
    assert prints[0].price == Decimal("765.24")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"data": None}, {"data": {}}, {}])
async def test_non_list_data_returns_empty(body: dict[str, Any]) -> None:
    client = _FakeClient(body)
    assert await _provider(client).recent_prints("SPY", _BEFORE, _HOUR) == ()


# ===========================================================================
# Look-ahead filter
# ===========================================================================


@pytest.mark.asyncio
async def test_executed_at_window_is_inclusive_and_future_prints_dropped() -> None:
    def at(ts: str) -> dict[str, Any]:
        return _row(_MIDPOINT, executed_at=ts, trf_executed_at=ts)

    rows = [
        at("2026-09-11T17:44:59Z"),  # before - window - 1s
        at("2026-09-11T17:45:00Z"),  # == before - window
        at("2026-09-11T18:45:00Z"),  # == before
        at("2026-09-11T18:45:01Z"),  # same bucket, after before
        at("2026-09-11T18:59:59Z"),  # same bucket, after before
    ]
    client = _FakeClient({"data": rows})
    prints = await _provider(client).recent_prints("SPY", _BEFORE, _HOUR)
    assert [p.when for p in prints] == [
        datetime(2026, 9, 11, 17, 45, 0, tzinfo=UTC),
        datetime(2026, 9, 11, 18, 45, 0, tzinfo=UTC),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trf", "kept"),
    [
        ("2026-09-11T18:45:01Z", False),  # executed before `before`, reported after
        ("2026-09-11T18:45:00Z", True),
        ("2026-09-11T18:44:58Z", True),
        (None, True),  # UW: null for trades before 2025-05-01
        ("not-a-timestamp", False),
    ],
    ids=["reported_after", "reported_at", "reported_before", "null", "garbage"],
)
async def test_trf_executed_at_must_not_be_after_before(
    trf: str | None, kept: bool,
) -> None:
    row = _row(_MIDPOINT, executed_at="2026-09-11T18:44:59Z", trf_executed_at=trf)
    client = _FakeClient({"data": [row]})
    prints = await _provider(client).recent_prints("SPY", _BEFORE, _HOUR)
    assert len(prints) == (1 if kept else 0)


@pytest.mark.asyncio
async def test_absent_trf_executed_at_uses_executed_at_only() -> None:
    row = _without(_row(_MIDPOINT, executed_at="2026-09-11T18:44:59Z"), "trf_executed_at")
    client = _FakeClient({"data": [row]})
    prints = await _provider(client).recent_prints("SPY", _BEFORE, _HOUR)
    assert len(prints) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "kept"),
    [
        (_row(_AT_BID, canceled=True), False),
        (_row(_AT_BID, canceled=False), True),
        (_without(_AT_BID, "canceled"), True),
    ],
    ids=["canceled", "not_canceled", "absent"],
)
async def test_canceled_prints_skipped(row: dict[str, Any], kept: bool) -> None:
    client = _FakeClient({"data": [row]})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert len(prints) == (1 if kept else 0)


# ===========================================================================
# Caching
# ===========================================================================


@pytest.mark.asyncio
async def test_same_bucket_and_window_share_one_fetch_filtered_per_call() -> None:
    client = _FakeClient({"data": [_QCT_CONTINGENT]})
    provider = _provider(client)
    early = await provider.recent_prints(
        "SPY", datetime(2026, 9, 11, 18, 5, tzinfo=UTC), _HOUR,
    )
    late = await provider.recent_prints("SPY", _BEFORE, _HOUR)
    assert len(client.calls) == 1
    # The 18:44:54 print is in the cached rows but in the future for 18:05.
    assert early == ()
    assert len(late) == 1


@pytest.mark.asyncio
async def test_cache_key_is_ticker_bucket_window() -> None:
    client = _FakeClient()
    provider = _provider(client)
    await provider.recent_prints("SPY", _BEFORE, _HOUR)
    await provider.recent_prints("SPY", datetime(2026, 9, 11, 18, 0, tzinfo=UTC), _HOUR)
    await provider.recent_prints(
        "SPY", datetime(2026, 9, 11, 18, 59, 59, 999999, tzinfo=UTC), _HOUR,
    )
    assert len(client.calls) == 1
    await provider.recent_prints("SPY", _BEFORE, timedelta(minutes=30))
    await provider.recent_prints("SPY", datetime(2026, 9, 11, 19, 0, tzinfo=UTC), _HOUR)
    await provider.recent_prints("QQQ", _BEFORE, _HOUR)
    assert [(path, (params or {})["newer_than"]) for path, params in client.calls] == [
        (_SPY_PATH, "2026-09-11T17:00:00Z"),
        (_SPY_PATH, "2026-09-11T17:30:00Z"),
        (_SPY_PATH, "2026-09-11T18:00:00Z"),
        ("/api/darkpool/QQQ", "2026-09-11T17:00:00Z"),
    ]


@pytest.mark.asyncio
async def test_ttl_zero_fetches_every_call() -> None:
    client = _FakeClient()
    provider = _provider(client, ttl=0)
    await provider.recent_prints("SPY", _BEFORE, _HOUR)
    await provider.recent_prints("SPY", _BEFORE, _HOUR)
    assert len(client.calls) == 2


# ===========================================================================
# Truncation and errors
# ===========================================================================


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == _LOGGER_NAME and r.levelno == logging.WARNING
    ]


@pytest.mark.asyncio
async def test_full_page_logs_truncation_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    rows = [_row(_MIDPOINT, tracking_id=i) for i in range(500)]
    client = _FakeClient({"data": rows})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert len(prints) == 500
    records = _warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert _SPY_PATH in message
    assert "truncated" in message
    assert "500" in message


@pytest.mark.asyncio
async def test_partial_page_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    rows = [_row(_MIDPOINT, tracking_id=i) for i in range(499)]
    client = _FakeClient({"data": rows})
    prints = await _provider(client).recent_prints("SPY", _WINDOW_END, _HOUR)
    assert len(prints) == 499
    assert _warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 422])
async def test_not_found_returns_empty(status: int) -> None:
    client = _FakeClient(
        error=UnusualWhalesNotFoundError(f"HTTP {status}", status_code=status),
    )
    assert await _provider(client).recent_prints("ZZZZ", _BEFORE, _HOUR) == ()
    assert client.calls[0][0] == "/api/darkpool/ZZZZ"


@pytest.mark.asyncio
async def test_auth_error_propagates() -> None:
    client = _FakeClient(error=UnusualWhalesAuthError("HTTP 401"))
    with pytest.raises(UnusualWhalesAuthError):
        await _provider(client).recent_prints("SPY", _BEFORE, _HOUR)
