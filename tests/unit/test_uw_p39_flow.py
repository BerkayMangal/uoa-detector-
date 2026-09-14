"""Phase 3.9.4 tests: flow-alerts paginator, REST flow source, mapper aliases.

Contract: docs/phase-3.9-uw-endpoint-correction-acceptance.md §3.1.
Row fixtures are trimmed real rows from GET /api/option-trades/flow-alerts
(captured 2026-09-14, 2026-09-11 session). No network: fake clients return
canned pages.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.sources.unusual_whales import rest_flow
from uoa_detector.sources.unusual_whales.flow_alerts import (
    FLOW_ALERTS_PAGE_LIMIT,
    FLOW_ALERTS_PATH,
    fetch_flow_alerts,
)
from uoa_detector.sources.unusual_whales.live import map_uw_flow_event
from uoa_detector.sources.unusual_whales.rest_flow import UnusualWhalesRestFlowSource

# ---------------------------------------------------------------------------
# Live fixtures (trimmed from fa_200.json, AAPL,MSFT,NVDA,TSLA,SPY, limit 200)
# ---------------------------------------------------------------------------

# Ask-side aggressor, no sweep.
_LIVE_ASK_ROW: dict[str, Any] = {
    "id": "cc16b827-281c-4927-9f09-6bc65e933056",
    "ticker": "TSLA",
    "type": "call",
    "strike": "400",
    "expiry": "2026-12-18",
    "created_at": "2026-09-11T19:59:50.817119Z",
    "price": "21.5",
    "bid": "21.35",
    "ask": "21.5",
    "underlying_price": "365.31",
    "total_premium": "118249",
    "total_ask_side_prem": "118249",
    "total_bid_side_prem": "0",
    "total_size": 55,
    "open_interest": 12605,
    "iv_end": "0.440580259552382",
    "has_sweep": False,
    "has_multileg": False,
    "alert_rule": "RepeatedHitsAscendingFill",
    "option_chain": "TSLA261218C00400000",
}

# Bid-side aggressor, no sweep (bid premium < total premium on this alert).
_LIVE_BID_ROW: dict[str, Any] = {
    "id": "36c1dd88-0ab0-44ee-8d4d-139043753a72",
    "ticker": "TSLA",
    "type": "call",
    "strike": "400",
    "expiry": "2026-10-16",
    "created_at": "2026-09-11T19:59:56.626865Z",
    "price": "6.79",
    "bid": "6.75",
    "ask": "6.85",
    "underlying_price": "365.54",
    "total_premium": "108668",
    "total_ask_side_prem": "0",
    "total_bid_side_prem": "89628",
    "total_size": 160,
    "open_interest": 11307,
    "iv_end": "0.400279582021514",
    "has_sweep": False,
    "has_multileg": False,
    "alert_rule": "RepeatedHitsDescendingFill",
    "option_chain": "TSLA261016C00400000",
}

# Sweep alert (has_sweep true), bid-side.
_LIVE_SWEEP_ROW: dict[str, Any] = {
    "id": "fdad3ba8-354c-4195-ad52-c381429f0463",
    "ticker": "TSLA",
    "type": "call",
    "strike": "370",
    "expiry": "2026-09-18",
    "created_at": "2026-09-11T19:36:11.754317Z",
    "price": "5.61",
    "bid": "5.6",
    "ask": "5.7",
    "underlying_price": "365.045",
    "total_premium": "112145",
    "total_ask_side_prem": "0",
    "total_bid_side_prem": "112145",
    "total_size": 200,
    "open_interest": 13153,
    "iv_end": "0.380114769861046",
    "has_sweep": True,
    "has_multileg": False,
    "alert_rule": "RepeatedHitsDescendingFill",
    "option_chain": "TSLA260918C00370000",
}

# 2026-09-11T20:00:00Z / 19:00:00Z in epoch seconds.
_EPOCH_2000Z = 1789156800
_EPOCH_1900Z = 1789153200


def _row(base: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    out = dict(base)
    out.update(overrides)
    return out


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _series(
    newest: datetime, count: int, *, step: timedelta, prefix: str,
) -> list[dict[str, Any]]:
    """``count`` live-shaped rows, newest first, ``step`` apart."""
    return [
        _row(_LIVE_ASK_ROW, id=f"{prefix}-{i}", created_at=_iso(newest - i * step))
        for i in range(count)
    ]


class _PagedClient:
    """Fake UW client: serves pages in order, then repeats the last page."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append((path, dict(params or {})))
        if not self._pages:
            return {"data": []}
        if len(self._pages) > 1:
            return self._pages.pop(0)
        return self._pages[0]


