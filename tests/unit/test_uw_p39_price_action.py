"""Phase 3.9.11 tests: UW price action — no look-ahead bar close, ET-date cache key.

Contract: ``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.8.

Pins:
  - ``spot_at`` is the close of the latest bar with ``end <= at``; a bar
    starting exactly at ``at``, or still forming at a mid-minute ``at``,
    is excluded.
  - ``spot_lookback_ago`` is the open of the earliest completed bar with
    ``start >= at - lookback``.
  - Bar end is the parsed ``end_time``; when it is missing, unparseable
    or not after ``start_time`` it falls back to start + candle duration.
  - Cache key is (ticker, ET date of ``at``, candle_size): two lookbacks
    on the same ET day share one fetch; UTC-midnight does not split an
    ET day, and a new ET day fetches again.
  - Non-intraday candle sizes are rejected at construction.
  - M23 stage end-to-end: the forming bar can no longer flip the branch.

Fixture rows are trimmed from a live capture of
``GET /api/stock/AAPL/ohlc/1m?date=2026-09-11`` (2026-09-14): string
prices, ``Z`` timestamps, newest first. No network.
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
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)

_AAPL_1M = "/api/stock/AAPL/ohlc/1m"

# Live rows (AAPL 1m, 2026-09-11 RTH), newest first, verbatim except that
# the day's other ~800 bars are dropped.
_LIVE_AAPL_20260911: list[dict[str, Any]] = [
    {"close": "332.635", "high": "332.78", "low": "332.62", "open": "332.76",
     "start_time": "2026-09-11T19:31:00Z", "volume": 57706,
     "end_time": "2026-09-11T19:32:00Z", "total_volume": 38707034,
     "market_time": "r"},
    {"close": "332.76", "high": "332.76", "low": "332.64", "open": "332.68",
     "start_time": "2026-09-11T19:30:00Z", "volume": 52947,
     "end_time": "2026-09-11T19:31:00Z", "total_volume": 38649328,
     "market_time": "r"},
    {"close": "332.6401", "high": "332.72", "low": "332.63", "open": "332.64",
     "start_time": "2026-09-11T19:29:00Z", "volume": 39581,
     "end_time": "2026-09-11T19:30:00Z", "total_volume": 38596381,
     "market_time": "r"},
    {"close": "332.63", "high": "332.7341", "low": "332.608", "open": "332.73",
     "start_time": "2026-09-11T19:28:00Z", "volume": 37995,
     "end_time": "2026-09-11T19:29:00Z", "total_volume": 38556800,
     "market_time": "r"},
    {"close": "332.67", "high": "332.705", "low": "332.5505", "open": "332.555",
     "start_time": "2026-09-11T19:15:00Z", "volume": 49343,
     "end_time": "2026-09-11T19:16:00Z", "total_volume": 36972167,
     "market_time": "r"},
    {"close": "332.57", "high": "332.63", "low": "332.5", "open": "332.59",
     "start_time": "2026-09-11T19:01:00Z", "volume": 45694,
     "end_time": "2026-09-11T19:02:00Z", "total_volume": 36190555,
     "market_time": "r"},
    {"close": "332.59", "high": "332.67", "low": "332.4", "open": "332.5114",
     "start_time": "2026-09-11T19:00:00Z", "volume": 103681,
     "end_time": "2026-09-11T19:01:00Z", "total_volume": 36144861,
     "market_time": "r"},
]


class _DatedFakeClient:
    """Returns a scripted payload per (path, ``date`` param); records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[tuple[str, str], Any] = {}

    def stub(self, path: str, day: str, response: Any) -> None:
        self.responses[(path, day)] = response

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> Any:
        del method
        self.calls.append((path, params))
        day = str((params or {}).get("date", ""))
        return self.responses.get((path, day), {"data": []})


def _provider(
    client: _DatedFakeClient,
    *,
    candle_size: str = "1m",
    ttl_seconds: int = 300,
) -> UnusualWhalesPriceActionProvider:
    return UnusualWhalesPriceActionProvider(
        client=client,  # type: ignore[arg-type]
        settings=UnusualWhalesSettings(
            cache_ttl=UnusualWhalesProviderCacheTTL(
                intraday_price_seconds=ttl_seconds,
            ),
        ),
        candle_size=candle_size,
    )


def _live_client(rows: list[dict[str, Any]] | None = None) -> _DatedFakeClient:
    client = _DatedFakeClient()
    payload = _LIVE_AAPL_20260911 if rows is None else rows
    client.stub(_AAPL_1M, "2026-09-11", {"data": [dict(r) for r in payload]})
    return client


# ===========================================================================
# Completed-bar rule on live-shaped rows
# ===========================================================================


