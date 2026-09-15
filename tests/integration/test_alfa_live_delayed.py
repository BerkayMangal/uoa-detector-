"""Phase 5.2 FAZ D live Unusual Whales tests: daily closes and delayed families.

Each test makes exactly ONE UW request through a recording wrapper, runs the
board's own daily job on that response into a tmp sqlite database, and asserts
both the raw shape probed on 2026-09-15 and the rows the job stored.

Gating:
- ``integration`` marker;
- skipped cleanly when ``UNUSUAL_WHALES_API_KEY`` is not set.

Run (key from .env, one command):
  set -a; source .env; set +a; uv run pytest tests/integration/test_alfa_live_delayed.py -q

Request budget: 1 request per test.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from webapp.board.daily_close import OHLC_DAILY_PATH, load_closes, run_daily_close_job
from webapp.board.db import make_engine

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

pytestmark = pytest.mark.integration

_ET = ZoneInfo("America/New_York")


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


class _RecordingClient:
    """Forwards to the real client and keeps every (path, params, body)."""

    def __init__(self, inner: UnusualWhalesClient) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any]]] = []

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        body = await self._inner.request_json(path, params=params, method=method)
        self.calls.append((path, params, body))
        return body


@pytest.fixture
def live() -> _RecordingClient:
    return _RecordingClient(
        UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings()),
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'board.db'}")


@pytest.mark.asyncio
async def test_live_ohlc_daily_closes_job(live: _RecordingClient, engine: Engine) -> None:
    now = datetime.now(UTC)
    try:
        result = await run_daily_close_job(live, engine, ["SPY"], now=now)  # type: ignore[arg-type]
    finally:
        await live._inner.aclose()

    assert len(live.calls) == 1
    path, _params, body = live.calls[0]
    assert path == OHLC_DAILY_PATH.format(ticker="SPY")
    rows = [r for r in body.get("data", []) if isinstance(r, dict)]
    assert rows, "ohlc/1d returned no rows"
    assert {"date", "close", "market_time"} <= rows[0].keys()
    assert "r" in {r["market_time"] for r in rows}
    assert isinstance(rows[0]["close"], str)  # probed: numbers arrive as strings

    assert result.tickers[0].status == "ok"
    closes = load_closes(engine, "SPY")
    assert len(closes) >= 20
    assert all(c.close > 0 for c in closes)
    assert [c.day for c in closes] == sorted(c.day for c in closes)
    # Holiday and weekend slack: the newest stored close is within a week.
    assert (now.astimezone(_ET).date() - closes[-1].day).days <= 7