def _map(row: dict[str, Any]) -> RawPrint | None:
    return map_uw_flow_event(
        row, source_id="unusual_whales", source_event_id_fallback="uw-fb-1",
    )


async def _drain(src: UnusualWhalesRestFlowSource) -> list[RawPrint]:
    return [rp async for rp in src.stream()]


# ---------------------------------------------------------------------------
# fetch_flow_alerts — request params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_request_params_are_epoch_seconds() -> None:
    client = _PagedClient([{"data": []}])
    await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["aapl", " msft ", "AAPL", ""],
        older_than=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
        newer_than=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
    )
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == FLOW_ALERTS_PATH == "/api/option-trades/flow-alerts"
    assert params == {
        "ticker_symbol": "AAPL,MSFT",
        "limit": FLOW_ALERTS_PAGE_LIMIT,
        "older_than": _EPOCH_2000Z,
        "newer_than": _EPOCH_1900Z,
    }
    assert FLOW_ALERTS_PAGE_LIMIT == 200
    # Epoch SECONDS as ints (live: ISO newer_than is ignored by the server).
    assert type(params["older_than"]) is int
    assert type(params["newer_than"]) is int


@pytest.mark.asyncio
async def test_fractional_bounds_widen_to_whole_seconds() -> None:
    client = _PagedClient([{"data": []}])
    await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["SPY"],
        older_than=datetime(2026, 9, 11, 20, 0, 0, 500000, tzinfo=UTC),
        newer_than=datetime(2026, 9, 11, 19, 0, 0, 500000, tzinfo=UTC),
    )
    params = client.calls[0][1]
    assert params["older_than"] == _EPOCH_2000Z + 1  # rounded up
    assert params["newer_than"] == _EPOCH_1900Z  # rounded down


@pytest.mark.asyncio
async def test_open_ended_request_omits_newer_than() -> None:
    client = _PagedClient([{"data": [_LIVE_ASK_ROW]}])
    await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        older_than=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
    )
    assert "newer_than" not in client.calls[0][1]


@pytest.mark.asyncio
async def test_invalid_arguments_raise_before_any_request() -> None:
    client = _PagedClient([{"data": []}])
    when = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="at least one ticker"):
        await fetch_flow_alerts(client, tickers=[" "], older_than=when)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await fetch_flow_alerts(client, tickers="AAPL", older_than=when)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        await fetch_flow_alerts(
            client,  # type: ignore[arg-type]
            tickers=["AAPL"],
            older_than=datetime(2026, 9, 11, 20, 0),  # naive on purpose
        )
    assert client.calls == []


# ---------------------------------------------------------------------------
# fetch_flow_alerts — pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paginates_on_oldest_created_at_and_dedupes_overlap() -> None:
    step = timedelta(seconds=30)
    newest = datetime(2026, 9, 11, 20, 12, 57, 101514, tzinfo=UTC)
    page1 = _series(newest, FLOW_ALERTS_PAGE_LIMIT, step=step, prefix="p1")
    oldest1 = newest - (FLOW_ALERTS_PAGE_LIMIT - 1) * step
    # Inclusive boundary: page 2 repeats page 1's oldest row (live: 1 overlap).
    page2 = [page1[-1], *_series(oldest1 - step, 3, step=step, prefix="p2")]
    client = _PagedClient([{"data": page1}, {"data": page2}])

    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["SPY"],
        older_than=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
    )

    assert result.pages == 2
    assert len(client.calls) == 2
    # Page 2 cursor = epoch seconds of page 1's oldest created_at (rounded up).
    assert client.calls[1][1]["older_than"] == math.ceil(oldest1.timestamp())
    assert client.calls[1][1]["ticker_symbol"] == "SPY"
    ids = [r["id"] for r in result.rows]
    assert len(ids) == FLOW_ALERTS_PAGE_LIMIT + 3
    assert len(set(ids)) == len(ids)  # overlap removed
    assert ids[0] == "p1-0"
    assert ids[-1] == "p2-2"
    assert result.stop_reason == "short_page"
    assert result.truncated is False


