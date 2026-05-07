"""Phase 3.3.4.5 subset integration test for the tier-2 download CLI.

Pins:
  - End-to-end CLI flow: argparse → universe load → orchestrator
    → state file → manifest
  - Subset cap: --max-tasks limits work
  - Dry-run produces task list without HTTP calls
  - Validate-only rebuilds manifest from disk without orchestrator run
  - Disk-space check fires when disk insufficient
  - Failed task captured in state + manifest

The orchestrator's HTTP layer is replaced with fakes; this is a
unit-style test that exercises the full CLI plumbing without
touching the real ThetaData endpoint.

Berkay's directive: agent NEVER auto-runs production downloads.
This test uses mocks ONLY — there is no path through the assertions
that would hit the real API.
"""

from __future__ import annotations

import sys
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from uoa_detector.backtest.parquet_schema import write_parquet
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.historical.orchestrator import (
    HistoricalOrchestrator,
    expand_universe_to_tasks,
)
from uoa_detector.historical.state import (
    init_or_load_state,
    load_state,
    save_state,
    state_path,
)
from uoa_detector.historical.universe import read_universe, tickers_only
from uoa_detector.historical.validation import (
    build_manifest,
    load_manifest,
    manifest_path,
    save_manifest,
)
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
    DownloadResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


@pytest.fixture
def tier_csv(tmp_path: Path) -> Path:
    """A small 3-ticker universe for sandbox testing."""
    p = tmp_path / "tier_subset.csv"
    p.write_text(
        "ticker,sector,market_cap_usd_b_approx,"
        "options_volume_30d_avg_approx,notes\n"
        "AAPL,tech,~3000,~5000000,\n"
        "MSFT,tech,~2500,~4000000,\n"
        "GOOGL,tech,~1700,~2500000,\n",
    )
    return p


