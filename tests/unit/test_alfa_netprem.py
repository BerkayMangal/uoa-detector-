"""Phase 5.2.A3: the net-premium tape (``webapp/board/netprem.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.2 (``alfa_net_prem``,
rebuildable, upsert), §4.4, §5 A3 ("Akış") and §5 B3 (since-print sums).

Fixture rows are trimmed from the live NVDA ``net-prem-ticks`` response captured
2026-09-15 (``alfa_probe/nvda_net_prem_ticks.json``).

Pins:
  - premiums arrive as strings and are per-minute increments; minutes sort
    oldest first; a repeated minute keeps its last row; without ``date`` the
    trading day is the New York date;
  - any row without a parseable minute or premium rejects the whole payload;
  - fetch: the probed path, no params; not-found is no data; rate limit,
    transient, open breaker and an unexpected payload are degraded; daily-limit
    and auth errors propagate;
  - upsert: new minutes inserted, a revised minute updated, an unchanged
    re-fetch writes nothing but re-stamps the newest minute;
  - whole-day and since-print summaries, and the table prefix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session
from webapp.board.db import TABLE_PREFIX, make_engine
from webapp.board.netprem import (
    NET_PREM_TICKS_PATH,
    AlfaNetPrem,
    ensure_netprem_tables,
    fetch_net_prem_ticks,
    parse_net_prem_ticks,
    read_net_premium_since,
    read_tape_summaries,
    upsert_tape,
)

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

if TYPE_CHECKING:
    from pathlib import Path

_ROWS: list[dict[str, Any]] = [
    {
        "date": "2026-09-15", "call_volume": 26560, "put_volume": 9547,
        "tape_time": "2026-09-15T13:30:00.000000Z", "net_call_volume": -987,
        "net_call_premium": "2214509.00", "net_put_volume": -1038, "net_put_premium": "-515070.00",
        "net_delta": "48197.7987974182169589200",
    },
    {
        "date": "2026-09-15", "call_volume": 14124, "put_volume": 9885,
        "tape_time": "2026-09-15T13:31:00.000000Z", "net_call_volume": -3389,
        "net_call_premium": "-862179.00", "net_put_volume": -409, "net_put_premium": "-300077.00",
        "net_delta": "-47013.290430361419730600",
    },
    {
        "date": "2026-09-15", "call_volume": 15430, "put_volume": 8079,
        "tape_time": "2026-09-15T13:32:00.000000Z", "net_call_volume": 3421,
        "net_call_premium": "638346.00", "net_put_volume": -6248, "net_put_premium": "-1206342.00",
        "net_delta": "172878.594859157335195500",
    },
]
_DAY = date(2026, 9, 15)
_FETCHED = datetime(2026, 9, 15, 13, 33, 5, tzinfo=UTC)


def _minute(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 15, hour, minute, tzinfo=UTC)


class _FakeClient:
    def __init__(self, *, payload: object = None, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._payload = payload
        self._error = error

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> Any:
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._payload


@pytest.fixture
def engine_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'tape.db'}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_reads_string_premiums_oldest_first() -> None:
    minutes = parse_net_prem_ticks({"data": list(reversed(_ROWS))})
    assert [m.tape_time for m in minutes] == [_minute(13, 30), _minute(13, 31), _minute(13, 32)]
    first = minutes[0]
    assert (first.trade_date, first.net_call_premium, first.net_put_premium) == (_DAY, 2214509.0, -515070.0)
    assert (first.net_call_volume, first.net_put_volume, first.call_volume, first.put_volume) == (
        -987, -1038, 26560, 9547,
    )
    assert first.net_delta == pytest.approx(48197.7987974182)
    assert first.tape_time.tzinfo is not None


def test_parse_keeps_the_last_row_of_a_repeated_minute() -> None:
    revised = {**_ROWS[2], "net_call_premium": "700000.00"}
    minutes = parse_net_prem_ticks({"data": [*_ROWS, revised]})
    assert len(minutes) == 3
    assert minutes[-1].net_call_premium == 700000.0


def test_parse_without_a_date_uses_the_new_york_trading_day() -> None:
    row = {k: v for k, v in _ROWS[0].items() if k != "date"}
    row["tape_time"] = "2026-09-16T00:30:00Z"
    (minute,) = parse_net_prem_ticks({"data": [row]})
    assert minute.trade_date == date(2026, 9, 15)


def test_parse_tolerates_missing_optional_fields() -> None:
    row = {"tape_time": "2026-09-15T13:30:00Z", "net_call_premium": "1.5", "net_put_premium": 2, "date": "2026-09-15"}
    (minute,) = parse_net_prem_ticks({"data": [row]})
    assert (minute.net_call_premium, minute.net_put_premium) == (1.5, 2.0)
    assert (minute.net_call_volume, minute.call_volume, minute.net_delta) == (None, None, None)


def test_empty_tape_parses_to_nothing() -> None:
    assert parse_net_prem_ticks({"data": []}) == ()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"data": None},
        {"data": {"tape_time": "2026-09-15T13:30:00Z"}},
        {"data": ["not a row"]},
        {"data": [{**_ROWS[0], "net_call_premium": None}]},
        {"data": [_ROWS[0], {**_ROWS[1], "net_put_premium": "n/a"}]},
        {"data": [{**_ROWS[0], "tape_time": "yesterday"}]},
        {"data": [{**_ROWS[0], "net_call_premium": True}]},
    ],
)
def test_parse_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(ValueError, match=r"expected|net-prem row"):
        parse_net_prem_ticks(payload)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def test_fetch_calls_the_probed_path_without_params() -> None:
    client = _FakeClient(payload={"data": _ROWS})
    fetch = await fetch_net_prem_ticks(client, " nvda ")
    assert client.calls == [(NET_PREM_TICKS_PATH.format(ticker="NVDA"), None)]
    assert client.calls[0][0] == "/api/stock/NVDA/net-prem-ticks"
    assert (fetch.ticker, len(fetch.minutes), fetch.degraded) == ("NVDA", 3, False)


async def test_fetch_not_found_is_no_data() -> None:
    client = _FakeClient(error=UnusualWhalesNotFoundError("HTTP 404", status_code=404))
    fetch = await fetch_net_prem_ticks(client, "NVDA")
    assert (fetch.minutes, fetch.degraded) == ((), False)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesRateLimitError("rate-limited (HTTP 429): slow down"), id="rate-limit"),
        pytest.param(UnusualWhalesTransientError("returned HTTP 503"), id="transient"),
        pytest.param(CircuitBreakerOpenError("circuit breaker is open"), id="breaker-open"),
    ],
)
async def test_fetch_degrades_on_rate_limit_transient_and_open_breaker(error: Exception) -> None:
    fetch = await fetch_net_prem_ticks(_FakeClient(error=error), "NVDA")
    assert (fetch.minutes, fetch.degraded) == ((), True)


async def test_fetch_degrades_on_an_unexpected_payload() -> None:
    fetch = await fetch_net_prem_ticks(_FakeClient(payload={"data": [{"tape_time": None}]}), "NVDA")
    assert (fetch.minutes, fetch.degraded) == ((), True)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), id="daily-limit"),
        pytest.param(UnusualWhalesAuthError("returned HTTP 401"), id="auth"),
    ],
)
async def test_fetch_propagates_daily_limit_and_auth_errors(error: Exception) -> None:
    with pytest.raises(type(error)):
        await fetch_net_prem_ticks(_FakeClient(error=error), "NVDA")


# ---------------------------------------------------------------------------
# Persistence and summaries
# ---------------------------------------------------------------------------


def _row_count(engine: Any) -> int:
    with Session(engine) as session:
        return int(session.execute(select(func.count()).select_from(AlfaNetPrem)).scalar_one())


def test_upsert_inserts_updates_revisions_and_restamps_the_newest_minute(engine_url: str) -> None:
    engine = make_engine(engine_url)
    try:
        ensure_netprem_tables(engine)
        minutes = parse_net_prem_ticks({"data": _ROWS})
        assert upsert_tape(engine, "nvda", minutes, fetched_at=_FETCHED) == 3

        later = _FETCHED + timedelta(minutes=5)
        assert upsert_tape(engine, "NVDA", minutes, fetched_at=later) == 0
        summary = read_tape_summaries(engine, [("NVDA", _DAY)])[("NVDA", _DAY)]
        assert summary.fetched_at == later

        revised = parse_net_prem_ticks(
            {
                "data": [
                    *_ROWS[:2],
                    {**_ROWS[2], "net_call_premium": "700000.00"},  # the partial minute, revised
                    {**_ROWS[2], "tape_time": "2026-09-15T13:33:00.000000Z"},  # a new minute
                ],
            },
        )
        assert upsert_tape(engine, "NVDA", revised, fetched_at=later) == 2
        assert upsert_tape(engine, "NVDA", (), fetched_at=later) == 0
        assert _row_count(engine) == 4
        summary = read_tape_summaries(engine, [("NVDA", _DAY)])[("NVDA", _DAY)]
    finally:
        engine.dispose()
    assert summary.net_call_premium == 2214509.0 - 862179.0 + 700000.0 + 638346.0
    assert summary.net_put_premium == -515070.0 - 300077.0 - 1206342.0 - 1206342.0
    assert (summary.minutes, summary.last_tape_time) == (4, _minute(13, 33))


def test_whole_day_summary(engine_url: str) -> None:
    engine = make_engine(engine_url)
    try:
        ensure_netprem_tables(engine)
        upsert_tape(engine, "NVDA", parse_net_prem_ticks({"data": _ROWS}), fetched_at=_FETCHED)
        summaries = read_tape_summaries(
            engine, [("nvda", _DAY), ("NVDA", date(2026, 9, 14)), ("SPY", _DAY)],
        )
        assert read_tape_summaries(engine, []) == {}
    finally:
        engine.dispose()
    assert set(summaries) == {("NVDA", _DAY)}
    summary = summaries[("NVDA", _DAY)]
    assert (summary.net_call_premium, summary.net_put_premium) == (1_990_676.0, -2_021_489.0)
    assert summary.bullish_net_premium == 4_012_165.0
    assert summary.net_premium_for("up") == 4_012_165.0
    assert summary.net_premium_for("down") == -4_012_165.0
    assert (summary.minutes, summary.first_tape_time, summary.last_tape_time) == (
        3, _minute(13, 30), _minute(13, 32),
    )
    assert summary.fetched_at == _FETCHED


def test_since_print_summary_starts_at_the_minute_of_the_print(engine_url: str) -> None:
    engine = make_engine(engine_url)
    try:
        ensure_netprem_tables(engine)
        upsert_tape(engine, "NVDA", parse_net_prem_ticks({"data": _ROWS}), fetched_at=_FETCHED)
        since = read_net_premium_since(engine, "nvda", _DAY, datetime(2026, 9, 15, 13, 31, 42, tzinfo=UTC))
        after = read_net_premium_since(engine, "NVDA", _DAY, _minute(13, 45))
    finally:
        engine.dispose()
    assert since is not None
    assert (since.net_call_premium, since.net_put_premium) == (-223_833.0, -1_506_419.0)
    assert since.bullish_net_premium == 1_282_586.0
    assert (since.minutes, since.first_tape_time) == (2, _minute(13, 31))
    assert after is None


def test_table_is_prefixed_and_creation_is_idempotent(engine_url: str) -> None:
    engine = make_engine(engine_url)
    try:
        ensure_netprem_tables(engine)
        ensure_netprem_tables(engine)
        assert "alfa_net_prem" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    assert AlfaNetPrem.__tablename__.startswith(TABLE_PREFIX)