@pytest.mark.asyncio
async def test_lookback_cutoff_stops_walk_and_clamps_rows() -> None:
    before = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
    newer = before - timedelta(minutes=60)
    step = timedelta(seconds=20)
    first = before - timedelta(seconds=10)
    # A full page reaching past newer_than: the clamp holds even if the server
    # over-returns, and the walk stops without a second request.
    page = _series(first, FLOW_ALERTS_PAGE_LIMIT, step=step, prefix="w")
    client = _PagedClient([{"data": page}])

    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        older_than=before,
        newer_than=newer,
    )

    assert result.pages == 1
    assert result.cutoff == newer
    assert result.stop_reason == "cutoff_reached"
    created = [datetime.fromisoformat(str(r["created_at"])) for r in result.rows]
    assert created
    assert all(newer <= c <= before for c in created)
    expected = sum(1 for i in range(FLOW_ALERTS_PAGE_LIMIT) if first - i * step >= newer)
    assert len(result.rows) == expected


@pytest.mark.asyncio
async def test_open_ended_cutoff_is_latest_session_midnight_et() -> None:
    step = timedelta(minutes=1)
    friday = datetime(2026, 9, 11, 20, 12, tzinfo=UTC)
    page1 = _series(friday, FLOW_ALERTS_PAGE_LIMIT, step=step, prefix="fri")
    thursday = datetime(2026, 9, 10, 19, 59, tzinfo=UTC)
    page2 = [
        *_series(friday - FLOW_ALERTS_PAGE_LIMIT * step, 100, step=step, prefix="fri2"),
        *_series(thursday, 100, step=step, prefix="thu"),
    ]
    client = _PagedClient([{"data": page1}, {"data": page2}, {"data": []}])

    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["AAPL", "MSFT", "NVDA", "TSLA", "SPY"],
        older_than=datetime(2026, 9, 14, 11, 46, tzinfo=UTC),  # Monday pre-market
    )

    # 00:00 America/New_York (EDT) on 2026-09-11 == 04:00Z.
    assert result.cutoff == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
    assert result.stop_reason == "cutoff_reached"
    assert result.pages == 2  # stops on the page that crosses the cutoff
    assert all("newer_than" not in params for _, params in client.calls)
    ids = [str(r["id"]) for r in result.rows]
    assert len(ids) == 300
    assert not any(i.startswith("thu") for i in ids)


@pytest.mark.asyncio
async def test_latest_session_uses_et_date_not_utc_date() -> None:
    rows = [
        # 2026-09-12T01:30Z is 21:30 ET on Friday 2026-09-11.
        _row(_LIVE_ASK_ROW, id="late", created_at="2026-09-12T01:30:00Z"),
        _row(_LIVE_ASK_ROW, id="early", created_at="2026-09-11T13:45:00Z"),
        # 2026-09-11T03:59:59Z is 23:59:59 ET on Thursday 2026-09-10.
        _row(_LIVE_ASK_ROW, id="prior", created_at="2026-09-11T03:59:59Z"),
    ]
    client = _PagedClient([{"data": rows}])
    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        older_than=datetime(2026, 9, 12, 2, 0, tzinfo=UTC),
    )
    assert result.cutoff == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
    assert [r["id"] for r in result.rows] == ["late", "early"]


@pytest.mark.asyncio
async def test_full_page_with_no_new_rows_stops_and_flags_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = _series(
        datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
        FLOW_ALERTS_PAGE_LIMIT,
        step=timedelta(seconds=1),
        prefix="dup",
    )
    client = _PagedClient([{"data": page}])  # server keeps serving the same page
    with caplog.at_level(logging.WARNING):
        result = await fetch_flow_alerts(
            client,  # type: ignore[arg-type]
            tickers=["SPY"],
            older_than=datetime(2026, 9, 11, 20, 0, 30, tzinfo=UTC),
        )
    assert result.pages == 2
    assert result.stop_reason == "no_new_rows"
    assert result.truncated is True
    assert len(result.rows) == FLOW_ALERTS_PAGE_LIMIT
    assert "no_new_rows" in caplog.text


@pytest.mark.asyncio
async def test_cursor_not_advancing_stops_and_flags_truncated() -> None:
    # 200 alerts inside one second: the whole-second cursor cannot move back.
    base = datetime(2026, 9, 11, 20, 0, 0, 100000, tzinfo=UTC)
    page = _series(base, FLOW_ALERTS_PAGE_LIMIT, step=timedelta(microseconds=100), prefix="burst")
    client = _PagedClient([{"data": page}])
    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["SPY"],
        older_than=datetime(2026, 9, 11, 20, 0, 0, 500000, tzinfo=UTC),
    )
    assert result.pages == 1
    assert result.stop_reason == "cursor_not_advancing"
    assert result.truncated is True
    assert len(result.rows) == FLOW_ALERTS_PAGE_LIMIT


