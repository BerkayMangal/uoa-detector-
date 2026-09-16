"""Phase 5.2.D0: ``alfa_daily_close``, the daily close job and the close helpers.

Hermetic. A fake client serves trimmed ohlc/1d payloads shaped like the live SPY
response (newest first, "pr"/"r"/"po" rows per date, values as strings; the
2026-09-14 regular close 762.04 is the probed value). A tmp sqlite file holds
the table. No network, no env.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from webapp.board import daily_close as dc
from webapp.board.daily_close import (
    AlfaDailyClose,
    ClosePoint,
    close_on_or_before,
    ensure_daily_close_tables,
    is_final_close,
    job_tickers,
    load_closes,
    pct_move_between,
    run_daily_close_job,
)
from webapp.board.db import TABLE_PREFIX, make_engine

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

# 2026-09-15 is a Tuesday in EDT: the session ends 16:00 ET = 20:00 UTC.
_DURING_SESSION = datetime(2026, 9, 15, 17, 0, tzinfo=UTC)
_AFTER_SESSION = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)


def _bar(day: str, session: str, close: str) -> dict[str, object]:
    return {
        "close": close, "date": day, "high": close, "low": close, "market_time": session,
        "open": close, "total_volume": "1000000", "volume": "250000",
    }


def _spy_payload(close_0914: str = "762.04") -> dict[str, Any]:
    return {"data": [
        _bar("2026-09-15", "pr", "759.01"), _bar("2026-09-15", "r", "764.10"),
        _bar("2026-09-14", "po", "761.50"), _bar("2026-09-14", "r", close_0914),
        _bar("2026-09-14", "pr", "758.00"),
        _bar("2026-09-11", "r", "757.30"), _bar("2026-09-11", "po", "757.00"),
        _bar("2026-09-10", "r", "760.12"),
    ]}


def _path(ticker: str) -> str:
    return f"/api/stock/{ticker}/ohlc/1d"


class _FakeClient:
    """Records (path, params); serves a scripted body or raises a scripted error per path."""

    def __init__(self, responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        result = self.responses.get(path, {"data": []})
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'board.db'}")


def _stored(engine: Engine, ticker: str) -> dict[date, float]:
    return {p.day: p.close for p in load_closes(engine, ticker)}


def test_ensure_creates_the_prefixed_table_idempotently(engine: Engine) -> None:
    ensure_daily_close_tables(engine)
    ensure_daily_close_tables(engine)
    assert AlfaDailyClose.__tablename__.startswith(TABLE_PREFIX)
    assert "alfa_daily_close" in inspect(engine).get_table_names()
    pk = inspect(engine).get_pk_constraint("alfa_daily_close")["constrained_columns"]
    assert pk == ["ticker", "day"]


def test_job_tickers_uppercase_dedupe_and_append_spy() -> None:
    assert job_tickers([" nvda", "NVDA", "smci"]) == ("NVDA", "SMCI", "SPY")
    assert job_tickers(["spy", "AAPL"]) == ("SPY", "AAPL")
    assert job_tickers([]) == ("SPY",)


async def test_job_stores_regular_session_closes_for_tickers_plus_spy(engine: Engine) -> None:
    client = _FakeClient({_path("NVDA"): _spy_payload("212.19"), _path("SPY"): _spy_payload()})
    result = await run_daily_close_job(client, engine, ["nvda"], now=_AFTER_SESSION)  # type: ignore[arg-type]

    assert [c[0] for c in client.calls] == [_path("NVDA"), _path("SPY")]
    assert [r.status for r in result.tickers] == ["ok", "ok"]
    assert _stored(engine, "SPY") == {
        date(2026, 9, 10): 760.12, date(2026, 9, 11): 757.30,
        date(2026, 9, 14): 762.04, date(2026, 9, 15): 764.10,
    }
    assert _stored(engine, "NVDA")[date(2026, 9, 14)] == 212.19
    spy = result.tickers[1]
    assert (spy.inserted, spy.already_stored, spy.not_final) == (4, 0, 0)


async def test_intraday_close_is_not_frozen_during_the_session(engine: Engine) -> None:
    client = _FakeClient({_path("SPY"): _spy_payload()})
    during = await run_daily_close_job(client, engine, [], now=_DURING_SESSION)  # type: ignore[arg-type]
    assert date(2026, 9, 15) not in _stored(engine, "SPY")
    assert (during.tickers[0].inserted, during.tickers[0].not_final) == (3, 1)

    after = await run_daily_close_job(client, engine, [], now=_AFTER_SESSION)  # type: ignore[arg-type]
    assert _stored(engine, "SPY")[date(2026, 9, 15)] == 764.10
    assert (after.tickers[0].inserted, after.tickers[0].already_stored) == (1, 3)


async def test_rerun_is_idempotent_and_never_rewrites_a_stored_close(engine: Engine) -> None:
    await run_daily_close_job(_FakeClient({_path("SPY"): _spy_payload()}), engine, [], now=_AFTER_SESSION)  # type: ignore[arg-type]
    revised = _FakeClient({_path("SPY"): _spy_payload(close_0914="999.00")})
    again = await run_daily_close_job(revised, engine, [], now=_AFTER_SESSION)  # type: ignore[arg-type]

    assert (again.tickers[0].inserted, again.tickers[0].already_stored) == (0, 4)
    assert _stored(engine, "SPY")[date(2026, 9, 14)] == 762.04


async def test_unparseable_zero_and_conflicting_closes_are_skipped(engine: Engine) -> None:
    payload = {"data": [
        _bar("2026-09-14", "r", "n/a"),
        _bar("2026-09-11", "r", "0"),
        _bar("2026-09-10", "r", "760.12"), _bar("2026-09-10", "r", "761.00"),
        _bar("2026-09-09", "r", "751.88"), _bar("2026-09-09", "r", "751.88"),
        "not-a-row",
    ]}
    result = await run_daily_close_job(_FakeClient({_path("SPY"): payload}), engine, [], now=_AFTER_SESSION)  # type: ignore[arg-type]
    assert _stored(engine, "SPY") == {date(2026, 9, 9): 751.88}
    assert result.tickers[0].ambiguous_days == 1


async def test_not_found_and_empty_payload_read_as_no_data(engine: Engine) -> None:
    client = _FakeClient({
        _path("ZZZZ"): UnusualWhalesNotFoundError("422", status_code=422),
        _path("NVDA"): {"data": []},
        _path("SPY"): _spy_payload(),
    })
    result = await run_daily_close_job(client, engine, ["ZZZZ", "NVDA"], now=_AFTER_SESSION)  # type: ignore[arg-type]
    assert [(r.ticker, r.status) for r in result.tickers] == [
        ("ZZZZ", "no_data"), ("NVDA", "no_data"), ("SPY", "ok"),
    ]
    assert result.degraded == ()


@pytest.mark.parametrize(
    "error",
    [
        UnusualWhalesRateLimitError("429"),
        UnusualWhalesTransientError("503"),
        CircuitBreakerOpenError("open"),
    ],
)
async def test_degradable_errors_mark_the_ticker_and_the_job_continues(
    engine: Engine, error: Exception,
) -> None:
    client = _FakeClient({_path("NVDA"): error, _path("SPY"): _spy_payload()})
    result = await run_daily_close_job(client, engine, ["NVDA"], now=_AFTER_SESSION)  # type: ignore[arg-type]
    assert result.degraded == ("NVDA",)
    assert len(_stored(engine, "SPY")) == 4


@pytest.mark.parametrize(
    "error",
    [UnusualWhalesDailyLimitError("daily_request_limit_hit"), UnusualWhalesAuthError("401")],
)
async def test_daily_limit_and_auth_errors_propagate(engine: Engine, error: Exception) -> None:
    client = _FakeClient({_path("NVDA"): error, _path("SPY"): _spy_payload()})
    with pytest.raises(type(error)):
        await run_daily_close_job(client, engine, ["NVDA"], now=_AFTER_SESSION)  # type: ignore[arg-type]
    assert [c[0] for c in client.calls] == [_path("NVDA")]


async def test_naive_now_is_rejected(engine: Engine) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await run_daily_close_job(_FakeClient({}), engine, [], now=datetime(2026, 9, 15, 21, 5))  # type: ignore[arg-type]


def test_is_final_close_uses_the_et_session_end_in_both_dst_states() -> None:
    day = date(2026, 9, 15)  # EDT: 16:00 ET = 20:00 UTC
    assert not is_final_close(day, datetime(2026, 9, 15, 19, 59, tzinfo=UTC))
    assert is_final_close(day, datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
    winter = date(2026, 1, 15)  # EST: 16:00 ET = 21:00 UTC
    assert not is_final_close(winter, datetime(2026, 1, 15, 20, 30, tzinfo=UTC))
    assert is_final_close(winter, datetime(2026, 1, 15, 21, 0, tzinfo=UTC))
    assert is_final_close(date(2026, 9, 14), _DURING_SESSION)
    assert not is_final_close(date(2026, 9, 16), _AFTER_SESSION)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_final_close(day, datetime(2026, 9, 15, 21, 0))


_CLOSES = (
    ClosePoint(date(2026, 9, 14), 110.0),  # unsorted on purpose
    ClosePoint(date(2026, 9, 10), 100.0),
    ClosePoint(date(2026, 9, 11), 105.0),
)


def test_close_on_or_before() -> None:
    assert close_on_or_before(_CLOSES, date(2026, 9, 11)) == ClosePoint(date(2026, 9, 11), 105.0)
    # Saturday and Sunday resolve to Friday's close.
    assert close_on_or_before(_CLOSES, date(2026, 9, 13)) == ClosePoint(date(2026, 9, 11), 105.0)
    assert close_on_or_before(_CLOSES, date(2026, 9, 30)) == ClosePoint(date(2026, 9, 14), 110.0)
    assert close_on_or_before(_CLOSES, date(2026, 9, 9)) is None
    assert close_on_or_before((), date(2026, 9, 9)) is None


def test_pct_move_between() -> None:
    move = pct_move_between(_CLOSES, date(2026, 9, 10), date(2026, 9, 14))
    assert move is not None
    assert move.pct == pytest.approx(10.0)
    assert (move.start.day, move.end.day) == (date(2026, 9, 10), date(2026, 9, 14))
    weekend = pct_move_between(_CLOSES, date(2026, 9, 12), date(2026, 9, 13))
    assert weekend is not None
    assert weekend.pct == 0.0
    assert weekend.start == weekend.end
    assert pct_move_between(_CLOSES, date(2026, 9, 14), date(2026, 9, 10)) is None
    assert pct_move_between(_CLOSES, date(2026, 9, 1), date(2026, 9, 14)) is None
    assert pct_move_between((ClosePoint(date(2026, 9, 10), 0.0), *_CLOSES[:1]),
                            date(2026, 9, 10), date(2026, 9, 14)) is None


def test_module_exposes_no_rewrite_path() -> None:
    """Append-only (contract §4.2): no update, delete, reset or drop entry point."""
    names = [n.lower() for n in dir(dc) if not n.startswith("__")]
    for word in ("update", "delete", "reset", "drop", "upsert"):
        assert not [n for n in names if word in n], word
