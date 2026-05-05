"""Phase 3.2.2.2 tests for ``ParquetReplaySource``.

Pins the contracts the acceptance doc enumerates:
  - basic streaming (10-row fixture round-trips through the source)
  - streaming ordering validation rejects out-of-order rows mid-iteration
  - replay_speed validation (positive only)
  - file-list snapshot at first read; new files mid-replay ignored
  - empty parquet skipped with log
  - missing-month gaps logged but not fatal
  - corrupt parquet raises DataIntegrityError (NOT silent skip)
  - schema-mismatch raises ParquetSchemaMismatchError with regenerate hint
  - universe filter (tickers kwarg)
  - date-range filter (from_month / to_month)
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uoa_detector.backtest import (
    DataIntegrityError,
    ParquetSchemaMismatchError,
)
from uoa_detector.backtest.parquet_schema import (
    RAWPRINT_PARQUET_SCHEMA,
    make_synthetic_aapl_2025_06_fixture,
    write_parquet,
)
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.sources import ParquetReplaySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_print(
    ts: datetime,
    *,
    ticker: str = "AAPL",
    source_event_id: str = "e0",
    source_id: str = "synthetic_replay",
) -> RawPrint:
    return RawPrint(
        source_id=source_id,
        source_event_id=source_event_id,
        timestamp=ts,
        ticker=ticker,
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=39,
        spot_price=Decimal("198.50"),
        premium_paid=Decimal("1000"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        source_tags=(),
    )


def _drain(src: ParquetReplaySource) -> list[RawPrint]:
    async def go() -> list[RawPrint]:
        rows: list[RawPrint] = []
        async for r in src.stream():
            rows.append(r)
        await src.close()
        return rows

    return asyncio.run(go())


def _drain_or_raise(src: ParquetReplaySource) -> list[RawPrint]:
    """Drain; let exceptions from ``stream()`` propagate.

    ``stream()`` is an async-iterator; just calling ``run`` on the
    drain coroutine surfaces exceptions raised mid-iteration.
    """
    return _drain(src)


# ---------------------------------------------------------------------------
# Basic streaming — synthetic fixture
# ---------------------------------------------------------------------------


def test_replay_streams_synthetic_fixture() -> None:
    src = ParquetReplaySource(
        "synthetic_replay",
        Path("tests/fixtures/historical/synthetic"),
    )
    rows = _drain(src)
    assert len(rows) == 10
    # Strict ascending event time
    for i in range(9):
        assert rows[i].timestamp <= rows[i + 1].timestamp
    # Source identity preserved
    assert all(r.source_id == "synthetic_replay" for r in rows)


def test_replay_speed_validation() -> None:
    """replay_speed must be positive (inf is fine)."""
    with pytest.raises(ValueError, match="replay_speed"):
        ParquetReplaySource(
            "x", Path("nonexistent"), replay_speed=0,
        )
    with pytest.raises(ValueError, match="replay_speed"):
        ParquetReplaySource(
            "x", Path("nonexistent"), replay_speed=-1.0,
        )
    # These should construct without raising.
    ParquetReplaySource("x", Path("nonexistent"), replay_speed=math.inf)
    ParquetReplaySource("x", Path("nonexistent"), replay_speed=1.0)
    ParquetReplaySource("x", Path("nonexistent"), replay_speed=10.0)


def test_replay_handles_missing_data_dir() -> None:
    """Non-existent data_dir produces an empty stream with a warning."""
    src = ParquetReplaySource("test", Path("/does/not/exist"))
    assert _drain(src) == []


# ---------------------------------------------------------------------------
# Streaming ordering validation — Tier-2 scale critical
# ---------------------------------------------------------------------------


def test_streaming_ordering_validation_catches_out_of_order_at_iteration_time(
    tmp_path: Path,
) -> None:
    """A row whose timestamp is earlier than the previous row in the
    same file raises DataIntegrityError mid-iteration.

    The point of this test: the file ITSELF is malformed (rows 0..3
    ascending, row 4 goes back). A correct streaming validator catches
    row 4 without loading the rest of the file.
    """
    aapl_dir = tmp_path / "AAPL"
    rows = [
        _build_print(datetime(2025, 6, 9, 14, 30, tzinfo=UTC), source_event_id="e0"),
        _build_print(datetime(2025, 6, 9, 14, 31, tzinfo=UTC), source_event_id="e1"),
        _build_print(datetime(2025, 6, 9, 14, 32, tzinfo=UTC), source_event_id="e2"),
        _build_print(datetime(2025, 6, 9, 14, 33, tzinfo=UTC), source_event_id="e3"),
        # Goes BACKWARDS — must trigger DataIntegrityError on row 4.
        _build_print(datetime(2025, 6, 9, 14, 30, 30, tzinfo=UTC), source_event_id="e4"),
        _build_print(datetime(2025, 6, 9, 14, 34, tzinfo=UTC), source_event_id="e5"),
    ]
    aapl_dir.mkdir(parents=True)
    write_parquet(rows, aapl_dir / "2025-06.parquet")

    src = ParquetReplaySource("test", tmp_path)
    with pytest.raises(DataIntegrityError, match="not monotonic"):
        _drain_or_raise(src)


# ---------------------------------------------------------------------------
# File-list snapshot at replay start — determinism precondition
# ---------------------------------------------------------------------------


def test_file_list_snapshot_at_replay_start(tmp_path: Path) -> None:
    """Files added to disk after the snapshot are NOT picked up.

    Construct the source, snapshot, drop another file, drain — only
    the original file's rows come out.
    """
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)
    write_parquet(
        make_synthetic_aapl_2025_06_fixture(),
        aapl_dir / "2025-06.parquet",
    )

    src = ParquetReplaySource("test", tmp_path)
    snapshot = src.force_snapshot_now()
    assert len(snapshot) == 1

    # Now add a new file. Snapshot should NOT include it.
    msft_dir = tmp_path / "MSFT"
    msft_dir.mkdir(parents=True)
    write_parquet(
        [
            _build_print(
                datetime(2025, 6, 9, 14, 30, tzinfo=UTC),
                ticker="MSFT", source_event_id="msft-0",
            ),
        ],
        msft_dir / "2025-06.parquet",
    )

    rows = _drain(src)
    # Only the AAPL fixture's rows; the post-snapshot MSFT file is
    # invisible to this source instance.
    assert len(rows) == 10
    assert all(r.ticker == "AAPL" for r in rows)
    # And the captured snapshot itself reports only the original.
    assert len(src.file_snapshot or []) == 1


# ---------------------------------------------------------------------------
# Edge cases: empty file, missing month, corrupt parquet, schema mismatch
# ---------------------------------------------------------------------------


def test_empty_parquet_file_skipped_with_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A 0-row parquet file is skipped (warning logged), not fatal."""
    caplog.set_level(logging.WARNING, logger="uoa_detector.sources.parquet_replay")
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)

    # Build an empty table with the canonical schema.
    empty_table = pa.table(
        {f.name: pa.array([], type=f.type) for f in RAWPRINT_PARQUET_SCHEMA},
        schema=RAWPRINT_PARQUET_SCHEMA,
    )
    pq.write_table(  # type: ignore[no-untyped-call]
        empty_table, aapl_dir / "2025-06.parquet",
        compression="zstd", compression_level=3,
    )

    src = ParquetReplaySource("test", tmp_path)
    rows = _drain(src)
    assert rows == []
    assert any("empty file skipped" in msg for msg in caplog.messages)