@pytest.mark.asyncio
async def test_full_page_without_parseable_created_at_stops_no_cursor() -> None:
    page = [
        _row(_LIVE_ASK_ROW, id=f"bad-{i}", created_at="not-a-time")
        for i in range(FLOW_ALERTS_PAGE_LIMIT)
    ]
    client = _PagedClient([{"data": page}])
    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["SPY"],
        older_than=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
    )
    assert result.pages == 1
    assert result.stop_reason == "no_cursor"
    assert result.truncated is True
    assert result.cutoff is None
    # Unparseable created_at rows are passed through for the mapper to drop.
    assert len(result.rows) == FLOW_ALERTS_PAGE_LIMIT


@pytest.mark.asyncio
async def test_unparseable_created_at_passed_through_and_idless_rows_deduped() -> None:
    no_id = _row(_LIVE_ASK_ROW)
    del no_id["id"]
    same_alert = dict(no_id)
    other_size = _row(no_id, total_size=56)
    garbage_time = _row(_LIVE_BID_ROW, created_at="yesterday")
    client = _PagedClient(
        [{"data": [no_id, same_alert, other_size, garbage_time, "junk", 7]}],
    )
    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        older_than=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
    )
    assert result.rows == (no_id, other_size, garbage_time)
    assert result.non_object_rows == 2


@pytest.mark.asyncio
async def test_missing_data_key_is_an_empty_short_page() -> None:
    client = _PagedClient([{"other": []}])
    result = await fetch_flow_alerts(
        client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        older_than=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
    )
    assert result.rows == ()
    assert result.pages == 1
    assert result.cutoff is None
    assert result.stop_reason == "short_page"


# ---------------------------------------------------------------------------
# map_uw_flow_event — flow-alert aliases on live rows
# ---------------------------------------------------------------------------


def test_live_ask_side_row_maps_every_alias() -> None:
    rp = _map(_LIVE_ASK_ROW)
    assert rp is not None
    assert rp.source_event_id == "uw-cc16b827-281c-4927-9f09-6bc65e933056"
    assert rp.ticker == "TSLA"
    assert rp.timestamp == datetime(2026, 9, 11, 19, 59, 50, 817119, tzinfo=UTC)
    assert rp.option_type == "call"
    assert rp.strike == Decimal("400")
    assert rp.dte == (datetime(2026, 12, 18).date() - datetime(2026, 9, 11).date()).days
    assert rp.premium_paid == Decimal("118249")
    assert rp.option_price == Decimal("21.5")
    assert rp.bid == Decimal("21.35")
    assert rp.ask == Decimal("21.5")
    assert rp.spot_price == Decimal("365.31")
    assert rp.implied_volatility == pytest.approx(0.440580259552382)
    assert rp.open_interest == 12605
    assert rp.fill_side == "at_ask"
    assert rp.is_iso is False
    assert rp.source_tags == ("uw:repeatedhitsascendingfill",)
    assert "uw:no-spot" not in rp.source_tags


def test_live_bid_side_row_maps_at_bid() -> None:
    rp = _map(_LIVE_BID_ROW)
    assert rp is not None
    assert rp.fill_side == "at_bid"
    assert rp.is_iso is False
    assert rp.source_tags == ("uw:repeatedhitsdescendingfill",)


def test_live_sweep_row_sets_is_iso() -> None:
    rp = _map(_LIVE_SWEEP_ROW)
    assert rp is not None
    assert rp.is_iso is True
    assert rp.fill_side == "at_bid"
    assert rp.open_interest == 13153


@pytest.mark.parametrize(
    ("ask_prem", "bid_prem", "expected"),
    [
        ("118249", "0", "at_ask"),
        ("0", "89628", "at_bid"),
        ("50000", "50000", "unknown"),
        ("0", "0", "unknown"),
        (None, "100", "unknown"),
        ("abc", "0", "unknown"),
        ("NaN", "0", "unknown"),
    ],
)
def test_fill_side_from_premium_split(
    ask_prem: str | None, bid_prem: str | None, expected: str,
) -> None:
    rp = _map(_row(_LIVE_ASK_ROW, total_ask_side_prem=ask_prem, total_bid_side_prem=bid_prem))
    assert rp is not None
    assert rp.fill_side == expected


def test_fill_side_unknown_when_premium_split_absent() -> None:
    row = _row(_LIVE_ASK_ROW)
    del row["total_ask_side_prem"]
    rp = _map(row)
    assert rp is not None
    assert rp.fill_side == "unknown"


