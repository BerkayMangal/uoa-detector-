"""Phase 3.3.4.3 tests for ``historical.orchestrator``.

Pins:
  - contract_subdir_name: 'EXPYYMMDD_C_STRIKE' format
  - contract_output_dir composes path correctly
  - expand_universe_to_tasks: cartesian product, sorted, uppercase
  - run_one_task happy-path: contracts fetched, rows summed
  - run_one_task: empty contracts list → row_count=0, no error
  - run_one_task: contracts_for_ticker raises → outcome.error set
  - run_one_task: per-contract failure → continue; first error captured
  - run() iterates pending tasks and saves state after each
  - state-checkpoint after every task (even on failure)
  - skipped contracts counted (idempotent re-runs)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from uoa_detector.historical.orchestrator import (
    HistoricalOrchestrator,
    contract_output_dir,
    contract_subdir_name,
    expand_universe_to_tasks,
)
from uoa_detector.historical.state import (
    STATE_SCHEMA_VERSION,
    DateRange,
    DownloadState,
    load_state,
    state_path,
)
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
    DownloadResult,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_contract_subdir_name_format() -> None:
    """'EXPYYMMDD_C_STRIKE_8DIGITS'."""
    c = ContractSpec(
        ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("150.00"), right="C",
    )
    assert contract_subdir_name(c) == "EXP240216_C_00150000"


def test_contract_subdir_name_put_with_decimal_strike() -> None:
    c = ContractSpec(
        ticker="MSFT", expiry=date(2024, 12, 20),
        strike_dollars=Decimal("412.50"), right="P",
    )
    assert contract_subdir_name(c) == "EXP241220_P_00412500"


def test_contract_output_dir_composes_correctly(tmp_path: Path) -> None:
    c = ContractSpec(
        ticker="AAPL", expiry=date(2024, 2, 16),
        strike_dollars=Decimal("150.00"), right="C",
    )
    p = contract_output_dir(tmp_path, c)
    # Layout: base/{contract_subdir}; downloader adds /{ticker}/{ym}.parquet
    assert p == tmp_path / "EXP240216_C_00150000"


# ---------------------------------------------------------------------------
# expand_universe_to_tasks
# ---------------------------------------------------------------------------


def test_expand_universe_cartesian_sorted_uppercased() -> None:
    tasks = expand_universe_to_tasks(
        tickers=["msft", "aapl"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
    )
    # 2 tickers × 3 months = 6 tasks; sorted by (ticker, year, month)
    assert tasks == [
        ("AAPL", 2024, 1), ("AAPL", 2024, 2), ("AAPL", 2024, 3),
        ("MSFT", 2024, 1), ("MSFT", 2024, 2), ("MSFT", 2024, 3),
    ]


def test_expand_universe_year_boundary() -> None:
    tasks = expand_universe_to_tasks(
        tickers=["AAPL"],
        start_date=date(2023, 11, 1),
        end_date=date(2024, 2, 29),
    )
    assert tasks == [
        ("AAPL", 2023, 11), ("AAPL", 2023, 12),
        ("AAPL", 2024, 1), ("AAPL", 2024, 2),
    ]


def test_expand_universe_end_before_start_raises() -> None:
    with pytest.raises(ValueError, match="before"):
        expand_universe_to_tasks(
            tickers=["AAPL"],
            start_date=date(2024, 6, 1),
            end_date=date(2024, 1, 1),
        )


def test_expand_universe_empty_tickers_returns_empty() -> None:
    tasks = expand_universe_to_tasks(
        tickers=[],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
    )
    assert tasks == []


# ---------------------------------------------------------------------------
# Orchestrator: fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingDownloader:
    """Stand-in for ThetaDataHistoricalDownloader.

    Records calls and returns canned DownloadResult lists.
    """

    raise_on: set[str] | None = None  # contract subdir names to fail on
    rows_per_call: int = 100

    def __post_init__(self) -> None:
        self.calls: list = []
        if self.raise_on is None:
            self.raise_on = set()

    async def download_request(self, request) -> list[DownloadResult]:  # type: ignore[no-untyped-def]
        from uoa_detector.historical.orchestrator import contract_subdir_name as _csn
        subdir = _csn(request.contract)
        self.calls.append((request.contract, request.start_date, request.end_date))
        if self.raise_on and subdir in self.raise_on:
            msg = f"injected failure for {subdir}"
            raise RuntimeError(msg)
        # Fake one DownloadResult per month in the range
        return [DownloadResult(
            contract=request.contract,
            year=request.start_date.year,
            month=request.start_date.month,
            file_path=request.output_dir / f"{request.start_date.strftime('%Y-%m')}.parquet",
            skipped=False,
            row_count=self.rows_per_call,
        )]


def _make_contract(ticker: str, strike: str = "150.00") -> ContractSpec:
    return ContractSpec(
        ticker=ticker, expiry=date(2024, 2, 16),
        strike_dollars=Decimal(strike), right="C",
    )


def _const_snapshot(_c: ContractSpec, _d: date) -> ContextSnapshot:
    return ContextSnapshot(
        spot_price=Decimal("150.0"),
        open_interest=1000,
        implied_volatility=0.25,
    )


# ---------------------------------------------------------------------------
# run_one_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_task_happy_path_aggregates_rows(tmp_path: Path) -> None:
    contracts = [_make_contract("AAPL", "150.00"), _make_contract("AAPL", "155.00")]

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        return contracts

    downloader = _RecordingDownloader(rows_per_call=500)
    orch = HistoricalOrchestrator(
        downloader=downloader,  # type: ignore[arg-type]
        base_output_dir=tmp_path,
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcome = await orch.run_one_task("AAPL", 2024, 1)
    assert outcome.error is None
    assert outcome.contract_count == 2
    assert outcome.row_count == 1000  # 500 × 2 contracts


@pytest.mark.asyncio
async def test_run_one_task_empty_contracts_no_error(tmp_path: Path) -> None:
    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        return []

    orch = HistoricalOrchestrator(
        downloader=_RecordingDownloader(),  # type: ignore[arg-type]
        base_output_dir=tmp_path,
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcome = await orch.run_one_task("AAPL", 2024, 1)
    assert outcome.error is None
    assert outcome.contract_count == 0
    assert outcome.row_count == 0


@pytest.mark.asyncio
async def test_run_one_task_contracts_lookup_failure_recorded(tmp_path: Path) -> None:
    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        msg = "list endpoint down"
        raise RuntimeError(msg)

    orch = HistoricalOrchestrator(
        downloader=_RecordingDownloader(),  # type: ignore[arg-type]
        base_output_dir=tmp_path,
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcome = await orch.run_one_task("AAPL", 2024, 1)
    assert outcome.error is not None
    assert "list endpoint down" in outcome.error
    assert outcome.contract_count == 0


@pytest.mark.asyncio
async def test_run_one_task_per_contract_failure_continues(tmp_path: Path) -> None:
    """One failing contract doesn't abort the others."""
    contracts = [
        _make_contract("AAPL", "150.00"),
        _make_contract("AAPL", "155.00"),
        _make_contract("AAPL", "160.00"),
    ]

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        return contracts

    fail_subdir = "EXP240216_C_00155000"
    downloader = _RecordingDownloader(
        raise_on={fail_subdir}, rows_per_call=300,
    )
    orch = HistoricalOrchestrator(
        downloader=downloader,  # type: ignore[arg-type]
        base_output_dir=tmp_path,
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcome = await orch.run_one_task("AAPL", 2024, 1)
    # First error captured but other two contracts succeed
    assert outcome.error is not None
    assert "EXP240216_C_00155000" in outcome.error
    assert outcome.contract_count == 3
    assert outcome.row_count == 600  # 300 × 2 successful contracts


# ---------------------------------------------------------------------------
# run() — full flow with state checkpointing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_iterates_pending_and_saves_state(tmp_path: Path) -> None:
    state = DownloadState(
        schema_version=STATE_SCHEMA_VERSION,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        tier="tier_test",
        date_range=DateRange(start=date(2024, 1, 1), end=date(2024, 2, 29)),
        tasks={},
    )
    state.initialize_tasks([
        ("AAPL", 2024, 1), ("AAPL", 2024, 2),
    ])
    sp = state_path(tmp_path)

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        return [_make_contract("AAPL", "150.00")]

    orch = HistoricalOrchestrator(
        downloader=_RecordingDownloader(rows_per_call=42),  # type: ignore[arg-type]
        base_output_dir=tmp_path / "data",
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcomes = await orch.run(state=state, state_path=sp)
    assert len(outcomes) == 2
    assert all(o.error is None for o in outcomes)

    # State file persisted with both tasks done
    loaded = load_state(sp)
    assert loaded is not None
    assert loaded.tasks["AAPL:2024-01"].status == "done"
    assert loaded.tasks["AAPL:2024-02"].status == "done"
    assert loaded.tasks["AAPL:2024-01"].row_count == 42


@pytest.mark.asyncio
async def test_run_records_failure_in_state(tmp_path: Path) -> None:
    state = DownloadState(
        schema_version=STATE_SCHEMA_VERSION,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        tier="tier_test",
        date_range=DateRange(start=date(2024, 1, 1), end=date(2024, 1, 31)),
        tasks={},
    )
    state.initialize_tasks([("AAPL", 2024, 1)])
    sp = state_path(tmp_path)

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        msg = "boom"
        raise RuntimeError(msg)

    orch = HistoricalOrchestrator(
        downloader=_RecordingDownloader(),  # type: ignore[arg-type]
        base_output_dir=tmp_path / "data",
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcomes = await orch.run(state=state, state_path=sp)
    assert len(outcomes) == 1
    assert outcomes[0].error is not None

    loaded = load_state(sp)
    assert loaded is not None
    assert loaded.tasks["AAPL:2024-01"].status == "failed"
    assert "boom" in (loaded.tasks["AAPL:2024-01"].error or "")


@pytest.mark.asyncio
async def test_run_skips_already_done_tasks(tmp_path: Path) -> None:
    """A task already marked 'done' is not re-executed."""
    state = DownloadState(
        schema_version=STATE_SCHEMA_VERSION,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        tier="tier_test",
        date_range=DateRange(start=date(2024, 1, 1), end=date(2024, 1, 31)),
        tasks={},
    )
    # Pre-populate with done
    state.mark_done(
        "AAPL", 2024, 1, row_count=999,
        file_path=str(tmp_path / "AAPL"),
    )
    state.initialize_tasks([("AAPL", 2024, 1)])
    sp = state_path(tmp_path)

    contracts_lookup_calls = 0

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        nonlocal contracts_lookup_calls
        contracts_lookup_calls += 1
        return [_make_contract("AAPL")]

    orch = HistoricalOrchestrator(
        downloader=_RecordingDownloader(),  # type: ignore[arg-type]
        base_output_dir=tmp_path / "data",
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcomes = await orch.run(state=state, state_path=sp)
    assert outcomes == []  # nothing to do
    assert contracts_lookup_calls == 0


@pytest.mark.asyncio
async def test_run_concurrency_bound_respected(tmp_path: Path) -> None:
    """Verify that concurrency=2 caps simultaneous tasks at 2.

    Uses an asyncio.Event to gate downloader calls and counts
    in-flight callers.
    """
    import asyncio

    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()
    started = asyncio.Event()

    class _GatedDownloader:
        async def download_request(self, request):  # type: ignore[no-untyped-def]
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            started.set()
            try:
                await release.wait()
                return [DownloadResult(
                    contract=request.contract,
                    year=request.start_date.year,
                    month=request.start_date.month,
                    file_path=request.output_dir / "x.parquet",
                    skipped=False, row_count=1,
                )]
            finally:
                in_flight -= 1

    state = DownloadState(
        schema_version=STATE_SCHEMA_VERSION,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        tier="tier_test",
        date_range=DateRange(start=date(2024, 1, 1), end=date(2024, 4, 30)),
        tasks={},
    )
    state.initialize_tasks([
        ("AAPL", 2024, 1), ("AAPL", 2024, 2),
        ("AAPL", 2024, 3), ("AAPL", 2024, 4),
    ])

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        return [_make_contract("AAPL")]

    orch = HistoricalOrchestrator(
        downloader=_GatedDownloader(),  # type: ignore[arg-type]
        base_output_dir=tmp_path / "data",
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
        concurrency=2,
    )
    sp = state_path(tmp_path)
    task = asyncio.create_task(orch.run(state=state, state_path=sp))
    await started.wait()
    await asyncio.sleep(0.05)  # give all 2 a chance to start
    release.set()
    outcomes = await task
    assert len(outcomes) == 4
    assert max_in_flight <= 2


@pytest.mark.asyncio
async def test_run_skipped_contracts_counted(tmp_path: Path) -> None:
    """Idempotent re-run: skipped DownloadResult counted in skipped_contracts."""
    contracts = [_make_contract("AAPL", "150.00")]

    async def _lookup(_t: str) -> Iterable[ContractSpec]:
        return contracts

    class _SkippingDownloader:
        async def download_request(self, request):  # type: ignore[no-untyped-def]
            return [DownloadResult(
                contract=request.contract,
                year=request.start_date.year,
                month=request.start_date.month,
                file_path=request.output_dir / "skipped.parquet",
                skipped=True, row_count=1234,
            )]

    orch = HistoricalOrchestrator(
        downloader=_SkippingDownloader(),  # type: ignore[arg-type]
        base_output_dir=tmp_path,
        contracts_for_ticker=_lookup,
        snapshot_for=_const_snapshot,
    )
    outcome = await orch.run_one_task("AAPL", 2024, 1)
    assert outcome.error is None
    assert outcome.skipped_contracts == 1
    assert outcome.row_count == 1234
