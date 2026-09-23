"""Atomic UW request reservation (Phase 5.24)."""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables, reserve, reserved_on

DAY = date(2026, 9, 23)


@pytest.fixture
def engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    eng = make_engine(f"sqlite:///{tmp_path / 'q.db'}")
    ensure_quota_tables(eng)
    return eng


def test_grants_up_to_the_cap_and_refuses_beyond(engine) -> None:  # type: ignore[no-untyped-def]
    assert reserve(engine, day=DAY, n=3, cap=5).granted
    assert reserve(engine, day=DAY, n=2, cap=5).granted
    refused = reserve(engine, day=DAY, n=1, cap=5)
    assert not refused.granted
    assert refused.reserved_after == 5
    assert reserved_on(engine, DAY) == 5


def test_a_refusal_spends_nothing(engine) -> None:  # type: ignore[no-untyped-def]
    reserve(engine, day=DAY, n=4, cap=5)
    assert not reserve(engine, day=DAY, n=2, cap=5).granted
    assert reserved_on(engine, DAY) == 4
    assert reserve(engine, day=DAY, n=1, cap=5).granted


def test_the_vendor_count_raises_the_floor(engine) -> None:  # type: ignore[no-untyped-def]
    """Requests other consumers made without reserving still count."""
    assert reserve(engine, day=DAY, n=1, cap=10).granted
    assert not reserve(engine, day=DAY, n=2, cap=10, observed=9).granted
    assert reserved_on(engine, DAY) == 9
    # A LOWER observation never lowers what is already reserved.
    assert reserve(engine, day=DAY, n=1, cap=10, observed=3).granted
    assert reserved_on(engine, DAY) == 10


def test_days_are_independent(engine) -> None:  # type: ignore[no-untyped-def]
    reserve(engine, day=DAY, n=5, cap=5)
    assert reserve(engine, day=date(2026, 9, 24), n=5, cap=5).granted


def test_concurrent_reservations_never_cross_the_cap(tmp_path: Path) -> None:
    """Forty threads race for seven slots on one file database: exactly seven win."""
    url = f"sqlite:///{tmp_path / 'race.db'}"
    setup = make_engine(url)
    ensure_quota_tables(setup)
    reserve(setup, day=DAY, n=1, cap=7)  # the row exists; one slot used
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker() -> None:
        eng = make_engine(url, )
        barrier.wait()
        for _ in range(50):
            try:
                granted = reserve(eng, day=DAY, n=1, cap=7).granted
            except Exception:  # sqlite "database is locked": retry, never a grant
                continue
            with lock:
                results.append(granted)
            return

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 6
    assert reserved_on(setup, DAY) == 7


def test_n_must_be_positive(engine) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="positive"):
        reserve(engine, day=DAY, n=0, cap=5)
