"""Phase 5.2.A2 live Unusual Whales tests for the Alfa Board quote endpoints.

These hit https://api.unusualwhales.com and check that the two endpoints the
board refresher uses return real rows in the shape probed on 2026-09-15
(``alfa_disc/probe_chain_nbbo.md``):

  - ``GET /api/stock/{ticker}/option-contracts?option_symbol[]=...``
  - ``GET /api/option-contract/{symbol}/flow?limit=1``

Gating:
  - ``integration`` marker.
  - Skipped cleanly when ``UNUSUAL_WHALES_API_KEY`` is not set.
  - Contracts are discovered from the newest real flow alerts
    (``/api/option-trades/flow-alerts``), so no contract or date is
    hard-coded. Skipped (not failed) when UW returns no alerts (holiday).

Run (key from .env, one command):
  set -a; source .env; set +a; uv run pytest tests/integration/test_alfa_live_quotes.py -q

Request budget: 4 requests per full run (2 per test).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from webapp.board.quotes import (
    CONTRACT_FLOW_PATH,
    OPTION_CONTRACTS_PATH,
    SYMBOL_PARAM,
    parse_contract_flow,
    parse_option_contract_rows,
)

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.flow_alerts import FLOW_ALERTS_PATH

pytestmark = pytest.mark.integration

_MAX_SYMBOLS = 10  # a handful of real contracts is enough to check shape and diff


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


def _client() -> UnusualWhalesClient:
    return UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings())


async def _alert_contracts(client: UnusualWhalesClient) -> tuple[str, list[str]]:
    """The newest alert's ticker and up to 10 distinct contracts of that ticker."""
    response = await client.request_json(FLOW_ALERTS_PATH, params={"limit": 50})
    found: list[tuple[str, str, str]] = []
    for row in response.get("data", []):
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker") or row.get("underlying_symbol")
        chain = row.get("option_chain") or row.get("option_symbol")
        if isinstance(ticker, str) and ticker and isinstance(chain, str) and chain:
            found.append((str(row.get("created_at", "")), ticker.upper(), chain.upper()))
    if not found:
        pytest.skip("UW returned no flow alerts with a contract to anchor on (market holiday?).")
    found.sort(reverse=True)
    ticker = found[0][1]
    symbols = list(dict.fromkeys(chain for _created, t, chain in found if t == ticker))
    return ticker, symbols[:_MAX_SYMBOLS]


@pytest.mark.asyncio
async def test_option_contracts_symbol_filter_returns_real_quotes() -> None:
    client = _client()
    try:
        ticker, symbols = await _alert_contracts(client)
        bogus = f"{ticker}991231C99999000"  # a well-formed symbol no exchange lists
        requested = [*symbols, bogus]
        payload: dict[str, Any] = await client.request_json(
            OPTION_CONTRACTS_PATH.format(ticker=ticker), params={SYMBOL_PARAM: requested},
        )
        daily_count = client.last_daily_request_count
    finally:
        await client.aclose()

    rows = payload.get("data")
    assert isinstance(rows, list) and rows, f"no option-contracts rows for {ticker} {symbols}"
    returned = {r["option_symbol"] for r in rows}
    assert bogus not in returned
    assert returned <= set(requested)
    for key in ("option_symbol", "nbbo_bid", "nbbo_ask", "last_price", "volume", "open_interest", "last_tape_time"):
        assert key in rows[0], key
    for row in rows:
        for price_key in ("nbbo_bid", "nbbo_ask"):
            assert row[price_key] is None or isinstance(row[price_key], str)  # prices arrive as strings
        assert isinstance(row["volume"], int)

    quotes = {q.option_symbol: q for q in parse_option_contract_rows(payload, ticker, requested)}
    assert quotes[bogus].returned is False
    traded = [q for q in quotes.values() if q.returned and q.volume]
    assert traded, "flow-alert contracts should have traded today"
    for q in traded:
        assert q.nbbo_bid is not None and q.nbbo_ask is not None, q.option_symbol
        assert 0 <= q.nbbo_bid <= q.nbbo_ask, q.option_symbol

    assert isinstance(daily_count, int) and daily_count > 0  # x-uw-daily-req-count captured


@pytest.mark.asyncio
async def test_contract_flow_limit_one_returns_nbbo_sizes_at_the_last_print() -> None:
    client = _client()
    try:
        _ticker, symbols = await _alert_contracts(client)
        symbol = symbols[0]
        payload: dict[str, Any] = await client.request_json(
            CONTRACT_FLOW_PATH.format(symbol=symbol), params={"limit": 1},
        )
    finally:
        await client.aclose()

    rows = payload.get("data")
    assert isinstance(rows, list)
    if not rows:
        pytest.skip(f"no prints for {symbol} yet")
    assert len(rows) == 1  # limit=1 honoured
    for key in ("executed_at", "nbbo_bid_size", "nbbo_ask_size", "nbbo_bid_time", "nbbo_ask_time"):
        assert key in rows[0], key

    depth = parse_contract_flow(payload, symbol)
    assert depth is not None
    assert depth.option_symbol == symbol
    assert depth.nbbo_bid_size is None or depth.nbbo_bid_size >= 0
    assert depth.nbbo_ask_size is None or depth.nbbo_ask_size >= 0
    later_than_now = datetime.now(UTC) + timedelta(minutes=1)
    for stamp in (depth.nbbo_bid_time, depth.nbbo_ask_time):
        if stamp is not None:
            assert stamp.tzinfo is not None
            assert stamp <= later_than_now
