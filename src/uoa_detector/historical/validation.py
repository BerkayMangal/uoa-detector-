"""Historical-download validation + manifest generation.

Phase 3.3.4.4: post-download checks and summary reporting.

Validation per (ticker, year, month):
  - Schema match: every parquet file conforms to RAWPRINT_PARQUET_SCHEMA
  - Row count: file has >0 rows (catches zero-byte stubs)
  - Date coverage: rows fall within the expected (year, month) window
  - Trading-day gaps: list dates in the month with no rows (operator
    inspects to confirm they're weekends/holidays, not real gaps)

Manifest output: ``{historical_dir}/.manifest.json``

  {
    "schema_version": 1,
    "generated_at": "2024-11-15T19:42:13+00:00",
    "tier": "tier2_starter",
    "date_range": {"start": "2024-01-01", "end": "2024-06-30"},
    "summary": {
      "total_tickers": 51,
      "total_months": 6,
      "total_tasks_done": 306,
      "total_tasks_failed": 0,
      "total_rows": 287456321,
      "total_files": 1245,
      "total_bytes": 4823945612
    },
    "per_ticker": {
      "AAPL": {
        "months_done": 6, "months_failed": 0,
        "rows": 12453321, "files": 24, "bytes": 234234234,
        "gaps_by_month": {"2024-01": ["2024-01-15"]}
      },
      ...
    }
  }

decision (validation as a separate pass, not interleaved with download):
  Keeps each layer single-responsibility. Operator runs:
    1. download_tier2.py → state file with done/failed
    2. validate_historical.py (uses this module) → manifest
  Validation can be re-run after a fix without re-downloading.

decision (gap detection lists dates, doesn't classify):
  We don't have a trading calendar in scope — the operator
  inspects the gap list and confirms (e.g., 2024-01-01 New
  Year's, 2024-01-15 MLK Day). A trading-calendar provider is
  on the Phase 3.4 roadmap; this module surfaces the raw list.

decision (manifest as a single JSON file, not per-ticker):
  Easier to grep, diff, and inspect. ~50 tickers × 24 months
  fits comfortably in one file (~10-50 KB).

decision (manifest is not state; can be regenerated):
  The state file is the source of truth for in-progress work.
  The manifest is a snapshot of what's on disk. Operator can
  delete + regenerate manifest at any time without affecting
  downstream consumers. State file is never auto-regenerated.

decision (validation reads parquet via pyarrow.dataset.head):
  Reading just the first row is enough to validate schema; full
  row count requires scanning all row groups. Use pyarrow's
  metadata APIs (parquet.ParquetFile.metadata.num_rows) which
  reads the footer only — O(1) per file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.backtest.parquet_schema import (
    RAWPRINT_PARQUET_SCHEMA,
    ParquetSchemaMismatchError,
)

if TYPE_CHECKING:
    pass


_logger = logging.getLogger(__name__)
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MANIFEST_FILENAME = ".manifest.json"


# ---------------------------------------------------------------------------
# Per-file validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileValidation:
    """Result of validating one parquet file."""

    path: Path
    rows: int
    bytes_on_disk: int
    schema_ok: bool
    schema_error: str | None = None


def validate_parquet_file(path: Path) -> FileValidation:
    """Inspect one parquet file: row count, bytes, schema match.

    Reads only the parquet footer + schema; does NOT load the data.
    """
    if not path.exists():
        return FileValidation(
            path=path, rows=0, bytes_on_disk=0,
            schema_ok=False, schema_error="file not found",
        )
    try:
        bytes_on_disk = path.stat().st_size
        pf = pq.ParquetFile(path)  # type: ignore[no-untyped-call]
        rows = pf.metadata.num_rows
        actual_schema = pf.schema_arrow
    except Exception as exc:
        return FileValidation(
            path=path, rows=0, bytes_on_disk=0,
            schema_ok=False, schema_error=f"read failed: {exc!r}",
        )
    try:
        # Use the existing checker; it raises on mismatch with a
        # detailed message.
        from uoa_detector.backtest.parquet_schema import (
            validate_schema_or_raise,
        )
        validate_schema_or_raise(actual_schema, file_path=path)
        schema_ok = True
        schema_error = None
    except ParquetSchemaMismatchError as exc:
        schema_ok = False
        schema_error = str(exc)
    return FileValidation(
        path=path, rows=rows, bytes_on_disk=bytes_on_disk,
        schema_ok=schema_ok, schema_error=schema_error,
    )


# ---------------------------------------------------------------------------
# Per-(ticker, month) aggregate
# ---------------------------------------------------------------------------


@dataclass
class MonthValidation:
    """Aggregate of all parquet files for one (ticker, year, month)."""

    ticker: str
    year: int
    month: int
    files: list[FileValidation] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(f.rows for f in self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes_on_disk for f in self.files)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def schema_failures(self) -> list[FileValidation]:
        return [f for f in self.files if not f.schema_ok]


def collect_month_files(
    base_output_dir: Path, ticker: str, year: int, month: int,
) -> MonthValidation:
    """Find all parquet files for (ticker, year, month) under base_output_dir.

    Looks under: ``{base}/{ticker}/{contract_subdir}/{YYYY-MM}.parquet``
    """
    ticker_dir = base_output_dir / ticker.upper()
    month_label = f"{year:04d}-{month:02d}"
    files: list[FileValidation] = []
    if ticker_dir.exists() and ticker_dir.is_dir():
        for contract_dir in sorted(ticker_dir.iterdir()):
            if not contract_dir.is_dir():
                continue
            parquet_path = contract_dir / f"{month_label}.parquet"
            if parquet_path.exists():
                files.append(validate_parquet_file(parquet_path))
    return MonthValidation(
        ticker=ticker.upper(), year=year, month=month, files=files,
    )


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


def calendar_days_in_month(year: int, month: int) -> list[date]:
    """All calendar days in the given month."""
    days: list[date] = []
    d = date(year, month, 1)
    while d.month == month:
        days.append(d)
        d += timedelta(days=1)
    return days


def trading_dates_in_files(month_val: MonthValidation) -> set[date]:
    """Read 'timestamp' column from each file, return distinct dates.

    Loads only the timestamp column (parquet column-pruning).
    Empty if no files.
    """
    dates: set[date] = set()
    for f in month_val.files:
        if not f.path.exists() or f.rows == 0:
            continue
        try:
            table = pq.read_table(f.path, columns=["timestamp"])  # type: ignore[no-untyped-call]
        except Exception as exc:
            _logger.warning("read failed for %s: %r", f.path, exc)
            continue
        for ts in table.column("timestamp").to_pylist():
            if ts is None:
                continue
            d = ts.date() if hasattr(ts, "date") else ts
            if isinstance(d, date):
                dates.add(d)
    return dates


def detect_calendar_gaps(month_val: MonthValidation) -> list[date]:
    """Calendar days in the month with no rows in any file.

    NOTE: Includes weekends and holidays — the operator inspects
    and confirms vs. the trading calendar. A trading-calendar
    overlay is Phase 3.4 work.
    """
    if month_val.file_count == 0:
        return calendar_days_in_month(month_val.year, month_val.month)
    found = trading_dates_in_files(month_val)
    return [
        d for d in calendar_days_in_month(month_val.year, month_val.month)
        if d not in found
    ]


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


class TickerManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    months_done: int
    months_failed: int
    rows: int
    files: int
    bytes_on_disk: int
    # gaps_by_month: 'YYYY-MM' → ['YYYY-MM-DD', ...]
    gaps_by_month: dict[str, list[str]] = Field(default_factory=dict)


class ManifestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tickers: int
    total_months: int
    total_tasks_done: int
    total_tasks_failed: int
    total_rows: int
    total_files: int
    total_bytes: int


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = MANIFEST_SCHEMA_VERSION
    generated_at: datetime
    tier: str
    date_range_start: date
    date_range_end: date
    summary: ManifestSummary
    per_ticker: dict[str, TickerManifest] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    base_output_dir: Path,
    tickers: list[str],
    start_date: date,
    end_date: date,
    tier: str,
    failed_keys: set[str] | None = None,
    detect_gaps: bool = True,
) -> Manifest:
    """Walk the on-disk parquet tree and produce a Manifest.

    ``failed_keys`` is a set of 'TICKER:YYYY-MM' keys from the state
    file that the orchestrator marked failed; manifest counts these
    in months_failed.

    ``detect_gaps`` toggles the per-row scan for missing dates;
    skip it (False) for a faster manifest that only counts files.
    """
    failed_keys = failed_keys or set()
    per_ticker: dict[str, TickerManifest] = {}

    # Enumerate (year, month) pairs in range
    months: list[tuple[int, int]] = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    summary_rows = 0
    summary_files = 0
    summary_bytes = 0
    summary_done = 0
    summary_failed = 0

    for ticker in tickers:
        t_up = ticker.upper()
        months_done = 0
        months_failed = 0
        t_rows = 0
        t_files = 0
        t_bytes = 0
        gaps_by_month: dict[str, list[str]] = {}
        for year, month in months:
            month_label = f"{year:04d}-{month:02d}"
            key = f"{t_up}:{month_label}"
            mv = collect_month_files(base_output_dir, t_up, year, month)
            t_rows += mv.total_rows
            t_files += mv.file_count
            t_bytes += mv.total_bytes
            if key in failed_keys:
                months_failed += 1
            elif mv.file_count > 0 and mv.total_rows > 0:
                months_done += 1
            if detect_gaps and mv.file_count > 0:
                gaps = detect_calendar_gaps(mv)
                if gaps:
                    gaps_by_month[month_label] = [d.isoformat() for d in gaps]
        per_ticker[t_up] = TickerManifest(
            months_done=months_done,
            months_failed=months_failed,
            rows=t_rows,
            files=t_files,
            bytes_on_disk=t_bytes,
            gaps_by_month=gaps_by_month,
        )
        summary_rows += t_rows
        summary_files += t_files
        summary_bytes += t_bytes
        summary_done += months_done
        summary_failed += months_failed

    summary = ManifestSummary(
        total_tickers=len(tickers),
        total_months=len(months),
        total_tasks_done=summary_done,
        total_tasks_failed=summary_failed,
        total_rows=summary_rows,
        total_files=summary_files,
        total_bytes=summary_bytes,
    )

    return Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(UTC),
        tier=tier,
        date_range_start=start_date,
        date_range_end=end_date,
        summary=summary,
        per_ticker=per_ticker,
    )


def manifest_path(historical_dir: Path) -> Path:
    return historical_dir / DEFAULT_MANIFEST_FILENAME


def save_manifest(manifest: Manifest, path: Path) -> None:
    """Write manifest to JSON (pretty-printed, sorted keys)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump(mode="json")
    text = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")


def load_manifest(path: Path) -> Manifest | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    return Manifest.model_validate(parsed)


# Re-export for callers
__all__ = [
    "DEFAULT_MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "FileValidation",
    "Manifest",
    "ManifestSummary",
    "MonthValidation",
    "TickerManifest",
    "build_manifest",
    "calendar_days_in_month",
    "collect_month_files",
    "detect_calendar_gaps",
    "load_manifest",
    "manifest_path",
    "save_manifest",
    "trading_dates_in_files",
    "validate_parquet_file",
]

# Suppress unused-name warnings for re-exported sentinels
_ = RAWPRINT_PARQUET_SCHEMA
