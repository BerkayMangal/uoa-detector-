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
from webapp.board.delayed import (
    CONGRESS_RECENT_TRADES_PATH,
    build_delayed_evidence,
    load_delayed_records,
    refresh_congress,
)
from webapp.board.settings import DelayedSettings, load_board_settings

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

pytestmark = pytest.mark.integration

_ET = ZoneInfo("America/New_York")
_REPO = Path(__file__).resolve().parents[2]


def _settings() -> DelayedSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml").delayed


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


@pytest.mark.asyncio
async def test_live_congress_recent_trades(live: _RecordingClient, engine: Engine) -> None:
    now = datetime.now(UTC)
    try:
        result = await refresh_congress(live, engine, "NVDA", now=now, settings=_settings())  # type: ignore[arg-type]
    finally:
        await live._inner.aclose()

    assert [(p, q) for p, q, _ in live.calls] == [
        (CONGRESS_RECENT_TRADES_PATH, {"ticker": "NVDA", "limit": 200}),
    ]
    rows = [r for r in live.calls[0][2].get("data", []) if isinstance(r, dict)]
    assert rows, "congress recent-trades returned no rows for NVDA"
    assert {
        "name", "politician_id", "ticker", "transaction_date", "filed_at_date", "txn_type",
        "amounts", "member_type", "issuer",
    } <= rows[0].keys()
    assert "id" not in rows[0]  # probed: no id field, hence the composite dedupe key
    assert all(isinstance(r["member_type"], str) for r in rows)  # probed: str, not the spec's bool
    assert all(isinstance(r["amounts"], str) for r in rows)

    assert result.status == "ok"
    records = load_delayed_records(engine, "NVDA")
    assert records, "no NVDA congress buy/sell filed inside the lookback window"
    assert result.inserted == len(records)
    for record in records:
        assert record.family == "congress"
        assert record.side in {"buy", "sell"}
        assert record.delay_days is not None
        assert record.delay_days >= 0
        assert record.transaction_date is not None
    today = now.astimezone(_ET).date()
    evidence = build_delayed_evidence("NVDA", records, (), today=today, settings=_settings())
    assert evidence.items
    assert {i.date_label for i in evidence.items} == {"bildirim tarihi"}