def test_missing_month_file_skipped_with_gap_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Jan and Mar present, Feb absent → gap log warning, but no crash."""
    caplog.set_level(logging.WARNING, logger="uoa_detector.sources.parquet_replay")
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)

    write_parquet(
        [_build_print(datetime(2025, 1, 9, 14, 30, tzinfo=UTC))],
        aapl_dir / "2025-01.parquet",
    )
    write_parquet(
        [_build_print(datetime(2025, 3, 9, 14, 30, tzinfo=UTC))],
        aapl_dir / "2025-03.parquet",
    )

    src = ParquetReplaySource("test", tmp_path)
    rows = _drain(src)
    # Two months × 1 row each = 2; fully successful drain.
    assert len(rows) == 2
    # And the gap warning fired.
    assert any(
        "gaps" in msg and "2025-02" in msg for msg in caplog.messages
    ), f"expected gap warning, got: {caplog.messages}"


def test_corrupt_parquet_raises_data_integrity_error(tmp_path: Path) -> None:
    """A non-parquet file at a parquet path raises, NOT a silent skip.

    The acceptance doc is explicit: corrupt files are a data problem
    the operator must see. Continuing past would let a backtest report
    'success' on a partially-loaded dataset.
    """
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)
    bad = aapl_dir / "2025-06.parquet"
    bad.write_bytes(b"this is not a parquet file at all")

    src = ParquetReplaySource("test", tmp_path)
    with pytest.raises(DataIntegrityError, match="Failed to open"):
        _drain_or_raise(src)


def test_schema_version_mismatch_raises_with_regenerate_message(
    tmp_path: Path,
) -> None:
    """A parquet file missing a RawPrint field raises with regenerate hint.

    Simulates the post-RawPrint-schema-change scenario: a fixture from
    yesterday is now missing today's new field.
    """
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)

    # Build an "old" schema — drop implied_volatility.
    bad_schema = pa.schema(
        [f for f in RAWPRINT_PARQUET_SCHEMA if f.name != "implied_volatility"],
    )
    table = pa.table(
        {
            f.name: pa.array([], type=f.type)
            for f in RAWPRINT_PARQUET_SCHEMA
            if f.name != "implied_volatility"
        },
        schema=bad_schema,
    )
    pq.write_table(  # type: ignore[no-untyped-call]
        table, aapl_dir / "2025-06.parquet",
        compression="zstd", compression_level=3,
    )

    src = ParquetReplaySource("test", tmp_path)
    with pytest.raises(ParquetSchemaMismatchError) as exc:
        _drain_or_raise(src)
    msg = str(exc.value)
    assert "implied_volatility" in msg
    assert "Regenerate" in msg


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_universe_filter_restricts_tickers(tmp_path: Path) -> None:
    """tickers kwarg restricts which directories are read."""
    for ticker in ("AAPL", "MSFT", "NVDA"):
        d = tmp_path / ticker
        d.mkdir(parents=True)
        write_parquet(
            [
                _build_print(
                    datetime(2025, 6, 9, 14, 30, tzinfo=UTC),
                    ticker=ticker, source_event_id=f"{ticker}-0",
                ),
            ],
            d / "2025-06.parquet",
        )

    src = ParquetReplaySource(
        "test", tmp_path, tickers=["AAPL", "MSFT"],
    )
    rows = _drain(src)
    assert len(rows) == 2
    assert {r.ticker for r in rows} == {"AAPL", "MSFT"}


def test_date_range_filter_restricts_months(tmp_path: Path) -> None:
    """from_month / to_month restrict the file glob."""
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)
    for m, day in [(1, 9), (2, 9), (3, 9), (4, 9)]:
        write_parquet(
            [
                _build_print(
                    datetime(2025, m, day, 14, 30, tzinfo=UTC),
                    source_event_id=f"month-{m}",
                ),
            ],
            aapl_dir / f"2025-{m:02d}.parquet",
        )

    src = ParquetReplaySource(
        "test", tmp_path, from_month="2025-02", to_month="2025-03",
    )
    rows = _drain(src)
    assert len(rows) == 2
    assert {r.timestamp.month for r in rows} == {2, 3}


# ---------------------------------------------------------------------------
# K-way merge across multiple month files
# ---------------------------------------------------------------------------


def test_k_way_merge_across_months_yields_global_order(tmp_path: Path) -> None:
    """Two month files with overlap-prone boundary timestamps produce a
    single ascending stream from the harness's perspective.

    File A: Jun 30 23:59:55, Jun 30 23:59:58
    File B: Jul 1 00:00:01, Jul 1 00:00:05

    Ordering across files: A0, A1, B0, B1.
    """
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)
    write_parquet(
        [
            _build_print(
                datetime(2025, 6, 30, 23, 59, 55, tzinfo=UTC),
                source_event_id="a0",
            ),
            _build_print(
                datetime(2025, 6, 30, 23, 59, 58, tzinfo=UTC),
                source_event_id="a1",
            ),
        ],
        aapl_dir / "2025-06.parquet",
    )
    write_parquet(
        [
            _build_print(
                datetime(2025, 7, 1, 0, 0, 1, tzinfo=UTC),
                source_event_id="b0",
            ),
            _build_print(
                datetime(2025, 7, 1, 0, 0, 5, tzinfo=UTC),
                source_event_id="b1",
            ),
        ],
        aapl_dir / "2025-07.parquet",
    )

    src = ParquetReplaySource("test", tmp_path)
    rows = _drain(src)
    assert [r.source_event_id for r in rows] == ["a0", "a1", "b0", "b1"]


def test_close_stops_stream_early(tmp_path: Path) -> None:
    """close() before draining the iterator: stream() returns clean."""
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)
    write_parquet(
        make_synthetic_aapl_2025_06_fixture(),
        aapl_dir / "2025-06.parquet",
    )

    async def go() -> int:
        src = ParquetReplaySource("test", tmp_path)
        # Pre-close → stream yields nothing.
        await src.close()
        n = 0
        async for _ in src.stream():
            n += 1
        return n

    assert asyncio.run(go()) == 0


# ---------------------------------------------------------------------------
# Round-trip equivalence: scenario via SyntheticRawFlowSource vs
# ParquetReplaySource over the same data produces the same stream.
# ---------------------------------------------------------------------------


def test_replay_via_parquet_equals_synthetic_in_memory(tmp_path: Path) -> None:
    """Construct the same RawPrint sequence through both sources;
    drained streams must match field-for-field.

    This is the foundational "behave like a live source" property the
    acceptance doc commits to.
    """
    from uoa_detector.sources import SyntheticRawFlowSource

    prints = make_synthetic_aapl_2025_06_fixture()

    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir(parents=True)
    write_parquet(prints, aapl_dir / "2025-06.parquet")

    in_memory = SyntheticRawFlowSource("synthetic_replay", prints)
    in_mem_rows = _drain_inmemory(in_memory)

    replay = ParquetReplaySource("synthetic_replay", tmp_path)
    replay_rows = _drain(replay)

    assert in_mem_rows == replay_rows


def _drain_inmemory(src: object) -> list[RawPrint]:
    async def go() -> list[RawPrint]:
        rows: list[RawPrint] = []
        async for r in src.stream():  # type: ignore[attr-defined]
            rows.append(r)
        await src.close()  # type: ignore[attr-defined]
        return rows

    return asyncio.run(go())
