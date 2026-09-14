"""Historical bulk-download orchestrator.

Phase 3.3.4.3: drives ``ThetaDataHistoricalDownloader`` across a
universe of tickers × month range, with state-tracked resume,
concurrency bound, and per-task error isolation.

The orchestrator's loop:

  for each (ticker, year, month) in pending_tasks(state):
      acquire concurrency-semaphore slot
      mark_in_progress
      try:
          for contract in contracts_for_ticker(ticker):
              download_request(contract, month_range)
          row_count = sum_rows_in_month_parquet(ticker, year, month)
          mark_done
      except Exception as exc:
          mark_failed
      finally:
          save_state (atomic)
          release semaphore

Architecture notes:

  - The orchestrator does NOT enumerate contracts itself. Phase
    3.3.4.3 takes a ``contracts_for_ticker`` callable; production
    wires it to a ThetaData ``/v2/list/contracts`` lookup, tests
    pass a synthetic generator.

  - Per-task error isolation: one failed (ticker, month) doesn't
    abort the run. The error is recorded in state.tasks[key].error
    and the orchestrator continues. The operator inspects the
    state file and re-runs to retry failed tasks.

  - Snapshot resolution is the caller's concern (constructor takes
    a ``snapshot_for_date_for_contract`` factory). For 3.3.4.3 the
    operator wires ThetaData stock-quote endpoint here; tests use
    a constant snapshot.

decision (per-(ticker, month) atomic state checkpoint):
  After every task completion (done or failed), state is saved
  atomically. A crash between save points loses at most one
  task's progress — the next run re-attempts it. Trade-off: more
  I/O than buffering, but bisect-friendly state. Acceptable
  because state file is tiny (few KB even for 51 tickers × 24
  months).

decision (concurrency bound is process-wide via asyncio.Semaphore):
  Default 4 = ThetaData Pro plan limit. The semaphore wraps the
  ``download_request`` call, which itself uses an inner semaphore
  for per-day fetches inside one contract. We don't try to bound
  total HTTP requests across both layers — let the inner downloader
  enforce per-contract concurrency and the orchestrator bound
  task-level (ticker × month) concurrency.

decision (no built-in retry — relies on resume):
  If a task fails, state.error is set and the orchestrator moves
  on. The operator decides whether to re-run (which retries failed
  tasks). This is simpler than embedding a retry loop; ThetaData
  client already retries transient errors per request, so a task
  failure is usually a hard error worth surfacing.

decision (sum_rows_in_month_file used for done row_count):
  After download_request, we sum row counts across all contracts
  for that month (one parquet per contract per month is NOT the
  layout — the downloader writes one parquet per (ticker, month)
  combining all contracts for that ticker). Pinned by the parquet-
  schema convention from Phase 3.3.2.4.

  Wait — re-reading historical.py: the downloader writes per
  CONTRACT per MONTH (each download_request handles one contract).
  So per ticker we need to call download_request N times (N
  contracts for that ticker), each producing one parquet per
  month. The orchestrator must combine row counts.

  Actually the file path is historical_file_path(output, ticker,
  year, month) — the SAME path. If two contracts on the same
  ticker run, they'd overwrite each other. The downloader writes
  one parquet per ticker × month, with all contracts combined.
  Looking at download_request more carefully: it iterates months
  for ONE contract, writes parquet of that contract's prints only.

  So per ticker the operator either:
    (a) treats each contract as independent (separate parquet per
        contract per month — NEEDS A DIFFERENT PATH)
    (b) accepts that contracts overwrite (only the last one wins)

  Phase 3.3.4 must handle this. The cleanest path: per-contract
  parquet, NOT per-(ticker, month). Path:
  data/historical/thetadata/{ticker}/{contract_id}/{YYYY-MM}.parquet

  But the downloader's historical_file_path doesn't take contract.
  This is a Phase 3.3.4 design call.

  For 3.3.4.3 we adopt: per-contract parquet INSIDE
  data/historical/thetadata/{ticker}/{contract_subdir}/{YYYY-MM}.parquet.
  The orchestrator manages contract subdirectories; the downloader
  writes via output_dir = base/{ticker}/{contract_subdir}.
  Aggregation across contracts for row_count happens in the
  orchestrator.

decision (contract subdirectory naming: 'EXPYYMMDD_C_STRIKE'):
  Compact, lex-sortable, OPRA-style. e.g. AAPL/EXP240216_C_150000/2024-01.parquet
  for AAPL 2024-02-16 call $150.00. Strike encoded as 1/1000 USD
  (matching OCC convention). This means we can list contracts by
  scanning the directory tree without parsing parquet headers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from uoa_detector.historical.state import (
    DownloadState,
    save_state,
)
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
    DownloadRequest,
    DownloadResult,
    ThetaDataHistoricalDownloader,
)

if TYPE_CHECKING:
    pass


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Caller-supplied lookups
# ---------------------------------------------------------------------------


# A callable that returns the contracts to download for one ticker.
# Production wires this to a ThetaData /v2/list endpoint; tests pass
# a synchronous lambda.
ContractsForTicker = Callable[
    [str, date], "Awaitable[Iterable[ContractSpec]]",
]
# Phase 3.5.3.1: signature changed from ``[str]`` → ``[str, date]``.
# The orchestrator now passes the (ticker, month-anchor date) so
# the resolver can list contracts as_of that specific month, not a
# single global snapshot at start_date / end_date / today. This
# eliminates the 472-storm where contracts listed at end_date are
# fetched for trade-days 18 months in the past (contract wasn't
# listed then; every request returns 472 "no data").

# A callable that returns the daily context snapshot for a (contract, date).
# Production wires this to ThetaData stock-quote endpoint.
SnapshotForContractDate = Callable[
    [ContractSpec, date],
    "Awaitable[ContextSnapshot] | ContextSnapshot",
]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def contract_subdir_name(contract: ContractSpec) -> str:
    """Compact, lex-sortable contract identifier: 'EXPYYMMDD_C_STRIKE'.

    Strike is encoded as 1/1000 USD (OCC convention).
    Example: AAPL 2024-02-16 call $150.00 → 'EXP240216_C_00150000'.
    """
    yymmdd = (
        f"{contract.expiry.year % 100:02d}"
        f"{contract.expiry.month:02d}"
        f"{contract.expiry.day:02d}"
    )
    strike_int = int(contract.strike_dollars * Decimal(1000))
    return f"EXP{yymmdd}_{contract.right}_{strike_int:08d}"


def contract_output_dir(
    base_output_dir: Path, contract: ContractSpec,
) -> Path:
    """Path the orchestrator passes as ``output_dir`` to the downloader.

    The downloader's ``historical_file_path`` appends ``/{ticker}/
    {YYYY-MM}.parquet`` to whatever ``output_dir`` we pass. To avoid
    a double-ticker layer (``{base}/{ticker}/{subdir}/{ticker}/...``)
    the orchestrator gives a ticker-less path: ``{base}/{subdir}``.
    Final on-disk layout:
        {base}/{contract_subdir}/{ticker}/{YYYY-MM}.parquet
    """
    return base_output_dir / contract_subdir_name(contract)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskOutcome:
    """One (ticker, year, month) task's aggregated outcome."""

    ticker: str
    year: int
    month: int
    row_count: int
    contract_count: int
    skipped_contracts: int
    error: str | None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class HistoricalOrchestrator:
    """Drive ``ThetaDataHistoricalDownloader`` across a universe.

    The orchestrator is async-iterator-friendly: callers ``await
    run()`` for the full batch, or use ``run_one_task()`` for
    fine-grained control (e.g., dry-run mode that lists tasks
    without executing).

    Lifecycle:
      orch = HistoricalOrchestrator(
          downloader=ThetaDataHistoricalDownloader(client=...),
          base_output_dir=Path('data/historical/thetadata'),
          contracts_for_ticker=lookup,
          snapshot_for=resolver,
          concurrency=4,
      )
      await orch.run(state=state, state_path=path)
    """

    def __init__(
        self,
        *,
        downloader: ThetaDataHistoricalDownloader,
        base_output_dir: Path,
        contracts_for_ticker: ContractsForTicker,
        snapshot_for: SnapshotForContractDate,
        concurrency: int = 4,
    ) -> None:
        self._downloader = downloader
        self._base_output_dir = base_output_dir
        self._contracts_for_ticker = contracts_for_ticker
        self._snapshot_for = snapshot_for
        self._sem = asyncio.Semaphore(max(1, concurrency))

    # -- public API -----------------------------------------------------

    async def run(
        self,
        *,
        state: DownloadState,
        state_path: Path,
    ) -> list[TaskOutcome]:
        """Execute every pending task, checkpointing state after each.

        Returns a list of outcomes in completion order. Failed tasks
        are present with ``.error`` populated.
        """
        # Initialise tasks for every (ticker, year, month) implied by
        # the state's date_range and the universe (tickers already
        # registered in state.tasks). The caller is responsible for
        # calling state.initialize_tasks(...) before invoking run().
        outcomes: list[TaskOutcome] = []
        pending = state.pending_tasks()
        if not pending:
            _logger.info("No pending tasks; nothing to do.")
            return outcomes

        _logger.info(
            "Starting orchestrator: %d pending task(s), concurrency=%d",
            len(pending), self._sem._value,
        )

        async def _task_coro(
            ticker: str, year: int, month: int,
        ) -> TaskOutcome:
            async with self._sem:
                state.mark_in_progress(ticker, year, month)
                # Save in_progress so a crash mid-task is visible
                save_state(state, state_path)
                outcome = await self.run_one_task(ticker, year, month)
                if outcome.error is None:
                    state.mark_done(
                        ticker, year, month,
                        row_count=outcome.row_count,
                        file_path=str(
                            self._base_output_dir / ticker.upper(),
                        ),
                    )
                else:
                    state.mark_failed(
                        ticker, year, month, error=outcome.error,
                    )
                save_state(state, state_path)
                return outcome

        coros = [
            _task_coro(ticker, year, month)
            for ticker, year, month in pending
        ]
        results = await asyncio.gather(*coros, return_exceptions=False)
        outcomes.extend(results)
        return outcomes

    async def run_one_task(
        self, ticker: str, year: int, month: int,
    ) -> TaskOutcome:
        """Download every contract for one (ticker, year, month).

        Errors at the contract level are tolerated — the outcome
        records aggregated row_count + contract_count + error
        summary. A complete fetch failure (e.g., contracts list
        endpoint down) returns an outcome with ``.error`` set.
        """
        # Phase 3.5.3.1: anchor the contract resolution at mid-month
        # of THIS task. Contract listing per-month + per-(contract,
        # day) DTE guard together eliminate the 472 storm.
        asof = date(year, month, 15)
        try:
            contracts = list(
                await self._contracts_for_ticker(ticker, asof),
            )
        except Exception as exc:
            return TaskOutcome(
                ticker=ticker, year=year, month=month,
                row_count=0, contract_count=0, skipped_contracts=0,
                error=f"contracts_for_ticker({ticker}, {asof}) failed: {exc!r}",
            )

        from datetime import timedelta as _td

        month_start = date(year, month, 1)
        next_first = (
            date(year + 1, 1, 1) if month == 12
            else date(year, month + 1, 1)
        )
        month_end = next_first - _td(days=1)

        async def _download_one(
            contract: ContractSpec,
        ) -> list[DownloadResult]:
            output_dir = contract_output_dir(
                self._base_output_dir, contract,
            )

            def _snapshot_dispatcher(
                d: date, _c: ContractSpec = contract,
            ) -> Awaitable[ContextSnapshot] | ContextSnapshot:
                return self._snapshot_for(_c, d)

            req = DownloadRequest(
                contract=contract,
                start_date=month_start,
                end_date=month_end,
                output_dir=output_dir,
                snapshot_for_date=_snapshot_dispatcher,
            )
            return await self._downloader.download_request(req)

        total_rows = 0
        skipped = 0
        contract_errors = 0
        first_contract_error: str | None = None

        # Phase 3.5.3.6: download a ticker-month's contracts in parallel
        # batches. Pre-3.5.3.6 this was a sequential ``for`` loop — only
        # ~6 requests in flight (one per concurrent ticker-month task),
        # roughly a quarter of the 26 req/s token-bucket capacity, so
        # tasks ran 4× slower than the rate limit allowed. The global
        # rate-limiter still caps total throughput; batching at 16
        # keeps in-flight requests under the httpx connection-pool
        # ceiling while saturating the bucket.
        batch_size = 8
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i:i + batch_size]
            gathered = await asyncio.gather(
                *(_download_one(c) for c in batch),
                return_exceptions=True,
            )
            for contract, res in zip(batch, gathered, strict=True):
                if isinstance(res, BaseException):
                    contract_errors += 1
                    if first_contract_error is None:
                        first_contract_error = (
                            f"download {contract.ticker} "
                            f"{contract_subdir_name(contract)} "
                            f"failed: {res!r}"
                        )
                    _logger.warning(
                        "contract %s %s download failed: %r",
                        contract.ticker,
                        contract_subdir_name(contract), res,
                    )
                    continue
                for r in res:
                    total_rows += r.row_count
                    if r.skipped:
                        skipped += 1

        # Phase 3.5.3.5: a handful of transient contract failures must
        # NOT fail the whole ticker-month. Those contracts simply lack
        # a parquet on disk; the next idempotent resume pass retries
        # them. Only fail the task when a MAJORITY of contracts errored
        # — that signals something genuinely broken (endpoint down,
        # auth lost), not transient HTTP noise. Pre-3.5.3.5 the FIRST
        # contract error stamped the whole task "failed", which put
        # the run into an infinite retry-fail loop (every resume hit
        # a different transient contract and re-failed the task).
        task_error: str | None = None
        if contracts and contract_errors > len(contracts) // 2:
            task_error = (
                f"{contract_errors}/{len(contracts)} contracts failed "
                f"(majority) — first: {first_contract_error}"
            )

        return TaskOutcome(
            ticker=ticker, year=year, month=month,
            row_count=total_rows,
            contract_count=len(contracts),
            skipped_contracts=skipped,
            error=task_error,
        )


# ---------------------------------------------------------------------------
# Universe → tasks expansion (used by CLI script in 3.3.4.5)
# ---------------------------------------------------------------------------


def expand_universe_to_tasks(
    *,
    tickers: Iterable[str],
    start_date: date,
    end_date: date,
) -> list[tuple[str, int, int]]:
    """Cartesian product: tickers × months in [start, end].

    Returns sorted list of (ticker, year, month) tuples for use with
    DownloadState.initialize_tasks.
    """
    if end_date < start_date:
        msg = f"end_date ({end_date}) before start_date ({start_date})"
        raise ValueError(msg)
    months: list[tuple[int, int]] = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    out: list[tuple[str, int, int]] = []
    for ticker in tickers:
        for year, month in months:
            out.append((ticker.upper(), year, month))
    out.sort()
    return out


# Suppress the unused-import warning while keeping the namespace
# explicitly available for callers / tests.
_ = contextlib
