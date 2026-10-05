"""Phase 5.25.10 hotfix: the live run id rolls over at UTC midnight.

``_ensure_run`` recomputes ``live-<UTC date>`` per loop iteration, but
``pipeline.run()`` drains an endless stream and never returns on its own, so the
worker called it once per process: every day after a restart was written into
the restart day's run id. Live on 2026-10-05 the newest run held four days, so
the board read four days of flow as "today" (every ticker "kararsız", every
pace multiple 6-68x).

``_close_at_utc_rollover`` closes the flow source when the UTC date leaves the
run's day, which ends the stream, ends ``pipeline.run()`` and lets the loop open
the next day's run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest
from webapp import worker

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Source:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _clock(stamps: list[datetime]) -> Any:
    it: Iterator[datetime] = iter(stamps)
    last = stamps[-1]

    def _now() -> datetime:
        return next(it, last)

    return _now


def test_rollover_closes_the_source_when_the_utc_date_changes() -> None:
    source = _Source()
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    day = date(2026, 10, 5)
    clock = _clock([
        datetime(2026, 10, 5, 23, 58, tzinfo=UTC),
        datetime(2026, 10, 5, 23, 59, tzinfo=UTC),
        datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
    ])
    asyncio.run(worker._close_at_utc_rollover(source, day, now=clock, sleep=_sleep))  # type: ignore[arg-type]
    assert source.closed is True
    assert slept == [worker._ROLLOVER_CHECK_S, worker._ROLLOVER_CHECK_S]


def test_rollover_leaves_the_source_open_during_its_own_day() -> None:
    source = _Source()
    calls = 0

    async def _sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise asyncio.CancelledError

    clock = _clock([datetime(2026, 10, 5, 14, 0, tzinfo=UTC)])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._close_at_utc_rollover(source, date(2026, 10, 5), now=clock, sleep=_sleep))
    assert source.closed is False
