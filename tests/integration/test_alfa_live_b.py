"""Phase 5.2 FAZ B live Unusual Whales endpoint tests (data layers B2a, B4a, B5a, B6a).

One test per endpoint the FAZ B data layers call. Each runs the real job (or its
fetch) against the live API and a tmp sqlite database, then asserts real rows in
the probed shape (probe 2026-09-15). No contract, expiry or date is hard-coded.

Gating:
  - ``integration`` marker;
  - skipped when ``UNUSUAL_WHALES_API_KEY`` is not set.

Run once with the key (never echo it):
  set -a; source .env; set +a; uv run pytest tests/integration/test_alfa_live_b.py -q
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from webapp.board import atm
from webapp.board.db import make_engine, session_factory
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_TICKER = "SPY"


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


def _client() -> UnusualWhalesClient:
    return UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings())


def _settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


def _sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    atm.ensure_atm_tables(engine)
    return session_factory(engine)


# ---------------------------------------------------------------------------
# B2a
# ---------------------------------------------------------------------------


async def test_live_expiry_breakdown(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    try:
        report = await atm.refresh_expiry_breakdown(
            client, sessions, tickers=[_TICKER], settings=_settings(),
            now=datetime.now(UTC),
        )
    finally:
        await client.aclose()
    assert report.degraded == () and report.no_data == ()
    with sessions() as s:
        listed = atm.load_listed_expiries(s, _TICKER)
    assert len(listed) >= 5, listed
    assert list(listed) == sorted(listed)
    assert listed[-1] > date.today()


async def test_live_atm_chains(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    settings = _settings()
    now = datetime.now(UTC)
    try:
        await atm.refresh_expiry_breakdown(
            client, sessions, tickers=[_TICKER], settings=settings, now=now,
        )
        report = await atm.refresh_atm_chains(
            client, sessions, wanted={_TICKER: []}, settings=settings, now=now,
        )
    finally:
        await client.aclose()
    assert report.degraded == () and report.malformed_rows == 0
    assert report.stored_rows >= 1, report
    with sessions() as s:
        rows = atm.load_atm(s, _TICKER)
    assert rows
    for row in rows:
        assert row.stock_price is not None and row.stock_price > 0
        # One strike per expiry, near spot (ATM is the nearest listed strike).
        assert abs(row.strike - row.stock_price) / row.stock_price < 0.05
        assert row.call_ask is not None or row.put_ask is not None