def _make_print(d: date, ticker: str = "AAPL") -> RawPrint:
    from datetime import datetime
    return RawPrint(
        source_id="thetadata",
        source_event_id=f"e-{d.isoformat()}-{ticker}",
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


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class _FakeDownloader:
    """Writes one fake parquet per (contract, year, month) request."""

    def __init__(self) -> None:
        self.calls: list = []

    async def download_request(self, request) -> list[DownloadResult]:  # type: ignore[no-untyped-def]
        self.calls.append((request.contract.ticker,
                            request.start_date, request.end_date))
        # Write a small parquet at the canonical path
        from uoa_detector.sources.thetadata.historical import (
            historical_file_path,
        )
        results = []
        # The downloader writes one file per month in the range; for our
        # subset test, the request covers exactly one month.
        year, month = request.start_date.year, request.start_date.month
        file_path = historical_file_path(
            request.output_dir, request.contract.ticker, year, month,
        )
        prints = [
            _make_print(request.start_date, request.contract.ticker),
            _make_print(request.start_date.replace(day=15), request.contract.ticker),
        ]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        write_parquet(prints, file_path)
        results.append(DownloadResult(
            contract=request.contract,
            year=year, month=month,
            file_path=file_path,
            skipped=False,
            row_count=len(prints),
        ))
        return results


# ---------------------------------------------------------------------------
# CLI: dry-run
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_dry_run_lists_tasks_no_state_change(
    tmp_path: Path, tier_csv: Path, capsys: pytest.CaptureFixture,
) -> None:
    """--dry-run prints the task table and exits 0 without calling HTTP."""
    sys.path.insert(0, str(tier_csv.parent.parent / "scripts"))
    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    download_tier2 = import_module("download_tier2")

    output_dir = tmp_path / "out"
    state_dir = tmp_path / "state_dir"
    rc = download_tier2.main([
        "--tier", "test_subset",
        "--tier-csv", str(tier_csv),
        "--start-date", "2024-01-01",
        "--end-date", "2024-02-29",
        "--output-dir", str(output_dir),
        "--state-dir", str(state_dir),
        "--dry-run",
        "--skip-disk-check",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert "DRY RUN" in captured.out
    assert "AAPL" in captured.out
    # Output dir should not have been populated
    assert not output_dir.exists() or list(output_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# CLI: validate-only
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_validate_only_rebuilds_manifest_from_disk(
    tmp_path: Path, tier_csv: Path,
) -> None:
    """--validate-only walks the parquet tree and writes manifest, no download."""
    output_dir = tmp_path / "out"
    state_dir = tmp_path / "state_dir"
    # Pre-populate the disk
    (output_dir / "EXP240216_C_00150000" / "AAPL").mkdir(parents=True)
    write_parquet(
        [_make_print(date(2024, 1, 15), "AAPL")],
        output_dir / "EXP240216_C_00150000" / "AAPL" / "2024-01.parquet",
    )

    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    download_tier2 = import_module("download_tier2")

    rc = download_tier2.main([
        "--tier", "test_subset",
        "--tier-csv", str(tier_csv),
        "--start-date", "2024-01-01",
        "--end-date", "2024-01-31",
        "--output-dir", str(output_dir),
        "--state-dir", str(state_dir),
        "--validate-only",
        "--skip-disk-check",
    ])
    assert rc == 0
    mp = manifest_path(state_dir)
    assert mp.exists()
    manifest = load_manifest(mp)
    assert manifest is not None
    # AAPL has 1 file, 1 row
    assert manifest.per_ticker["AAPL"].rows == 1
    assert manifest.per_ticker["AAPL"].files == 1


# ---------------------------------------------------------------------------
# Orchestrator end-to-end with mocked downloader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subset_run_full_orchestrator_state_and_manifest(
    tmp_path: Path, tier_csv: Path,
) -> None:
    """Full run: 3 tickers × 2 months × 1 contract = 6 tasks; verify
    state file, parquet on disk, and manifest."""
    entries = read_universe(tier_csv)
    tickers = list(tickers_only(entries))
    assert len(tickers) == 3

    output_dir = tmp_path / "out"
    state_dir = tmp_path / "state_dir"
    sp = state_path(state_dir)

    state = init_or_load_state(
        path=sp, tier="test_subset",
        start_date=date(2024, 1, 1), end_date=date(2024, 2, 29),
    )
    tasks = expand_universe_to_tasks(
        tickers=tickers,
        start_date=date(2024, 1, 1), end_date=date(2024, 2, 29),
    )
    state.initialize_tasks(tasks)
    save_state(state, sp)

    async def _contracts(_t: str) -> list[ContractSpec]:
        return [ContractSpec(
            ticker=_t, expiry=date(2024, 2, 16),
            strike_dollars=Decimal("150.00"), right="C",
        )]

    async def _snapshot(_c: ContractSpec, _d: date) -> ContextSnapshot:
        return ContextSnapshot(spot_price=Decimal("150.0"))

    orch = HistoricalOrchestrator(
        downloader=_FakeDownloader(),  # type: ignore[arg-type]
        base_output_dir=output_dir,
        contracts_for_ticker=_contracts,
        snapshot_for=_snapshot,
        concurrency=2,
    )
    outcomes = await orch.run(state=state, state_path=sp)
    assert len(outcomes) == 6
    assert all(o.error is None for o in outcomes)

    # State file: 6 tasks done
    loaded = load_state(sp)
    assert loaded is not None
    assert loaded.progress_summary()["done"] == 6

    # Manifest: build from disk
    manifest = build_manifest(
        base_output_dir=output_dir,
        tickers=tickers,
        start_date=date(2024, 1, 1), end_date=date(2024, 2, 29),
        tier="test_subset",
    )
    save_manifest(manifest, manifest_path(state_dir))
    assert manifest.summary.total_tickers == 3
    assert manifest.summary.total_months == 2
    assert manifest.summary.total_tasks_done == 6
    assert manifest.summary.total_rows == 12  # 2 prints × 6 tasks


# ---------------------------------------------------------------------------
# Disk-space check
# ---------------------------------------------------------------------------


def test_disk_check_fires_when_insufficient(tmp_path: Path) -> None:
    """check_disk_space returns ok=False when free < estimated."""
    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    download_tier2 = import_module("download_tier2")

    # Inject a fake free=0
    with patch("shutil.disk_usage") as mock_disk_usage:
        mock_disk_usage.return_value = type("DU", (), {"free": 0})()
        ok, msg = download_tier2.check_disk_space(
            output_dir=tmp_path, estimated_bytes=10 * 1024 * 1024,
        )
        assert ok is False
        assert "INSUFFICIENT" in msg


def test_disk_check_passes_when_sufficient(tmp_path: Path) -> None:
    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    download_tier2 = import_module("download_tier2")

    with patch("shutil.disk_usage") as mock_disk_usage:
        mock_disk_usage.return_value = type(
            "DU", (), {"free": 10 * 1024 * 1024 * 1024},  # 10 GB free
        )()
        ok, _ = download_tier2.check_disk_space(
            output_dir=tmp_path, estimated_bytes=10 * 1024 * 1024,
        )
        assert ok is True


def test_estimate_disk_bytes_default_factor() -> None:
    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    download_tier2 = import_module("download_tier2")

    # 10 tickers × 12 months × 50 MB × 1.5 safety = 9 GB
    est = download_tier2.estimate_disk_bytes(n_tickers=10, n_months=12)
    assert est == 10 * 12 * 50 * 1024 * 1024 * 3 // 2


# ---------------------------------------------------------------------------
# CLI: max-tasks subset cap
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_max_tasks_caps_work(
    tmp_path: Path, tier_csv: Path,
) -> None:
    """--max-tasks 2 limits state to 2 tasks even though 6 were possible."""
    output_dir = tmp_path / "out"
    state_dir = tmp_path / "state_dir"

    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    download_tier2 = import_module("download_tier2")

    rc = download_tier2.main([
        "--tier", "test_subset",
        "--tier-csv", str(tier_csv),
        "--start-date", "2024-01-01",
        "--end-date", "2024-02-29",
        "--output-dir", str(output_dir),
        "--state-dir", str(state_dir),
        "--dry-run",  # don't actually run
        "--max-tasks", "2",
        "--skip-disk-check",
    ])
    assert rc == 0
    sp = state_path(state_dir)
    state = load_state(sp)
    assert state is not None
    assert len(state.tasks) == 2


# ---------------------------------------------------------------------------
# Idempotent re-run via state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_after_partial_failure(
    tmp_path: Path, tier_csv: Path,
) -> None:
    """First run fails on one task; second run only retries that task."""
    output_dir = tmp_path / "out"
    state_dir = tmp_path / "state_dir"
    sp = state_path(state_dir)

    state = init_or_load_state(
        path=sp, tier="test_subset",
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
    )
    state.initialize_tasks([
        ("AAPL", 2024, 1), ("MSFT", 2024, 1),
    ])
    save_state(state, sp)

    # Round 1: MSFT contracts lookup fails
    fail_msft = [True]

    async def _contracts_round1(t: str) -> list[ContractSpec]:
        if t.upper() == "MSFT" and fail_msft[0]:
            msg = "MSFT failed"
            raise RuntimeError(msg)
        return [ContractSpec(
            ticker=t, expiry=date(2024, 2, 16),
            strike_dollars=Decimal("150.00"), right="C",
        )]

    async def _snapshot(_c: ContractSpec, _d: date) -> ContextSnapshot:
        return ContextSnapshot(spot_price=Decimal("150.0"))

    orch1 = HistoricalOrchestrator(
        downloader=_FakeDownloader(),  # type: ignore[arg-type]
        base_output_dir=output_dir,
        contracts_for_ticker=_contracts_round1,
        snapshot_for=_snapshot,
        concurrency=1,
    )
    await orch1.run(state=state, state_path=sp)
    summary1 = state.progress_summary()
    assert summary1["done"] == 1
    assert summary1["failed"] == 1

    # Round 2: re-load state, fix MSFT, re-run
    state2 = init_or_load_state(
        path=sp, tier="test_subset",
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
    )
    fail_msft[0] = False
    fake_dl_round2 = _FakeDownloader()
    orch2 = HistoricalOrchestrator(
        downloader=fake_dl_round2,  # type: ignore[arg-type]
        base_output_dir=output_dir,
        contracts_for_ticker=_contracts_round1,
        snapshot_for=_snapshot,
        concurrency=1,
    )
    outcomes2 = await orch2.run(state=state2, state_path=sp)
    # Only the failed task should have been retried
    assert len(outcomes2) == 1
    assert outcomes2[0].ticker == "MSFT"
    assert outcomes2[0].error is None
    # AAPL was not re-fetched
    assert len(fake_dl_round2.calls) == 1
    # Final state: both done
    final = load_state(sp)
    assert final is not None
    assert final.progress_summary()["done"] == 2
    assert final.progress_summary()["failed"] == 0


# Ensure the module is importable when pytest collects this file
_ = pq