@pytest.mark.asyncio
async def test_mid_minute_event_uses_previous_completed_bar() -> None:
    """at=19:30:30: the 19:30 bar is still forming → spot_at is 19:29's close."""
    client = _live_client()
    at = datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC)
    out = await _provider(client).get_intraday_price_movement("aapl", at, 30)
    assert out is not None
    assert out.ticker == "AAPL"
    assert out.as_of == at
    # 19:30 bar (close 332.76, ends 19:31) excluded; 19:29 bar ends 19:30.
    assert out.spot_at == Decimal("332.6401")
    # window_start 19:00:30 → the 19:00 bar starts before it; 19:01 is first.
    assert out.spot_lookback_ago == Decimal("332.59")
    assert out.move_pct == pytest.approx(
        float((Decimal("332.6401") - Decimal("332.59")) / Decimal("332.59")),
    )
    assert out.lookback_minutes_actual == 30
    assert client.calls == [(_AAPL_1M, {"date": "2026-09-11"})]


@pytest.mark.asyncio
async def test_bar_starting_exactly_at_event_is_excluded() -> None:
    """at=19:30:00: the 19:30 bar is excluded, the 19:29 bar (end == at) included."""
    client = _live_client()
    out = await _provider(client).get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, tzinfo=UTC), 30,
    )
    assert out is not None
    assert out.spot_at == Decimal("332.6401")
    # window_start 19:00:00 → the 19:00 bar (start == window_start) is first.
    assert out.spot_lookback_ago == Decimal("332.5114")


@pytest.mark.asyncio
async def test_no_completed_bar_in_window_returns_none() -> None:
    """lookback=1 at 19:30:30: only the forming 19:30 bar starts in window."""
    client = _live_client()
    out = await _provider(client).get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC), 1,
    )
    assert out is None


@pytest.mark.asyncio
async def test_bars_after_event_never_used() -> None:
    """An event before every returned bar yields None, not a future price."""
    client = _live_client()
    out = await _provider(client).get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 18, 59, 59, tzinfo=UTC), 30,
    )
    assert out is None


# ===========================================================================
# Bar end: end_time when present, else start + candle duration
# ===========================================================================


def _without_end_time() -> list[dict[str, Any]]:
    return [
        {k: v for k, v in row.items() if k != "end_time"}
        for row in _LIVE_AAPL_20260911
    ]


def _with_end_time(value: Any) -> list[dict[str, Any]]:
    return [{**row, "end_time": value} for row in _LIVE_AAPL_20260911]


def _with_end_equal_to_start() -> list[dict[str, Any]]:
    return [{**row, "end_time": row["start_time"]} for row in _LIVE_AAPL_20260911]


@pytest.mark.asyncio
async def test_end_time_missing_falls_back_to_start_plus_one_minute() -> None:
    client = _live_client(_without_end_time())
    provider = _provider(client, ttl_seconds=0)
    mid_minute = await provider.get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC), 30,
    )
    on_minute = await provider.get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, tzinfo=UTC), 30,
    )
    assert mid_minute is not None and on_minute is not None
    # 19:30 bar → fallback end 19:31 > at; 19:29 bar → fallback end 19:30 <= at.
    assert mid_minute.spot_at == Decimal("332.6401")
    assert on_minute.spot_at == Decimal("332.6401")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows_factory",
    [
        lambda: _with_end_time("not-a-timestamp"),
        lambda: _with_end_time(None),
        lambda: _with_end_time(1757619060000),
        _with_end_equal_to_start,
    ],
    ids=["unparseable", "null", "epoch-int", "end-equals-start"],
)
async def test_unusable_end_time_falls_back_to_start_plus_candle(
    rows_factory: Any,
) -> None:
    """A degenerate end_time can never admit the bar starting at ``at``."""
    client = _live_client(rows_factory())
    out = await _provider(client).get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, tzinfo=UTC), 30,
    )
    assert out is not None
    assert out.spot_at == Decimal("332.6401")


@pytest.mark.asyncio
async def test_end_time_later_than_candle_duration_is_respected() -> None:
    """A vendor end_time beyond start+1m keeps the bar out until it ends."""
    rows = [dict(r) for r in _LIVE_AAPL_20260911]
    rows[2]["end_time"] = "2026-09-11T19:31:00Z"  # 19:29 bar published late
    client = _live_client(rows)
    out = await _provider(client).get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC), 30,
    )
    assert out is not None
    assert out.spot_at == Decimal("332.63")  # 19:28 bar close


@pytest.mark.asyncio
async def test_five_minute_candle_falls_back_to_five_minute_end() -> None:
    client = _DatedFakeClient()
    client.stub("/api/stock/AAPL/ohlc/5m", "2026-09-11", {"data": [
        {"open": "333.00", "close": "340.00", "market_time": "r",
         "start_time": "2026-09-11T19:30:00Z"},
        {"open": "332.70", "close": "332.90", "market_time": "r",
         "start_time": "2026-09-11T19:25:00Z"},
        {"open": "332.10", "close": "332.20", "market_time": "r",
         "start_time": "2026-09-11T19:05:00Z"},
    ]})
    out = await _provider(client, candle_size="5m").get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 32, tzinfo=UTC), 30,
    )
    assert client.calls[0][0] == "/api/stock/AAPL/ohlc/5m"
    assert out is not None
    assert out.spot_at == Decimal("332.90")  # 19:30 bar ends 19:35 > at
    assert out.spot_lookback_ago == Decimal("332.10")


