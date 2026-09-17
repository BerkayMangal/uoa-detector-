"""Phase 5.2.PERF8 (decision P37): the run inventory is cached, freshness is not.

The board's run list is a ``GROUP BY`` over the whole ``signal`` table. On the
production Postgres that table is evicted from a 128 MB ``shared_buffers`` by a
much larger co-tenant application (decision P36), so every render paid a ~28 MB
cold read. Caching it is the half of the fix that is in our hands.

The trap the cache must not fall into: ``RunInfo.latest_ts`` feeds the page's
"Son baskı ... önce" line. Cache that and the board states an age it no longer
knows to be true — which breaks the contract's first honesty rule. So these
tests pin both halves: the inventory ages, the freshness never does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.repo import RunInfo, RunListCache, SignalRepo

if TYPE_CHECKING:
    from collections.abc import Iterator

_T0 = datetime(2026, 9, 17, 13, 30, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _CountingRepo:
    """A repo stand-in that counts reads and can be made to fail."""

    def __init__(self) -> None:
        self.calls = 0
        self.value: list[RunInfo] = [RunInfo("live-2026-09-17", 10, _T0)]
        self.error: Exception | None = None

    def runs(self) -> list[RunInfo]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def _cache(repo: object, clock: _Clock, ttl: float = 60.0) -> RunListCache:
    return RunListCache(repo, ttl, clock=clock)  # type: ignore[arg-type]


def test_a_second_render_inside_the_ttl_does_not_touch_the_database() -> None:
    repo, clock = _CountingRepo(), _Clock()
    cache = _cache(repo, clock)

    assert cache.runs() == repo.value
    clock.now += 59.0
    assert cache.runs() == repo.value
    assert repo.calls == 1, "the expensive GROUP BY ran twice inside its TTL"


def test_the_inventory_is_re_read_once_the_ttl_expires() -> None:
    repo, clock = _CountingRepo(), _Clock()
    cache = _cache(repo, clock)
    cache.runs()

    clock.now += 60.0
    repo.value = [RunInfo("live-2026-09-17", 11, _T0 + timedelta(minutes=5))]
    assert cache.runs() == repo.value
    assert repo.calls == 2


def test_a_zero_ttl_disables_the_cache_entirely() -> None:
    repo, clock = _CountingRepo(), _Clock()
    cache = _cache(repo, clock, ttl=0.0)
    cache.runs()
    cache.runs()
    assert repo.calls == 2


def test_a_failed_read_is_never_cached_so_an_outage_still_shows() -> None:
    """Serving the last good list through a real outage would let the board
    claim data it cannot read. The route's "veriyi okuyamadım" state must win."""
    repo, clock = _CountingRepo(), _Clock()
    cache = _cache(repo, clock)
    assert cache.runs() == repo.value

    clock.now += 61.0
    repo.error = RuntimeError("connection reset")
    with pytest.raises(RuntimeError):
        cache.runs()

    # And the failure did not poison the cache either: recovery is immediate.
    repo.error = None
    assert cache.runs() == repo.value


def test_invalidate_forces_the_next_read(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, clock = _CountingRepo(), _Clock()
    cache = _cache(repo, clock)
    cache.runs()
    cache.invalidate()
    cache.runs()
    assert repo.calls == 2


# ---------------------------------------------------------------------------
# The freshness half: latest_ts is read live, from the real repo.
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_with_two_runs(tmp_path: Path) -> Iterator[SignalRepo]:
    from uoa_detector.backtest.sqlite_models import Base

    url = f"sqlite:///{tmp_path / 'runs.db'}"
    repo = SignalRepo(url)
    Base.metadata.create_all(repo._engine)
    yield repo
    repo._engine.dispose()


def _insert(repo: SignalRepo, run_id: str, event_id: str, ts: datetime) -> None:
    from sqlalchemy import insert, select

    from uoa_detector.backtest.sqlite_models import BacktestRunRow, SignalRow

    with repo._session() as session:
        exists = session.execute(
            select(BacktestRunRow.run_id).where(BacktestRunRow.run_id == run_id),
        ).scalar_one_or_none()
        if exists is None:
            session.execute(insert(BacktestRunRow).values(
                run_id=run_id, started_at=ts, profile_id="p",
                profile_content_hash="h", total_signals_processed=0, total_errors=0,
            ))
        session.execute(insert(SignalRow).values(
            run_id=run_id, event_id=event_id, ts=ts, ticker="SPY", label="standard_uoa",
            combined_score_post=50.0, max_r=1.0, full_record_json="{}",
            profile_id="p", profile_content_hash="h",
        ))
        session.commit()


def _only(repo: SignalRepo, run_id: str) -> RunInfo:
    return next(r for r in repo.runs() if r.run_id == run_id)


def test_latest_ts_agrees_with_the_inventory_and_tracks_new_prints(
    repo_with_two_runs: SignalRepo,
) -> None:
    """``latest_ts`` must return exactly what the GROUP BY would have, for one
    run. Asserted against ``runs()`` rather than a literal, because SQLite
    returns this column naive and Postgres returns it tz-aware — the invariant
    is that both readers agree, whichever driver is under them."""
    repo = repo_with_two_runs
    _insert(repo, "live-2026-09-17", "e1", _T0)
    _insert(repo, "backtest-x", "e2", _T0 + timedelta(days=9))

    assert repo.latest_ts("live-2026-09-17") == _only(repo, "live-2026-09-17").latest_ts
    assert repo.latest_ts("backtest-x") == _only(repo, "backtest-x").latest_ts
    assert repo.latest_ts("no-such-run") is None

    before = repo.latest_ts("live-2026-09-17")
    _insert(repo, "live-2026-09-17", "e3", _T0 + timedelta(minutes=7))
    after = repo.latest_ts("live-2026-09-17")
    assert after is not None and before is not None
    assert after > before, "a new print did not move the freshness reading"
    assert after == _only(repo, "live-2026-09-17").latest_ts


def test_the_cache_goes_stale_where_latest_ts_does_not(
    repo_with_two_runs: SignalRepo,
) -> None:
    """The two halves, side by side: same moment, cached count vs live time."""
    repo = repo_with_two_runs
    _insert(repo, "live-2026-09-17", "e1", _T0)
    clock = _Clock()
    cache = RunListCache(repo, 60.0, clock=clock)

    cached = cache.runs()
    assert [(r.run_id, r.count) for r in cached] == [("live-2026-09-17", 1)]
    stale_ts = cached[0].latest_ts

    _insert(repo, "live-2026-09-17", "e2", _T0 + timedelta(minutes=7))

    assert cache.runs()[0].latest_ts == stale_ts, "inventory is allowed to age"
    live_ts = repo.latest_ts("live-2026-09-17")
    assert live_ts is not None and stale_ts is not None
    assert live_ts > stale_ts, "the freshness line must never be served from the cache"
