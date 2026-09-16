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

import json
import os
from datetime import UTC, date, datetime, timedelta
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
    FTDS_PATH,
    INSIDER_TRANSACTIONS_PATH,
    SHORT_INTEREST_PATH,
    build_delayed_evidence,
    load_delayed_records,
    refresh_congress,
    refresh_ftds,
    refresh_insider,
    refresh_short_interest,
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


@pytest.mark.asyncio
async def test_live_insider_transactions(live: _RecordingClient, engine: Engine) -> None:
    now = datetime.now(UTC)
    settings = _settings()
    try:
        result = await refresh_insider(live, engine, "NVDA", now=now, settings=settings)  # type: ignore[arg-type]
    finally:
        await live._inner.aclose()

    today = now.astimezone(_ET).date()
    start = today - timedelta(days=settings.insider_lookback_days)
    [(path, params, body)] = live.calls
    assert path == INSIDER_TRANSACTIONS_PATH
    assert params == {"ticker_symbol": "NVDA", "form_types[]": ["4", "4/A"], "start_date": start.isoformat()}
    assert isinstance(body.get("has_more"), bool)
    rows = [r for r in body.get("data", []) if isinstance(r, dict)]
    assert rows, "insider transactions returned no Form 4 rows for NVDA in the window"
    assert {
        "ticker", "transaction_date", "filing_date", "formtype", "transaction_code", "amount",
        "price", "ids", "owner_name", "is_10b5_1",
    } <= rows[0].keys()
    assert {r["formtype"] for r in rows} <= {"4", "4/A"}  # form_types[] is honoured (probed)
    assert all(date.fromisoformat(r["transaction_date"]) >= start for r in rows)  # start_date filters trades
    assert all(isinstance(r["amount"], int) and isinstance(r["price"], str) for r in rows)

    assert result.status == "ok"
    records = load_delayed_records(engine, "NVDA")
    trades = [r for r in rows if r["transaction_code"] in {"P", "S"}]
    assert bool(records) == bool(trades)
    assert len(records) <= len(trades)
    for record in records:
        assert record.family == "insider"
        assert record.form in {"4", "4/A"}
        assert record.delay_days is not None
        assert record.delay_days >= 0
        assert record.flag_10b5_1 in {True, False}
        assert record.size_low is None or record.size_low > 0


@pytest.mark.asyncio
async def test_live_short_interest_float_v2(live: _RecordingClient, engine: Engine) -> None:
    now = datetime.now(UTC)
    settings = _settings()
    try:
        result = await refresh_short_interest(live, engine, "TSLA", now=now, settings=settings)  # type: ignore[arg-type]
    finally:
        await live._inner.aclose()

    [(path, params, body)] = live.calls
    assert (path, params) == (SHORT_INTEREST_PATH.format(ticker="TSLA"), None)
    rows = [r for r in body.get("data", []) if isinstance(r, dict)]
    assert rows, "interest-float/v2 returned no rows for TSLA"
    assert {"symbol", "market_date", "short_interest", "total_float", "si_float", "days_to_cover"} <= rows[0].keys()
    dates = [r["market_date"] for r in rows]
    assert dates == sorted(dates, reverse=True)  # probed: newest first
    assert isinstance(rows[0]["si_float"], str)  # probed: a string
    assert 0 <= float(rows[0]["si_float"]) < 1  # probed: a fraction, not a percent (TSLA ~2%)

    assert result.status == "ok"
    records = load_delayed_records(engine, "TSLA")
    assert records, "no TSLA short-interest as-of date inside the window"
    for record in records:
        assert record.family == "short_interest"
        assert record.delay_days is None
        assert record.transaction_date is None
        assert "short_shares_available" not in json.loads(record.payload_json)
    today = now.astimezone(_ET).date()
    [newest, *_] = build_delayed_evidence("TSLA", records, (), today=today, settings=settings).items
    assert newest.date_label == "itibarıyla tarihi"
    assert newest.delay_days == (today - newest.filed_or_asof_date).days


@pytest.mark.asyncio
async def test_live_ftds(live: _RecordingClient, engine: Engine) -> None:
    now = datetime.now(UTC)
    settings = _settings()
    try:
        result = await refresh_ftds(live, engine, "NVDA", now=now, settings=settings)  # type: ignore[arg-type]
    finally:
        await live._inner.aclose()

    [(path, params, body)] = live.calls
    assert (path, params) == (FTDS_PATH.format(ticker="NVDA"), None)
    rows = [r for r in body.get("data", []) if isinstance(r, dict)]
    assert rows, "ftds returned no rows for NVDA"
    assert {"date", "quantity", "price"} <= rows[0].keys()
    assert all(isinstance(r["quantity"], int) and isinstance(r["price"], str) for r in rows[:50])
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates, reverse=True)  # probed: newest first

    assert result.status == "ok"
    today = now.astimezone(_ET).date()
    start = today - timedelta(days=settings.ftd_lookback_days)
    in_window = [r for r in rows if start <= date.fromisoformat(r["date"]) <= today and r["quantity"] > 0]
    records = load_delayed_records(engine, "NVDA")
    assert len(records) == len(in_window)
    for record in records:
        assert record.family == "ftd"
        assert record.size_low is None or record.size_low > 0
