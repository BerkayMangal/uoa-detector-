"""``ParquetReplaySource`` — historical-data ``RawFlowSource``.

Phase 3.2.2.2: implements the replay harness that feeds historical
parquet files through fusion exactly like a live source.

File layout (acceptance-doc pin):
  ``data/historical/{source}/{ticker}/{YYYY-MM}.parquet``

The source_id at construction time names the source dimension; the
ticker / month dimensions are resolved by globbing under the
``data_dir`` argument.

Streaming model:
  Each per-(ticker, month) file is opened as a PyArrow ``ParquetFile``;
  its row groups are streamed via ``iter_batches``. Per-file
  ``last_seen_timestamp`` tracking enforces the event-time-monotonic
  contract row by row; the first out-of-order row raises
  ``DataIntegrityError`` immediately, no full-file load. This matters
  at Tier-2 scale (~1200 files × up to 500K rows each).

Across files within a single (source, ticker), a k-way merge by
event timestamp produces a single ascending stream — necessary because
two adjacent months can have overlapping last-second / first-second
events that must interleave correctly.

Across tickers: SourceFusion-side. The harness emits everything
through one ``stream()``; if the consumer is SourceFusion with
multiple sources, three harness instances feed into it (one per
source_id); fusion does the per-source watermarking.

decision: snapshot the file list at the harness's first read, not at
each row. A file dropped into ``data_dir`` mid-replay is NOT picked
up — determinism precondition. Pinned by
``test_file_list_snapshot_at_replay_start``.

decision: edge-case handling for malformed files —
  - empty parquet (0 rows): warn-log and skip; not fatal
  - missing month in a contiguous range: warn-log gap and skip
  - corrupt parquet (raises during open or iter): re-raise as
    ``DataIntegrityError`` — a corrupt file is a data problem the
    operator must see, NOT a silent skip
  - schema mismatch: raise ``ParquetSchemaMismatchError`` (with
    regenerate hint) — same reasoning

decision: ``replay_speed`` parameter. ``inf`` (default) = batch mode,
emit as fast as possible. Positive float = real-time pacing scale
factor; ``1.0`` is real-time, ``10.0`` is 10× faster than walltime.
The pacing math is: per emission, sleep for ``(emit_walltime_now -
prev_emit_walltime) - (event_time - prev_event_time) /
replay_speed``. SourceFusion's watermark is event-time (Phase 2.3.3
guarantee, see ``test_window_differentiation_dict_equality``), so
correctness is independent of ``replay_speed`` — pinned by
``test_replay_at_inf_preserves_fusion_correctness`` in 3.2.2.3.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from uoa_detector.backtest.parquet_schema import (
    DataIntegrityError,
    row_to_raw_print,
    validate_schema_or_raise,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from uoa_detector.domain.raw_print import RawPrint


_logger = logging.getLogger(__name__)


_FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})\.parquet$")


class ParquetReplaySource:
    """Historical-data ``RawFlowSource`` reading from parquet files.

    See module docstring for the file-layout convention and the
    streaming / k-way-merge / pacing model.
    """

    def __init__(
        self,
        source_id: str,
        data_dir: Path,
        *,
        tickers: Iterable[str] | None = None,
        from_month: str | None = None,
        to_month: str | None = None,
        replay_speed: float = math.inf,
        min_premium_usd: Decimal | None = None,
    ) -> None:
        """Initialise but do not yet read.

        ``data_dir`` is the per-source root (e.g.,
        ``data/historical/synthetic/``). The harness expects
        ``{ticker}/{YYYY-MM}.parquet`` directly under it.

        ``tickers``, ``from_month``, ``to_month`` filter the file glob;
        all default to "no filter". Months are ``"YYYY-MM"`` strings.

        ``replay_speed`` ≤ 0 raises ``ValueError`` — we accept ``inf``
        (default) and any positive finite float.

        ``min_premium_usd`` (Phase 3.5.5 A5): when set, prints whose
        ``premium_paid`` is below it are skipped — a candidate
        pre-filter so a full-universe replay processes the unusual-size
        trades rather than all ~245M retail prints. Default None = no
        filter (every print is emitted, exactly as before). Rows are
        still validated for timestamp monotonicity before being
        skipped, so the data-integrity contract is unaffected.
        """
        if replay_speed <= 0:
            msg = f"replay_speed must be > 0; got {replay_speed!r}"
            raise ValueError(msg)
        if min_premium_usd is not None and min_premium_usd < 0:
            msg = f"min_premium_usd must be >= 0; got {min_premium_usd!r}"
            raise ValueError(msg)
        self.source_id = source_id
        self._data_dir = data_dir
        self._tickers: frozenset[str] | None = (
            frozenset(t.upper() for t in tickers) if tickers is not None else None
        )
        self._from_month = from_month
        self._to_month = to_month
        self._replay_speed = replay_speed
        self._min_premium_usd = min_premium_usd
        self._closed = False
        # Snapshot the file list at the FIRST stream() call rather than
        # at __init__ — tests construct the source and expect it not to
        # crash on a not-yet-populated directory. The snapshot itself
        # is built-then-frozen on first use; subsequent stream() calls
        # would reuse the same snapshot if they happened (but typical
        # usage is one stream() per source instance).
        self._file_snapshot: list[Path] | None = None

    # ---- File discovery ------------------------------------------------

    def _discover_files(self) -> list[Path]:
        """Build the per-(ticker, month) file list under ``data_dir``.

        Filters applied: ticker whitelist (if any), from_month, to_month.
        Sort order: alphabetical by (ticker, month) — deterministic,
        used by the snapshot-at-start contract.
        """
        if not self._data_dir.exists():
            _logger.warning(
                "ParquetReplaySource(%s): data_dir does not exist: %s",
                self.source_id, self._data_dir,
            )
            return []

        files: list[Path] = []
        for ticker_dir in sorted(self._data_dir.iterdir()):
            if not ticker_dir.is_dir():
                continue
            ticker = ticker_dir.name.upper()
            if self._tickers is not None and ticker not in self._tickers:
                continue

            # Collect parquet files under this ticker, parse YYYY-MM.
            ticker_files: list[tuple[str, Path]] = []
            for path in sorted(ticker_dir.iterdir()):
                m = _FILENAME_RE.match(path.name)
                if m is None:
                    continue
                month_key = f"{m.group(1)}-{m.group(2)}"
                if self._from_month is not None and month_key < self._from_month:
                    continue
                if self._to_month is not None and month_key > self._to_month:
                    continue
                ticker_files.append((month_key, path))

            # Gap detection within the chosen range.
            self._log_month_gaps(ticker, ticker_files)
            files.extend(p for _, p in ticker_files)

        return files

    def _log_month_gaps(
        self, ticker: str, ticker_files: list[tuple[str, Path]],
    ) -> None:
        """Warn-log any missing month between the first and last present."""
        if len(ticker_files) < 2:
            return
        months = sorted(month for month, _ in ticker_files)
        first, last = months[0], months[-1]
        # Build the inclusive month range first..last and diff against
        # the present set. Pure string math (we only deal with YYYY-MM).
        expected: list[str] = []
        y, m = (int(p) for p in first.split("-"))
        end_y, end_m = (int(p) for p in last.split("-"))
        while (y, m) <= (end_y, end_m):
            expected.append(f"{y:04d}-{m:02d}")
            m += 1
            if m == 13:
                y += 1
                m = 1
        present = {month for month, _ in ticker_files}
        gaps = [e for e in expected if e not in present]
        if gaps:
            _logger.warning(
                "ParquetReplaySource(%s): ticker %s has gaps in months: %s",
                self.source_id, ticker, gaps,
            )

    # ---- Per-file streaming with ordering validation ------------------

    def _iter_file(self, file_path: Path) -> Iterable[RawPrint]:
        """Stream rows from one parquet file, validating ordering inline.

        Opens with ``ParquetFile.iter_batches``; for each batch, emits
        rows in order; per-file ``last_seen_timestamp`` rejects any row
        that goes back in time. Empty files (header only, 0 rows) are
        skipped with a log message.

        Schema is validated on first open. Read errors are wrapped as
        ``DataIntegrityError``.
        """
        try:
            pf = pq.ParquetFile(file_path)  # type: ignore[no-untyped-call]
        except Exception as exc:
            msg = f"Failed to open parquet file {file_path}: {exc}"
            raise DataIntegrityError(msg) from exc

        validate_schema_or_raise(pf.schema_arrow, file_path=file_path)

        if pf.metadata.num_rows == 0:
            _logger.warning(
                "ParquetReplaySource(%s): empty file skipped: %s",
                self.source_id, file_path,
            )
            return

        last_seen_ts: datetime | None = None
        try:
            batches = pf.iter_batches(  # type: ignore[no-untyped-call]
                batch_size=1024,
            )
        except Exception as exc:
            msg = f"Failed to iterate batches in {file_path}: {exc}"
            raise DataIntegrityError(msg) from exc

        for batch in batches:
            for row in batch.to_pylist():
                ts = row["timestamp"]
                if not isinstance(ts, datetime):
                    msg = (
                        f"{file_path}: row timestamp is not a datetime: {ts!r}"
                    )
                    raise DataIntegrityError(msg)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if last_seen_ts is not None and ts < last_seen_ts:
                    msg = (
                        f"{file_path}: timestamps are not monotonic — row "
                        f"with timestamp {ts.isoformat()} follows "
                        f"{last_seen_ts.isoformat()}. Streaming "
                        "validation rejects this immediately. Regenerate "
                        "or pre-sort the source data."
                    )
                    raise DataIntegrityError(msg)
                last_seen_ts = ts
                # A5 candidate pre-filter: skip below-threshold prints
                # AFTER the monotonicity check (the integrity contract
                # covers every row on disk, filtered or not).
                if self._min_premium_usd is not None:
                    premium_raw = row.get("premium_paid")
                    if (
                        premium_raw is not None
                        and Decimal(str(premium_raw)) < self._min_premium_usd
                    ):
                        continue
                # Pydantic validation may raise; wrap as DataIntegrityError.
                try:
                    yield row_to_raw_print(row)
                except Exception as exc:
                    msg = (
                        f"{file_path}: row failed RawPrint validation: {exc}"
                    )
                    raise DataIntegrityError(msg) from exc

    # ---- K-way merge across files for a single source ----------------

    def _merged_stream(self, files: list[Path]) -> Iterable[RawPrint]:
        """K-way merge across ``files`` by event timestamp.

        Each per-file iterator is event-time-monotonic by the validation
        in ``_iter_file``; ``heapq.merge`` with a key on ``timestamp``
        yields a single ascending stream. ``heapq.merge`` is lazy —
        only one row per file is buffered in the heap at any time.
        """
        iterables = [self._iter_file(f) for f in files]
        yield from heapq.merge(*iterables, key=lambda p: p.timestamp)

    # ---- RawFlowSource Protocol surface --------------------------------

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield ``RawPrint``s in event-time order across the snapshot.

        Snapshot is built on first call. Replay pacing applies between
        emissions when ``replay_speed`` is finite.
        """
        if self._file_snapshot is None:
            self._file_snapshot = self._discover_files()

        if not self._file_snapshot:
            return

        # Per-emission pacing state. ``prev_event_ts`` is the previous
        # row's event time; ``prev_emit_walltime`` is the walltime we
        # last emitted at. Combined: sleep until the elapsed walltime
        # matches the elapsed event-time scaled by replay_speed.
        prev_event_ts: datetime | None = None
        loop = asyncio.get_event_loop()
        prev_emit_walltime: float | None = None

        for raw in self._merged_stream(self._file_snapshot):
            if self._closed:
                return

            if (
                math.isfinite(self._replay_speed)
                and prev_event_ts is not None
                and prev_emit_walltime is not None
            ):
                event_dt = (raw.timestamp - prev_event_ts).total_seconds()
                target_walltime_dt = event_dt / self._replay_speed
                actual_walltime_dt = loop.time() - prev_emit_walltime
                sleep_for = target_walltime_dt - actual_walltime_dt
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

            # Tag with replay_ts at emission time. RawPrint is frozen,
            # but replay_ts is not a RawPrint field — it lives on the
            # parquet schema only and is stripped going through
            # row_to_raw_print. Downstream stream consumers (fusion,
            # store) read RawPrint, not the parquet row; replay_ts is
            # captured in the SQLite signal row's full_record_json
            # only when explicitly threaded through. For 3.2.2.2 we
            # only emit RawPrint; replay_ts threading is a future
            # enhancement when it becomes useful for diff debugging.
            yield raw

            prev_event_ts = raw.timestamp
            prev_emit_walltime = loop.time()

    async def close(self) -> None:
        """Mark the source closed; subsequent ``stream`` iterations stop."""
        self._closed = True

    # ---- Test introspection (not part of RawFlowSource Protocol) ------

    @property
    def file_snapshot(self) -> list[Path] | None:
        """Return the discovered file list, or ``None`` if not yet streamed.

        Exposed so tests can assert the snapshot was built once and not
        re-discovered mid-iteration.
        """
        return list(self._file_snapshot) if self._file_snapshot is not None else None

    def force_snapshot_now(self) -> list[Path]:
        """Force file-list discovery now without iterating the stream.

        Used by ``test_file_list_snapshot_at_replay_start`` — the test
        wants to construct the source, snapshot, drop a new file, then
        confirm the new file is NOT picked up. Calling this directly
        avoids needing to start an async stream just to capture the
        snapshot.
        """
        if self._file_snapshot is None:
            self._file_snapshot = self._discover_files()
        return list(self._file_snapshot)
