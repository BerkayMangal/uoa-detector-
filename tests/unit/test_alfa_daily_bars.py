"""Phase 5.3.1: regular-session bars, written by the job that already fetched them.

Contract: ``docs/phase-5.3-spot-frame-acceptance.md`` §3.1 (the bar table costs
no additional UW request), §6.7 (the two payload readers can never drift), §6.8
(``alfa_daily_bar`` is append-only) and §6.10 (the daily job's request count is
unchanged by this phase).

Every test below states the rule it pins. The three that claim to pin a guard
were each verified by mutation — the guard was removed and the test failed —
and the mutations are named in the PR body (P39/P40).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from webapp.board.daily_close import (
    AlfaDailyBar,
    ensure_daily_bar_tables,
    job_tickers,
    run_daily_close_job,
)
from webapp.board.db import make_engine, session_factory
from webapp.ohlc import regular_session_bars, regular_session_closes

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

_NOW = datetime(2026, 9, 16, 21, 5, tzinfo=UTC)


def _row(day: str, session: str, close: str, **over: str | None) -> dict[str, object]:
    """One ohlc/1d row in the vendor's shape: values are strings, newest first."""
    row: dict[str, object] = {
        "date": day, "market_time": session, "open": close, "high": close,
        "low": close, "close": close, "volume": "250000",
    }
    row.update(over)
    return row


# Newest first, several sessions per date, exactly as the live payload arrives.
_PAYLOAD: dict[str, object] = {
    "data": [
        _row("2026-09-16", "po", "765.00"),
        _row("2026-09-16", "r", "764.10", open="760.00", high="766.20", low="759.10"),
        _row("2026-09-16", "pr", "759.01"),
        _row("2026-09-15", "r", "762.04", open="757.00", high="763.30", low="756.40"),
        _row("2026-09-14", "r", "757.30", open="750.10", high="758.00", low="749.55"),
        {"date": "not-a-date", "market_time": "r", "close": "1.00"},
        {"market_time": "r", "close": "1.00"},
        "not even a row",
    ],
}


def _path(ticker: str) -> str:
    return f"/api/stock/{ticker}/ohlc/1d"


class _CountingClient:
    """Serves the same payload for any path and counts every request made."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, object]:
        del params, method
        self.calls.append(path)
        return _PAYLOAD


class _FixedClient:
    """Serves one caller-supplied payload, so a run can be given an open session."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, object]:
        del path, params, method
        return self._payload


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'bars.db'}")


def _stored(engine: Engine, ticker: str) -> list[AlfaDailyBar]:
    with session_factory(engine)() as session:
        return list(
            session.execute(
                select(AlfaDailyBar)
                .where(AlfaDailyBar.ticker == ticker)
                .order_by(AlfaDailyBar.day),
            ).scalars(),
        )


def test_the_two_readers_never_drift() -> None:
    """§6.7: the same payload, the same days in the same order, the same closes.

    Both readers are built on one private filter, so a change to the session
    rule or the ordering cannot reach one and miss the other.
    """
    closes = regular_session_closes(_PAYLOAD["data"])
    bars = regular_session_bars(_PAYLOAD["data"])

    assert [c.day for c in closes] == [b.day for b in bars]
    assert [c.close for c in closes] == [b.close for b in bars]
    # Oldest first, and the undated / malformed rows are dropped by both.
    assert [b.day for b in bars] == [
        date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16),
    ]


def test_a_row_that_will_not_parse_is_stored_with_nulls_not_skipped() -> None:
    """§3.1: never skipped, never guessed — the missing value is null.

    The ATR window breaks on a gap; inventing a high would put a number under a
    stop the owner would size against.
    """
    payload = {"data": [_row("2026-09-16", "r", "764.10", high="n/a", low=None)]}
    bar = regular_session_bars(payload["data"])[0]

    assert bar.day == date(2026, 9, 16)
    assert bar.close == 764.10
    assert bar.open == 764.10
    assert bar.high is None
    assert bar.low is None


async def test_the_job_stores_a_bar_per_regular_session_for_every_ticker(
    engine: Engine,
) -> None:
    """§3.1: the daily-close job writes the bar table from the payload it holds."""
    client = _CountingClient()
    await run_daily_close_job(client, engine, ["NVDA"], now=_NOW)

    for ticker in job_tickers(["NVDA"]):
        bars = _stored(engine, ticker)
        assert [b.day for b in bars] == [
            date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16),
        ], ticker
        newest = bars[-1]
        assert (newest.open, newest.high, newest.low, newest.close) == (
            760.00, 766.20, 759.10, 764.10,
        )
        assert newest.fetched_at is not None


