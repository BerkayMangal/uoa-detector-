"""Phase 3.9.12 live Unusual Whales endpoint tests.

These hit the real API at https://api.unusualwhales.com and assert that
every Phase 3.9 provider returns REAL data parsed the way the Phase 3.9
acceptance contract (docs/phase-3.9-uw-endpoint-correction-acceptance.md)
requires. They exist so an endpoint is never again "written but not tried".

Gating:
  - ``integration`` marker.
  - Skipped cleanly when ``UNUSUAL_WHALES_API_KEY`` is not set.
  - Skipped (not failed) when UW returns no flow alerts to anchor on
    (holiday / long weekend) — every other assertion is anchored on a real
    alert, so no contract or date is hard-coded.

Run (key from .env):
  set -a; source .env; set +a
  uv run pytest -m integration tests/integration/test_uw_live_endpoints.py -v

Request budget: roughly 25 requests per full run (UW daily token limit 30k).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.flow_alerts import (
    FLOW_ALERTS_PATH,
)
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)
from uoa_detector.sources.unusual_whales.rest_flow import (
    UnusualWhalesRestFlowSource,
)

pytestmark = pytest.mark.integration

_ET = ZoneInfo("America/New_York")
_ANCHOR_TICKER = "SPY"  # always has flow alerts on a trading day


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


def _client() -> UnusualWhalesClient:
    return UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings())


def _parse_ts(raw: object) -> datetime:
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _anchor_alert(client: UnusualWhalesClient) -> dict[str, Any]:
    """Newest real flow alert on the anchor ticker; skip if UW has none."""
    resp = await client.request_json(
        FLOW_ALERTS_PATH, params={"ticker_symbol": _ANCHOR_TICKER, "limit": 50},
    )
    rows = [r for r in resp.get("data", []) if isinstance(r, dict)]
    if not rows:
        pytest.skip("UW returned no flow alerts to anchor on (market holiday?).")
    return max(rows, key=lambda r: _parse_ts(r["created_at"]))


@pytest.mark.asyncio
async def test_flow_alerts_rows_carry_the_fields_the_mapper_requires() -> None:
    client = _client()
    try:
        resp = await client.request_json(
            FLOW_ALERTS_PATH, params={"ticker_symbol": "AAPL,SPY", "limit": 50},
        )
    finally:
        await client.aclose()
    rows = resp.get("data", [])
    if not rows:
        pytest.skip("no flow alerts returned")
    required = (
        "id", "ticker", "type", "strike", "expiry", "created_at", "price",
        "bid", "ask", "iv_end", "total_premium", "underlying_price",
        "total_ask_side_prem", "total_bid_side_prem",
    )
    for row in rows:
        missing = [k for k in required if row.get(k) is None]
        assert not missing, f"flow alert row missing {missing}: keys={sorted(row)}"


@pytest.mark.asyncio
async def test_flow_alerts_epoch_cursor_window_is_honoured() -> None:
    client = _client()
    try:
        anchor = await _anchor_alert(client)
        end = _parse_ts(anchor["created_at"])
        start = end - timedelta(minutes=30)
        resp = await client.request_json(
            FLOW_ALERTS_PATH,
            params={
                "ticker_symbol": _ANCHOR_TICKER,
                "limit": 200,
                "newer_than": int(start.timestamp()),
                "older_than": int(end.timestamp()) + 1,
            },
        )
    finally:
        await client.aclose()
    rows = resp.get("data", [])
    assert rows, "the anchor alert itself must be inside its own window"
    for row in rows:
        ts = _parse_ts(row["created_at"])
        assert start - timedelta(seconds=1) <= ts <= end + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_rest_flow_source_maps_live_alerts_with_spot_and_iv() -> None:
    client = _client()
    try:
        anchor = await _anchor_alert(client)
        before = _parse_ts(anchor["created_at"]) + timedelta(seconds=1)
        src = UnusualWhalesRestFlowSource(
            client=client,
            tickers=["AAPL", "MSFT", "NVDA", "TSLA", "SPY"],
            lookback=timedelta(minutes=30),
            before=before,
        )
        prints = [rp async for rp in src.stream()]
    finally:
        await client.aclose()
    assert src.rows_fetched > 0
    assert prints, f"fetched={src.rows_fetched} mapped={src.rows_mapped} dropped={src.rows_dropped} samples={src.dropped_key_samples}"
    for rp in prints:
        assert rp.spot_price > 0
        assert rp.implied_volatility is not None
        assert rp.bid >= 0 and rp.ask >= rp.bid
        assert before - timedelta(minutes=30) <= rp.timestamp <= before


@pytest.mark.asyncio
async def test_dealer_gamma_net_is_usd_per_one_percent_not_share_gamma() -> None:
    client = _client()
    settings = UnusualWhalesSettings()
    try:
        anchor = await _anchor_alert(client)
        at = _parse_ts(anchor["created_at"])
        provider = UnusualWhalesDealerGammaProvider(client=client, settings=settings)
        aggregate = await provider.aggregate_for_ticker(_ANCHOR_TICKER, at)
        et_date = at.astimezone(_ET).date().isoformat()
        strikes = await client.request_json(
            f"/api/stock/{_ANCHOR_TICKER}/greek-exposure/strike", params={"date": et_date},
        )
    finally:
        await client.aclose()
    assert aggregate is not None
    assert aggregate.as_of <= at
    share_gamma = sum(
        (Decimal(r["call_gex"]) + Decimal(r["put_gex"]) for r in strikes["data"]),
        start=Decimal("0"),
    )
    # USD/1% = gamma * OI * price^2 while share gamma = gamma * OI * 100, so for
    # an underlying above ~$100 the USD figure is orders of magnitude larger.
    assert abs(aggregate.net_gamma_dollars) > abs(share_gamma) * 10


@pytest.mark.asyncio
async def test_iv_rank_is_on_a_0_to_100_scale_and_not_from_the_future() -> None:
    client = _client()
    try:
        anchor = await _anchor_alert(client)
        at = _parse_ts(anchor["created_at"])
        provider = UnusualWhalesIVHistoryProvider(client=client, settings=UnusualWhalesSettings())
        snap = await provider.iv_rank_at(
            ticker=_ANCHOR_TICKER,
            strike=Decimal(str(anchor["strike"])),
            expiry=datetime.fromisoformat(str(anchor["expiry"])).date(),
            option_type="call" if anchor["type"] == "call" else "put",
            at=at,
        )
    finally:
        await client.aclose()
    assert snap is not None
    assert snap.iv_rank_252d is not None
    assert 0.0 <= snap.iv_rank_252d <= 100.0
    assert snap.as_of <= at
    assert snap.implied_volatility > 0


@pytest.mark.asyncio
async def test_dark_pool_prints_stay_inside_the_requested_window() -> None:
    client = _client()
    try:
        anchor = await _anchor_alert(client)
        before = _parse_ts(anchor["created_at"])
        window = timedelta(minutes=60)
        provider = UnusualWhalesDarkPoolProvider(client=client, settings=UnusualWhalesSettings())
        prints = await provider.recent_prints(_ANCHOR_TICKER, before, window)
    finally:
        await client.aclose()
    if not prints:
        pytest.skip("no dark-pool prints in the anchor window")
    for p in prints:
        assert before - window <= p.when <= before
        assert p.size > 0 and p.price > 0
        assert p.side_estimate in {"above_ask", "at_or_below_bid", "midpoint", "unknown"}


@pytest.mark.asyncio
async def test_catalyst_calendar_returns_real_earnings_for_aapl() -> None:
    client = _client()
    now = datetime.now(UTC)
    try:
        provider = UnusualWhalesCatalystCalendarProvider(client=client, settings=UnusualWhalesSettings())
        events = await provider.catalysts_in_window("AAPL", now - timedelta(days=120), now + timedelta(days=120))
    finally:
        await client.aclose()
    kinds = {e.kind for e in events}
    assert "earnings" in kinds, f"no earnings for AAPL within +-120d: {events}"
    assert all(e.ticker == "AAPL" for e in events)


@pytest.mark.asyncio
async def test_open_interest_rows_are_as_of_and_contract_level() -> None:
    client = _client()
    try:
        anchor = await _anchor_alert(client)
        at = _parse_ts(anchor["created_at"])
        provider = UnusualWhalesOpenInterestProvider(client=client, settings=UnusualWhalesSettings())
        kwargs: dict[str, Any] = {
            "ticker": _ANCHOR_TICKER,
            "strike": Decimal(str(anchor["strike"])),
            "expiry": datetime.fromisoformat(str(anchor["expiry"])).date(),
            "option_type": "call" if anchor["type"] == "call" else "put",
        }
        current = await provider.at(when=at, **kwargs)
        prior = await provider.at(when=at - timedelta(days=1), **kwargs)
    finally:
        await client.aclose()
    assert current is not None, f"no OI row for {kwargs} at {at}"
    assert current.open_interest >= 0
    assert current.as_of.astimezone(_ET).date() <= at.astimezone(_ET).date()
    if prior is not None:
        assert prior.as_of <= current.as_of


@pytest.mark.asyncio
async def test_sector_peers_are_ranked_by_marketcap_and_peer_flow_is_windowed() -> None:
    client = _client()
    settings = UnusualWhalesSettings()
    try:
        sector_map = UnusualWhalesSectorMapProvider(client=client, settings=settings)
        sector = await sector_map.sector_of("AAPL")
        peers = list(await sector_map.peers_of("AAPL"))
        anchor = await _anchor_alert(client)
        before = _parse_ts(anchor["created_at"])
        window = timedelta(minutes=30)
        peer_flow = UnusualWhalesPeerFlowProvider(client=client, settings=settings)
        events = await peer_flow.recent_flow(peers[:5], before, window)
    finally:
        await client.aclose()
    assert sector == "Technology"
    assert "AAPL" not in peers
    assert {"MSFT", "NVDA"} & set(peers[:5]), f"top peers not by marketcap: {peers[:5]}"
    for ev in events:
        assert before - window <= ev.when <= before
        assert ev.direction in {"bullish", "bearish", "neutral"}


@pytest.mark.asyncio
async def test_price_action_movement_uses_completed_bars() -> None:
    client = _client()
    try:
        anchor = await _anchor_alert(client)
        at = _parse_ts(anchor["created_at"])
        provider = UnusualWhalesPriceActionProvider(client=client, settings=UnusualWhalesSettings())
        movement = await provider.get_intraday_price_movement(_ANCHOR_TICKER, at, 30)
    finally:
        await client.aclose()
    assert movement is not None
    assert movement.spot_at > 0 and movement.spot_lookback_ago > 0
    spot = Decimal(str(anchor["underlying_price"]))
    assert abs(movement.spot_at - spot) / spot < Decimal("0.02")