def test_side_label_keeps_priority_over_premium_split() -> None:
    # A directional side_classification still degrades to "unknown" (Phase 3.7
    # pin) even when a premium split is present; a fill 'side' label wins.
    directional = _map(_row(_LIVE_ASK_ROW, side_classification="bearish"))
    assert directional is not None
    assert directional.fill_side == "unknown"
    labelled = _map(_row(_LIVE_ASK_ROW, side="BID"))
    assert labelled is not None
    assert labelled.fill_side == "at_bid"


def test_multileg_alert_tagged_and_kept() -> None:
    rp = _map(_row(_LIVE_ASK_ROW, has_multileg=True))
    assert rp is not None
    assert rp.source_tags == ("uw:repeatedhitsascendingfill", "uw:multileg")


def test_executed_at_falls_back_to_created_at() -> None:
    unparseable = _map(_row(_LIVE_ASK_ROW, executed_at="not-a-time"))
    assert unparseable is not None
    assert unparseable.timestamp == datetime(2026, 9, 11, 19, 59, 50, 817119, tzinfo=UTC)
    null_executed = _map(_row(_LIVE_ASK_ROW, executed_at=None))
    assert null_executed is not None
    assert null_executed.timestamp == unparseable.timestamp
    # The WS name keeps priority when it parses.
    ws_first = _map(_row(_LIVE_ASK_ROW, executed_at="2026-09-11T19:59:45Z"))
    assert ws_first is not None
    assert ws_first.timestamp == datetime(2026, 9, 11, 19, 59, 45, tzinfo=UTC)


def test_no_parseable_timestamp_drops_row_naming_alias_group(
    caplog: pytest.LogCaptureFixture,
) -> None:
    row = _row(_LIVE_ASK_ROW, executed_at="not-a-time")
    del row["created_at"]
    with caplog.at_level(logging.ERROR):
        assert _map(row) is None
    msg = caplog.records[-1].getMessage()
    assert "row keys=" in msg
    assert "'executed_at|created_at'" in msg
    assert "'option_type|type'" in msg
    assert "'premium|total_premium'" in msg


def test_ws_names_keep_priority_over_flow_alert_names() -> None:
    rp = _map(
        _row(
            _LIVE_ASK_ROW,
            option_type="put",
            premium="1000",
            spot_price="360.00",
            implied_volatility=0.30,
            is_iso=False,
            has_sweep=True,
            alert_type="sweep",
        ),
    )
    assert rp is not None
    assert rp.option_type == "put"
    assert rp.premium_paid == Decimal("1000")
    assert rp.spot_price == Decimal("360.00")
    assert rp.implied_volatility == 0.30
    assert rp.is_iso is False
    assert rp.source_tags == ("uw:sweep",)


def test_type_alias_accepts_only_call_or_put() -> None:
    assert _map(_row(_LIVE_ASK_ROW, type="put")) is not None
    assert _map(_row(_LIVE_ASK_ROW, type="stock")) is None


@pytest.mark.parametrize(
    ("iv_end", "open_interest", "expected_iv", "expected_oi"),
    [
        ("0.1824", "25922", 0.1824, 25922),
        (0.2, 100, 0.2, 100),
        (None, None, None, None),
        ("-0.1", -5, None, None),
        ("NaN", "abc", None, None),
        (True, True, None, None),
    ],
)
def test_iv_end_and_open_interest_parsing(
    iv_end: object,
    open_interest: object,
    expected_iv: float | None,
    expected_oi: int | None,
) -> None:
    rp = _map(_row(_LIVE_ASK_ROW, iv_end=iv_end, open_interest=open_interest))
    assert rp is not None
    assert rp.implied_volatility == (
        pytest.approx(expected_iv) if expected_iv is not None else None
    )
    assert rp.open_interest == expected_oi


def test_missing_underlying_price_tags_no_spot() -> None:
    row = _row(_LIVE_ASK_ROW)
    del row["underlying_price"]
    rp = _map(row)
    assert rp is not None
    assert rp.spot_price == Decimal("0")
    assert "uw:no-spot" in rp.source_tags


def test_bid_and_ask_have_no_fallback() -> None:
    no_bid = _row(_LIVE_ASK_ROW)
    del no_bid["bid"]
    assert _map(no_bid) is None
    no_ask = _row(_LIVE_ASK_ROW, ask=None)
    assert _map(no_ask) is None


