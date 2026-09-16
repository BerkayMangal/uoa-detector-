"""Phase 5.2.C2b live test: the outcome job's secondary endpoint.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 ("the dominant
contract's bid on the horizon day, from ``/api/option-contract/{occ}/historic``.
It is ``veri yok`` when the contract did not trade that day. The probe found
NBBO is null on zero-volume days, so no option P&L is invented").

This hits https://api.unusualwhales.com and checks the two things the daily
outcome job depends on:

  - the payload's shape: dated rows carrying ``nbbo_bid``, which
    ``parse_historic_bids`` turns into ``{day: bid}``;
  - the claim the contract rests on: on a row with ``volume == 0`` the NBBO is
    null, so a day the contract did not trade yields NO bid rather than a stale
    or zero one.

Gating:
  - ``integration`` marker;
  - skipped cleanly when ``UNUSUAL_WHALES_API_KEY`` is not set;
  - the contract is discovered from the newest real flow alerts, so no symbol
    or date is hard-coded. Skipped (not failed) when UW returns no alerts
    (a market holiday).

Request budget: 2 requests per run (one flow-alerts anchor, one historic).

Run (key from .env, one command):
  set -a; source .env; set +a; uv run pytest tests/integration/test_alfa_live_outcomes.py -q
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest
from pydantic import SecretStr
from webapp.board.outcome_job import OPTION_HISTORIC_PATH, parse_historic_bids

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.flow_alerts import FLOW_ALERTS_PATH

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


def _client() -> UnusualWhalesClient:
    return UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings())


async def _newest_contract(client: UnusualWhalesClient) -> str:
    """The newest flow alert's option contract, so the test anchors on a real symbol."""
    response = await client.request_json(FLOW_ALERTS_PATH, params={"limit": 50})
    found: list[tuple[str, str]] = []
    for row in response.get("data", []):
        if not isinstance(row, dict):
            continue
        chain = row.get("option_chain") or row.get("option_symbol")
        if isinstance(chain, str) and chain:
            found.append((str(row.get("created_at", "")), chain.upper()))
    if not found:
        pytest.skip("UW returned no flow alerts with a contract to anchor on (market holiday?).")
    found.sort(reverse=True)
    return found[0][1]


async def test_historic_serves_dated_bids_and_nothing_on_a_zero_volume_day() -> None:
    client = _client()
    try:
        symbol = await _newest_contract(client)
        payload: dict[str, Any] = await client.request_json(
            OPTION_HISTORIC_PATH.format(symbol=symbol),
        )
    finally:
        await client.aclose()

    rows = payload.get("chains")
    if not isinstance(rows, list):
        rows = payload.get("data")
    assert isinstance(rows, list) and rows, f"no historic rows for {symbol}"
    for key in ("date", "nbbo_bid", "volume"):
        assert key in rows[0], f"{key} missing from a /historic row: {sorted(rows[0])}"

    bids = parse_historic_bids(payload)
    assert bids, f"no dated bid parsed out of {len(rows)} rows for {symbol}"
    for day, bid in bids.items():
        assert isinstance(day, date)
        assert isinstance(bid, float)
        assert bid >= 0.0, (symbol, day, bid)

    # Contract §4's RULE: a day the contract did not trade is "veri yok".
    #
    # Its stated rationale does NOT hold live. §4 says "the probe found NBBO is
    # null on zero-volume days"; on 2026-09-16 this test found
    # SPXW260925C07730000 serving {"date": "2026-08-18", "volume": 0,
    # "nbbo_bid": "120.60"}. So the rule cannot rest on the payload being null,
    # and the parser enforces it: a zero-volume row contributes no bid whatever
    # its NBBO. That is what is pinned here, against the real endpoint.
    untraded = [
        row for row in rows
        if isinstance(row, dict) and row.get("volume") == 0 and isinstance(row.get("date"), str)
    ]
    if not untraded:
        pytest.skip(f"{symbol} traded on every row served; no zero-volume day to check.")
    for row in untraded:
        assert date.fromisoformat(row["date"]) not in bids, (symbol, row["date"], row["nbbo_bid"])