@pytest.mark.parametrize("candle_size", ["1d", "1w", "2m", ""])
def test_non_intraday_candle_size_rejected(candle_size: str) -> None:
    with pytest.raises(ValueError, match="candle_size"):
        _provider(_DatedFakeClient(), candle_size=candle_size)


# ===========================================================================
# Cache key: (ticker, ET date, candle_size)
# ===========================================================================


@pytest.mark.asyncio
async def test_two_lookbacks_same_day_share_one_fetch() -> None:
    client = _live_client()
    provider = _provider(client)
    at = datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC)
    wide = await provider.get_intraday_price_movement("AAPL", at, 30)
    narrow = await provider.get_intraday_price_movement("aapl", at, 5)
    later = await provider.get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 32, tzinfo=UTC), 20,
    )
    assert len(client.calls) == 1
    assert wide is not None and narrow is not None and later is not None
    assert wide.spot_lookback_ago == Decimal("332.59")
    # window_start 19:25:30 → 19:28 bar is the earliest completed bar.
    assert narrow.spot_lookback_ago == Decimal("332.73")
    assert narrow.lookback_minutes_actual == 5
    # at 19:32 the 19:31 bar (end 19:32) is complete; window_start 19:12
    # → the 19:15 bar is the earliest.
    assert later.spot_at == Decimal("332.635")
    assert later.spot_lookback_ago == Decimal("332.555")


@pytest.mark.asyncio
async def test_cache_key_uses_et_date_not_utc_date() -> None:
    """00:10Z on 09-12 is 20:10 ET on 09-11; 13:45Z on 09-12 is a new ET day."""
    client = _live_client()
    client.stub(_AAPL_1M, "2026-09-12", {"data": []})
    provider = _provider(client)
    rth = await provider.get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC), 30,
    )
    same_et_day = await provider.get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 12, 0, 10, tzinfo=UTC), 30,
    )
    next_et_day = await provider.get_intraday_price_movement(
        "AAPL", datetime(2026, 9, 12, 13, 45, tzinfo=UTC), 30,
    )
    assert rth is not None
    assert same_et_day is None  # no bars in 23:40-00:10Z in the trimmed day
    assert next_et_day is None
    assert [c[1] for c in client.calls] == [
        {"date": "2026-09-11"},
        {"date": "2026-09-12"},
    ]


@pytest.mark.asyncio
async def test_ticker_case_shares_cache_entry() -> None:
    client = _live_client()
    provider = _provider(client)
    at = datetime(2026, 9, 11, 19, 30, 30, tzinfo=UTC)
    await provider.get_intraday_price_movement("aapl", at, 30)
    await provider.get_intraday_price_movement("AAPL", at, 30)
    assert client.calls == [(_AAPL_1M, {"date": "2026-09-11"})]


# ===========================================================================
# M23 stage end-to-end
# ===========================================================================


def _call_event(ts: datetime) -> EnrichedEvent:
    op = OptionsPrint(
        event_id="p39-m23-1",
        timestamp=ts,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("105.00"),
        expiry=date(2024, 1, 19),
        dte=4,
        spot_price=Decimal("100.20"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.20,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=10000,
        source_agreement=SourceAgreement(
            sources_seen=("unusual_whales",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


@pytest.mark.asyncio
async def test_m23_stage_does_not_score_off_forming_bar() -> None:
    """The 15:30 bar (+1.8%) prints after the 15:30:00 event and must not count.

    Completed bars give +0.20%, under the default profile's confirmation
    threshold → neutral. January date, so the stage's session clamp does
    not shorten the 30-minute window.
    """
    client = _DatedFakeClient()
    client.stub(_AAPL_1M, "2024-01-15", {"data": [
        {"open": "100.20", "close": "102.00", "market_time": "r",
         "start_time": "2024-01-15T15:30:00Z", "end_time": "2024-01-15T15:31:00Z"},
        {"open": "100.10", "close": "100.20", "market_time": "r",
         "start_time": "2024-01-15T15:29:00Z", "end_time": "2024-01-15T15:30:00Z"},
        {"open": "100.00", "close": "100.05", "market_time": "r",
         "start_time": "2024-01-15T15:00:00Z", "end_time": "2024-01-15T15:01:00Z"},
    ]})
    stage = PriceConfirmationStage(provider=_provider(client))
    event = _call_event(datetime(2024, 1, 15, 15, 30, tzinfo=UTC))
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    m23 = ctx.profile.scoring.modules.m23
    assert event.price_confirmation_score == m23.neutral_score
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "neutral"
    assert stage.last_execution_metadata["move_pct"] == "0.002000"
