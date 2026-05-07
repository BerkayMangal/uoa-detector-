"""Phase 3.3.4.4 tests for ``historical.validation``.

Pins:
  - validate_parquet_file: schema OK + row count from real fixture
  - validate_parquet_file: missing file → schema_ok=False
  - collect_month_files: finds parquet under ticker/contract subdirs
  - calendar_days_in_month covers leap year
  - detect_calendar_gaps: empty month returns all calendar days
  - detect_calendar_gaps: file with timestamps removes those dates
  - build_manifest: tickers × months → per-ticker aggregates
  - build_manifest: failed_keys reflected in months_failed counter
  - manifest JSON round-trip
  - schema mismatch detected (write a wrong-schema parquet)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uoa_detector.backtest.parquet_schema import (
    make_synthetic_aapl_2025_06_fixture,
    write_parquet,
)
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.historical.validation import (
    DEFAULT_MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    build_manifest,
    calendar_days_in_month,
    collect_month_files,
    detect_calendar_gaps,
    load_manifest,
    manifest_path,
    save_manifest,
    validate_parquet_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_print(d: date, ticker: str = "AAPL") -> RawPrint:
    return RawPrint(
        source_id="test",
        source_event_id=f"e-{d.isoformat()}",
        timestamp=datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC),
        ticker=ticker,
        option_type="call",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=30,
        spot_price=Decimal("150.0"),
        premium_paid=Decimal("100.0"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        implied_volatility=0.25,
        open_interest=1000,
    )


def _write_test_parquet(path: Path, prints: list[RawPrint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(prints, path)


# ---------------------------------------------------------------------------
# validate_parquet_file
# ---------------------------------------------------------------------------


def test_validate_parquet_file_with_real_fixture(tmp_path: Path) -> None:
    """The Phase 3.2 synthetic fixture round-trips with schema_ok=True."""
    fixture = make_synthetic_aapl_2025_06_fixture()
    p = tmp_path / "aapl_2025_06.parquet"
    write_parquet(fixture, p)
    result = validate_parquet_file(p)
    assert result.schema_ok is True
    assert result.schema_error is None
    assert result.rows == len(fixture)
    assert result.bytes_on_disk > 0


def test_validate_parquet_file_missing(tmp_path: Path) -> None:
    result = validate_parquet_file(tmp_path / "nonexistent.parquet")
    assert result.schema_ok is False
    assert "not found" in (result.schema_error or "")
    assert result.rows == 0


def test_validate_parquet_file_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.parquet"
    p.write_bytes(b"this is not a parquet file")
    result = validate_parquet_file(p)
    assert result.schema_ok is False
    assert result.schema_error is not None


def test_validate_parquet_schema_mismatch(tmp_path: Path) -> None:
    """Wrong-schema parquet is detected."""
    p = tmp_path / "wrong_schema.parquet"
    table = pa.table({"random_col": [1, 2, 3]})
    pq.write_table(table, p)  # type: ignore[no-untyped-call]
    result = validate_parquet_file(p)
    assert result.schema_ok is False
    assert result.schema_error is not None


# ---------------------------------------------------------------------------
# collect_month_files
# ---------------------------------------------------------------------------


def test_collect_month_files_empty(tmp_path: Path) -> None:
    mv = collect_month_files(tmp_path, "AAPL", 2024, 1)
    assert mv.file_count == 0
    assert mv.total_rows == 0


def test_collect_month_files_finds_per_contract_files(tmp_path: Path) -> None:
    """Multiple contract subdirs under one ticker × one month."""
    base = tmp_path
    (base / "AAPL" / "EXP240216_C_00150000").mkdir(parents=True)
    (base / "AAPL" / "EXP240216_C_00155000").mkdir(parents=True)
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-01.parquet",
        [_make_print(date(2024, 1, 15))],
    )
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00155000" / "2024-01.parquet",
        [_make_print(date(2024, 1, 16)), _make_print(date(2024, 1, 17))],
    )
    mv = collect_month_files(base, "AAPL", 2024, 1)
    assert mv.file_count == 2
    assert mv.total_rows == 3


def test_collect_month_files_other_months_excluded(tmp_path: Path) -> None:
    base = tmp_path
    (base / "AAPL" / "EXP240216_C_00150000").mkdir(parents=True)
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-01.parquet",
        [_make_print(date(2024, 1, 15))],
    )
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-02.parquet",
        [_make_print(date(2024, 2, 15))],
    )
    mv = collect_month_files(base, "AAPL", 2024, 1)
    assert mv.file_count == 1


# ---------------------------------------------------------------------------
# Calendar / gap detection
# ---------------------------------------------------------------------------


def test_calendar_days_in_january_has_31() -> None:
    days = calendar_days_in_month(2024, 1)
    assert len(days) == 31
    assert days[0] == date(2024, 1, 1)
    assert days[-1] == date(2024, 1, 31)


def test_calendar_days_in_february_leap_year() -> None:
    days = calendar_days_in_month(2024, 2)
    assert len(days) == 29


def test_calendar_days_in_february_non_leap() -> None:
    days = calendar_days_in_month(2023, 2)
    assert len(days) == 28


def test_detect_calendar_gaps_empty_month(tmp_path: Path) -> None:
    """No files → all days are gaps."""
    mv = collect_month_files(tmp_path, "AAPL", 2024, 1)
    gaps = detect_calendar_gaps(mv)
    assert len(gaps) == 31


def test_detect_calendar_gaps_with_present_dates(tmp_path: Path) -> None:
    base = tmp_path
    (base / "AAPL" / "EXP240216_C_00150000").mkdir(parents=True)
    # Three trading days
    prints = [
        _make_print(date(2024, 1, 2)),
        _make_print(date(2024, 1, 3)),
        _make_print(date(2024, 1, 4)),
    ]
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-01.parquet",
        prints,
    )
    mv = collect_month_files(base, "AAPL", 2024, 1)
    gaps = detect_calendar_gaps(mv)
    assert date(2024, 1, 2) not in gaps
    assert date(2024, 1, 3) not in gaps
    assert date(2024, 1, 4) not in gaps
    assert date(2024, 1, 1) in gaps  # New Year's
    assert date(2024, 1, 5) in gaps


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def test_build_manifest_empty_universe(tmp_path: Path) -> None:
    m = build_manifest(
        base_output_dir=tmp_path,
        tickers=[],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        tier="tier_test",
    )
    assert m.summary.total_tickers == 0
    assert m.summary.total_months == 1
    assert m.per_ticker == {}


def test_build_manifest_with_one_complete_ticker(tmp_path: Path) -> None:
    base = tmp_path
    (base / "AAPL" / "EXP240216_C_00150000").mkdir(parents=True)
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-01.parquet",
        [_make_print(date(2024, 1, 15))],
    )
    m = build_manifest(
        base_output_dir=base,
        tickers=["AAPL"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        tier="tier_test",
    )
    assert m.summary.total_tickers == 1
    assert m.summary.total_months == 1
    assert m.summary.total_tasks_done == 1
    assert m.summary.total_files == 1
    assert m.summary.total_rows == 1
    assert "AAPL" in m.per_ticker
    aapl = m.per_ticker["AAPL"]
    assert aapl.months_done == 1
    assert aapl.months_failed == 0
    assert aapl.rows == 1
    assert aapl.files == 1


def test_build_manifest_failed_keys_counted(tmp_path: Path) -> None:
    """failed_keys from state file → months_failed in manifest."""
    m = build_manifest(
        base_output_dir=tmp_path,
        tickers=["AAPL"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 29),
        tier="tier_test",
        failed_keys={"AAPL:2024-01"},
    )
    aapl = m.per_ticker["AAPL"]
    assert aapl.months_failed == 1
    assert m.summary.total_tasks_failed == 1


def test_build_manifest_includes_gaps(tmp_path: Path) -> None:
    base = tmp_path
    (base / "AAPL" / "EXP240216_C_00150000").mkdir(parents=True)
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-01.parquet",
        [_make_print(date(2024, 1, 15))],
    )
    m = build_manifest(
        base_output_dir=base,
        tickers=["AAPL"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        tier="tier_test",
    )
    aapl = m.per_ticker["AAPL"]
    # 30 gap dates (31 calendar days minus 2024-01-15)
    gaps = aapl.gaps_by_month.get("2024-01", [])
    assert len(gaps) == 30
    assert "2024-01-15" not in gaps
    assert "2024-01-01" in gaps


def test_build_manifest_skip_gaps_for_speed(tmp_path: Path) -> None:
    """detect_gaps=False produces a manifest without per-row scans."""
    base = tmp_path
    (base / "AAPL" / "EXP240216_C_00150000").mkdir(parents=True)
    _write_test_parquet(
        base / "AAPL" / "EXP240216_C_00150000" / "2024-01.parquet",
        [_make_print(date(2024, 1, 15))],
    )
    m = build_manifest(
        base_output_dir=base,
        tickers=["AAPL"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        tier="tier_test",
        detect_gaps=False,
    )
    assert m.per_ticker["AAPL"].gaps_by_month == {}


# ---------------------------------------------------------------------------
# Manifest JSON round-trip
# ---------------------------------------------------------------------------


def test_manifest_save_load_round_trip(tmp_path: Path) -> None:
    m = build_manifest(
        base_output_dir=tmp_path,
        tickers=["AAPL"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        tier="tier_test",
    )
    path = manifest_path(tmp_path)
    save_manifest(m, path)
    loaded = load_manifest(path)
    assert loaded is not None
    assert loaded.tier == "tier_test"
    assert loaded.summary.total_tickers == 1
    assert loaded.schema_version == MANIFEST_SCHEMA_VERSION


def test_manifest_path_filename(tmp_path: Path) -> None:
    p = manifest_path(tmp_path)
    assert p.name == DEFAULT_MANIFEST_FILENAME


def test_load_missing_manifest_returns_none(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "no.json") is None


def test_manifest_extra_forbid() -> None:
    with pytest.raises(Exception):
        Manifest(  # type: ignore[call-arg]
            schema_version=1,
            generated_at=datetime.now(UTC),
            tier="x",
            date_range_start=date(2024, 1, 1),
            date_range_end=date(2024, 1, 31),
            summary={  # type: ignore[arg-type]
                "total_tickers": 0, "total_months": 1,
                "total_tasks_done": 0, "total_tasks_failed": 0,
                "total_rows": 0, "total_files": 0, "total_bytes": 0,
            },
            unknown_extra_field=42,
        )
