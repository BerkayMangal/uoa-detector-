"""Phase 3.6.2 — daily chain-snapshot reader tests.

The as-of selection is the look-ahead guard: ``as_of(ticker, at)`` must
return the most-recent snapshot date ≤ ``at.date()`` and NEVER a future
day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from uoa_detector.sources.thetadata_derived.snapshot_reader import (
    DailyChainSnapshotSource,
)


def _write_snapshot(path: Path, rows: list[dict[str, object]]) -> None:
    table = pa.table(
        {
            "snapshot_date": pa.array(
                [r["snapshot_date"] for r in rows], type=pa.date32(),
            ),
            "strike": pa.array([r["strike"] for r in rows], type=pa.float64()),
            "expiry": pa.array(
                [r["expiry"] for r in rows], type=pa.date32(),
            ),
            "option_type": pa.array(
                [r["option_type"] for r in rows], type=pa.string(),
            ),
            "open_interest": pa.array(
                [r["open_interest"] for r in rows], type=pa.int64(),
            ),
            "implied_volatility": pa.array(
                [r["implied_volatility"] for r in rows], type=pa.float64(),
            ),
            "spot": pa.array([r["spot"] for r in rows], type=pa.float64()),
        },
    )
    pq.write_table(table, path)


def _row(d: date, strike: float, kind: str, spot: float) -> dict[str, object]:
    return {
        "snapshot_date": d,
        "strike": strike,
        "expiry": date(2025, 8, 15),
        "option_type": kind,
        "open_interest": 1000,
        "implied_volatility": 0.5,
        "spot": spot,
    }


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=UTC)


def _make_source(tmp_path: Path) -> DailyChainSnapshotSource:
    # Two snapshot days, a gap on 07-02, distinct spots to tell them apart.
    rows = [
        _row(date(2025, 7, 1), 100.0, "call", spot=100.0),
        _row(date(2025, 7, 1), 100.0, "put", spot=100.0),
        _row(date(2025, 7, 3), 110.0, "call", spot=110.0),
    ]
    _write_snapshot(tmp_path / "TSLA.parquet", rows)
    return DailyChainSnapshotSource(tmp_path)


def test_before_first_snapshot_returns_none(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    assert src.as_of("TSLA", _at(date(2025, 6, 30))) is None


def test_exact_snapshot_day(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    chain = src.as_of("TSLA", _at(date(2025, 7, 1)))
    assert chain is not None
    assert chain.snapshot_date == date(2025, 7, 1)
    assert chain.spot == Decimal("100.0")
    assert len(chain.contracts) == 2


def test_gap_day_falls_back_to_most_recent_prior(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    chain = src.as_of("TSLA", _at(date(2025, 7, 2)))
    assert chain is not None
    assert chain.snapshot_date == date(2025, 7, 1)  # not the future 07-03


def test_future_snapshot_never_returned(tmp_path: Path) -> None:
    # Querying on 07-02 must not surface the 07-03 chain (look-ahead).
    src = _make_source(tmp_path)
    chain = src.as_of("TSLA", _at(date(2025, 7, 2)))
    assert chain is not None
    assert chain.snapshot_date < date(2025, 7, 3)
    assert chain.spot == Decimal("100.0")  # 07-01 spot, not 07-03's 110


def test_after_last_snapshot_uses_last(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    chain = src.as_of("TSLA", _at(date(2025, 7, 10)))
    assert chain is not None
    assert chain.snapshot_date == date(2025, 7, 3)
    assert chain.spot == Decimal("110.0")


def test_unknown_ticker_returns_none(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    assert src.as_of("NOPE", _at(date(2025, 7, 5))) is None


def test_contracts_round_trip(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    chain = src.as_of("TSLA", _at(date(2025, 7, 1)))
    assert chain is not None
    kinds = sorted(c.option_type for c in chain.contracts)
    assert kinds == ["call", "put"]
    assert all(c.strike == Decimal("100.0") for c in chain.contracts)
    assert all(c.expiry == date(2025, 8, 15) for c in chain.contracts)
