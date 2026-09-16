"""Phase 5.2.A3 live Unusual Whales tests for the Alfa Board tape and ticker-info endpoints.

These hit https://api.unusualwhales.com and check that the two endpoints the
board refresher added in A3 return real data in the shape probed on 2026-09-15:

  - ``GET /api/stock/{ticker}/net-prem-ticks`` (``alfa_disc/probe_chain_nbbo.md``)
  - ``GET /api/stock/{ticker}/info`` (``alfa_disc/probe_clusters.md``)

Gating:
  - ``integration`` marker.
  - Skipped cleanly when ``UNUSUAL_WHALES_API_KEY`` is not set.
  - The tape test skips (not fails) when UW has no ticks yet for the day
    (pre-market, weekend or holiday).

Run (key from .env, one command):
  set -a; source .env; set +a; uv run pytest tests/integration/test_alfa_live_tape.py -q

Request budget: 3 requests per full run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from webapp.board.netprem import NET_PREM_TICKS_PATH, parse_net_prem_ticks
from webapp.board.ticker_info import TICKER_INFO_PATH, TickerInfoView, parse_ticker_info

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

pytestmark = pytest.mark.integration

_TAPE_TICKER = "SPY"


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


def _client() -> UnusualWhalesClient:
    return UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings())


@pytest.mark.asyncio
async def test_net_prem_ticks_returns_per_minute_rows_oldest_first() -> None:
    client = _client()
    try:
        payload: dict[str, Any] = await client.request_json(NET_PREM_TICKS_PATH.format(ticker=_TAPE_TICKER))
    finally:
        await client.aclose()

    rows = payload.get("data")
    assert isinstance(rows, list)
    if not rows:
        pytest.skip(f"no net-prem ticks for {_TAPE_TICKER} yet (pre-market, weekend or holiday)")
    for key in (
        "date", "tape_time", "net_call_premium", "net_put_premium",
        "net_call_volume", "net_put_volume", "call_volume", "put_volume", "net_delta",
    ):
        assert key in rows[0], key
    for row in rows:
        assert isinstance(row["net_call_premium"], str)  # premiums arrive as strings
        assert isinstance(row["net_put_premium"], str)
        assert isinstance(row["call_volume"], int)

    minutes = parse_net_prem_ticks(payload)
    assert len(minutes) == len(rows)  # one row per minute, no repeats
    raw_times = [m.tape_time for m in minutes]
    assert raw_times == sorted(raw_times)
    parsed_in_payload_order = [
        datetime.fromisoformat(str(r["tape_time"]).replace("Z", "+00:00")) for r in rows
    ]
    assert parsed_in_payload_order == sorted(parsed_in_payload_order)  # oldest first
    assert all(t.second == 0 and t.microsecond == 0 and t.tzinfo is not None for t in raw_times)
    assert len({m.trade_date for m in minutes}) == 1
    assert raw_times[-1] <= datetime.now(UTC) + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_stock_info_returns_issue_type_and_sector() -> None:
    client = _client()
    try:
        stock: dict[str, Any] = await client.request_json(TICKER_INFO_PATH.format(ticker="NVDA"))
        fund: dict[str, Any] = await client.request_json(TICKER_INFO_PATH.format(ticker="SPY"))
    finally:
        await client.aclose()

    assert isinstance(stock.get("data"), dict)  # one object, not a list
    assert isinstance(fund.get("data"), dict)
    nvda = parse_ticker_info(stock, "NVDA")
    spy = parse_ticker_info(fund, "SPY")
    assert nvda.issue_type == "Common Stock"
    assert isinstance(nvda.sector, str) and nvda.sector
    assert spy.issue_type == "ETF"
    assert spy.sector is None

    now = datetime.now(UTC)
    assert TickerInfoView(spy.ticker, spy.issue_type, spy.sector, now).fund_or_index is True
    assert TickerInfoView(nvda.ticker, nvda.issue_type, nvda.sector, now).fund_or_index is False