# ---------------------------------------------------------------------------
# UnusualWhalesRestFlowSource on flow-alerts
# ---------------------------------------------------------------------------


def test_rest_flow_reexports_flow_alerts_path() -> None:
    assert rest_flow.FLOW_ALERTS_PATH == FLOW_ALERTS_PATH
    assert not hasattr(rest_flow, "RECENT_FLOW_PATH")


@pytest.mark.asyncio
async def test_rest_source_lookback_sends_epoch_window() -> None:
    client = _PagedClient([{"data": [_LIVE_ASK_ROW]}])
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["aapl", "MSFT", "nvda"],
        lookback=timedelta(minutes=60),
        before=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
    )
    prints = await _drain(src)
    assert len(prints) == 1
    assert client.calls == [
        (
            FLOW_ALERTS_PATH,
            {
                "ticker_symbol": "AAPL,MSFT,NVDA",
                "limit": 200,
                "older_than": _EPOCH_2000Z,
                "newer_than": _EPOCH_1900Z,
            },
        ),
    ]
    assert src.pages_fetched == 1
    assert src.cutoff == datetime(2026, 9, 11, 19, 0, tzinfo=UTC)
    assert src.truncated is False


@pytest.mark.asyncio
async def test_rest_source_yields_live_rows_in_event_time_order() -> None:
    # The endpoint returns newest first; the source yields oldest first (D9).
    page = [_LIVE_BID_ROW, _LIVE_ASK_ROW, _LIVE_SWEEP_ROW]
    client = _PagedClient([{"data": page}])
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        before=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
    )
    prints = await _drain(src)
    assert [p.source_event_id for p in prints] == [
        f"uw-{_LIVE_SWEEP_ROW['id']}",
        f"uw-{_LIVE_ASK_ROW['id']}",
        f"uw-{_LIVE_BID_ROW['id']}",
    ]
    assert [p.fill_side for p in prints] == ["at_bid", "at_ask", "at_bid"]
    assert [p.is_iso for p in prints] == [True, False, False]
    assert src.rows_fetched == 3
    assert src.rows_mapped == 3
    assert src.rows_dropped == 0
    assert src.cutoff == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_rest_source_drops_ivless_print_with_error_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ivless = _row(_LIVE_BID_ROW, iv_end=None)
    client = _PagedClient([{"data": [ivless, _LIVE_ASK_ROW]}])
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        before=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
    )
    with caplog.at_level(logging.ERROR):
        prints = await _drain(src)
    assert [p.source_event_id for p in prints] == [f"uw-{_LIVE_ASK_ROW['id']}"]
    assert src.rows_fetched == 2
    assert src.rows_mapped == 1
    assert src.rows_dropped == 1
    assert src.dropped_key_samples == [tuple(sorted(ivless.keys()))]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "implied_volatility" in errors[0]
    assert "row keys=" in errors[0]
    assert "'iv_end'" in errors[0]


@pytest.mark.asyncio
async def test_rest_source_drops_oiless_print() -> None:
    oiless = _row(_LIVE_BID_ROW, open_interest=None)
    client = _PagedClient([{"data": [oiless, _LIVE_ASK_ROW]}])
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        before=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
    )
    prints = await _drain(src)
    assert len(prints) == 1
    assert src.rows_mapped == 1
    assert src.rows_dropped == 1


@pytest.mark.asyncio
async def test_rest_source_walks_pages_and_reports_them() -> None:
    step = timedelta(seconds=30)
    newest = datetime(2026, 9, 11, 19, 59, 0, tzinfo=UTC)
    page1 = _series(newest, FLOW_ALERTS_PAGE_LIMIT, step=step, prefix="a")
    page2 = [page1[-1], _row(_LIVE_SWEEP_ROW, created_at=_iso(newest - 250 * step))]
    client = _PagedClient([{"data": page1}, {"data": page2}])
    src = UnusualWhalesRestFlowSource(
        client=client,  # type: ignore[arg-type]
        tickers=["TSLA"],
        before=datetime(2026, 9, 11, 20, 0, tzinfo=UTC),
    )
    prints = await _drain(src)
    assert src.pages_fetched == 2
    assert src.rows_fetched == FLOW_ALERTS_PAGE_LIMIT + 1
    assert len(prints) == FLOW_ALERTS_PAGE_LIMIT + 1
    assert prints[0].source_event_id == f"uw-{_LIVE_SWEEP_ROW['id']}"
    stamps = [p.timestamp for p in prints]
    assert stamps == sorted(stamps)