async def test_an_open_session_is_never_frozen_into_the_append_only_bar_table(
    engine: Engine,
) -> None:
    """A bar written mid-session can never be corrected, because the table is append-only.

    Found by audit. The closes path has always filtered on ``is_final_close``; the
    bars path did not, so a run before 16:00 ET stored the in-progress OHLC and the
    post-close run skipped that ``(ticker, day)`` as already present. The frozen
    bar's true range was a fraction of the settled one, which understates the ATR,
    understates the stop distance and OVERSTATES the share count by the same factor.
    """
    intraday = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)  # 14:00 ET: the session is open
    in_progress = {
        "data": [
            _row("2026-09-16", "r", "760.50", open="760.00", high="761.00", low="759.50"),
            _row("2026-09-15", "r", "762.04", open="757.00", high="763.30", low="756.40"),
            _row("2026-09-14", "r", "757.30", open="750.10", high="758.00", low="749.55"),
        ],
    }
    await run_daily_close_job(_FixedClient(in_progress), engine, ["NVDA"], now=intraday)

    stored = [b.day for b in _stored(engine, "NVDA")]
    assert date(2026, 9, 16) not in stored, "an unfinished session must not be stored"
    assert stored == [date(2026, 9, 14), date(2026, 9, 15)]

    # After the close the settled bar lands, with the day's real high and low.
    await run_daily_close_job(_CountingClient(), engine, ["NVDA"], now=_NOW)
    newest = _stored(engine, "NVDA")[-1]
    assert newest.day == date(2026, 9, 16)
    assert (newest.open, newest.high, newest.low, newest.close) == (
        760.00, 766.20, 759.10, 764.10,
    )


async def test_the_bar_write_costs_no_additional_request(engine: Engine) -> None:
    """§6.10: this phase must not change the daily job's UW request count.

    One request per board ticker plus SPY, exactly as before — the bars are
    parsed from that same response. If a future edit fetches the payload a
    second time for the bars, this fails.
    """
    client = _CountingClient()
    tickers = ["NVDA", "SMCI"]
    await run_daily_close_job(client, engine, tickers, now=_NOW)

    expected = [_path(t) for t in job_tickers(tickers)]
    assert client.calls == expected
    assert len(client.calls) == len(job_tickers(tickers)) == 3


async def test_bars_are_append_only_across_runs(engine: Engine) -> None:
    """§6.8: a second run adds nothing and rewrites nothing.

    The rows are evidence about a session that is over; re-running the job must
    never restate them.
    """
    client = _CountingClient()
    await run_daily_close_job(client, engine, ["NVDA"], now=_NOW)
    first = [(b.day, b.open, b.high, b.low, b.close) for b in _stored(engine, "NVDA")]

    later = datetime(2026, 9, 17, 21, 5, tzinfo=UTC)
    await run_daily_close_job(client, engine, ["NVDA"], now=later)
    second = [(b.day, b.open, b.high, b.low, b.close) for b in _stored(engine, "NVDA")]

    assert second == first
    # And the second run did not restamp them: every row still carries the
    # moment it was first stored. (The previous version of this line compared a
    # set with itself and could not fail — the exact defect P39/P40 record.)
    stamps = {b.fetched_at.replace(tzinfo=UTC) for b in _stored(engine, "NVDA")}
    assert stamps == {_NOW}, "the second run restamped rows it should not have touched"


def test_ensure_is_idempotent_and_the_key_is_ticker_day(engine: Engine) -> None:
    """The table is created the same way every other alfa_ table is: additively."""
    from sqlalchemy import inspect

    ensure_daily_bar_tables(engine)
    ensure_daily_bar_tables(engine)
    assert "alfa_daily_bar" in inspect(engine).get_table_names()
    pk = inspect(engine).get_pk_constraint("alfa_daily_bar")["constrained_columns"]
    assert pk == ["ticker", "day"]


async def test_a_day_whose_regular_rows_disagree_is_dropped_not_guessed(
    engine: Engine,
) -> None:
    """§3.1: never guessed. Two different regular rows for one date resolve to no bar.

    The payload does carry this shape — the close reader already refuses to
    choose between them and counts the day ambiguous. Picking one would put a
    high the vendor never settled under an ATR stop the owner sizes against.
    An identical duplicate is not a disagreement and is kept.
    """
    payload = {"data": [
        # Two rows for the 15th that disagree on the high.
        _row("2026-09-15", "r", "762.04", high="763.30"),
        _row("2026-09-15", "r", "762.04", high="999.00"),
        # Two rows for the 14th that are byte-identical.
        _row("2026-09-14", "r", "757.30"),
        _row("2026-09-14", "r", "757.30"),
    ]}

    class _Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def request_json(
            self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
        ) -> dict[str, object]:
            del params, method
            self.calls.append(path)
            return payload

    await run_daily_close_job(_Client(), engine, [], now=_NOW)

    days = [b.day for b in _stored(engine, "SPY")]
    assert days == [date(2026, 9, 14)], "the conflicting day must not be stored"
    kept = _stored(engine, "SPY")[0]
    assert (kept.open, kept.high, kept.low, kept.close) == (757.30, 757.30, 757.30, 757.30)
