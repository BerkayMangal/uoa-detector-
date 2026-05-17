#!/usr/bin/env python
"""Tier-N historical bulk download driver.

Phase 3.3.4.5 CLI entry point. Reads a tier universe CSV, drives
``HistoricalOrchestrator`` to fetch OPRA trades + quotes via
ThetaData, writes one parquet per (ticker, contract, month), then
generates a ``.manifest.json`` summary.

USAGE
=====

  uv run python scripts/download_tier2.py \\
      --tier tier2_starter \\
      --start-date 2024-01-01 --end-date 2024-06-30 \\
      --output-dir data/historical/thetadata \\
      --concurrency 4

OPERATOR-FACING FEATURES
========================

  --dry-run            Enumerate tasks; no HTTP calls. Prints the
                       task table and would-be disk impact.
  --validate-only      Skip download; rebuild manifest from disk.
  --max-tasks N        Cap to N tasks (sandbox sanity check).
  --max-contracts N    Cap contracts per ticker (sandbox).
  --skip-disk-check    Skip the pre-flight free-space estimate.
  --tier-csv PATH      Override the default tier CSV location.

CRITICAL
========

  Production runs against the real ThetaData Pro endpoint cost
  bandwidth + quota. Always start with --dry-run, then a small
  --max-tasks subset, before kicking off the full run.

  The agent that wrote this script NEVER auto-runs production
  downloads. Berkay must invoke it manually with the appropriate
  flags after reviewing the dry-run output.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from uoa_detector.calibration import load_default_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.historical.contracts import (
    ContractListFilter,
    ThetaDataContractLister,
)
from uoa_detector.historical.orchestrator import (
    HistoricalOrchestrator,
    expand_universe_to_tasks,
)
from uoa_detector.historical.state import (
    init_or_load_state,
    save_state,
    state_path,
)
from uoa_detector.historical.universe import read_universe, tickers_only
from uoa_detector.historical.validation import (
    build_manifest,
    manifest_path,
    save_manifest,
)
from uoa_detector.sources._http_base import RetryPolicy
from uoa_detector.sources.thetadata.client import ThetaDataClient
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
    ThetaDataHistoricalDownloader,
)

_logger = logging.getLogger("download_tier2")


# ---------------------------------------------------------------------------
# Disk-space pre-check
# ---------------------------------------------------------------------------


_DEFAULT_PER_MONTH_BYTES_ESTIMATE = 50 * 1024 * 1024  # 50 MB / ticker / month


def estimate_disk_bytes(
    *,
    n_tickers: int, n_months: int,
    per_month_bytes: int = _DEFAULT_PER_MONTH_BYTES_ESTIMATE,
    safety_margin: float = 1.5,
) -> int:
    """Estimate total disk needed: tickers × months × per_month × margin."""
    return int(n_tickers * n_months * per_month_bytes * safety_margin)


def check_disk_space(
    *,
    output_dir: Path, estimated_bytes: int,
) -> tuple[bool, str]:
    """Return (ok, message). ok=False if free < estimated_bytes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    free_bytes = shutil.disk_usage(output_dir).free
    if free_bytes < estimated_bytes:
        return False, (
            f"INSUFFICIENT DISK SPACE\n"
            f"  estimated need: {_human_bytes(estimated_bytes)}\n"
            f"  free at {output_dir}: {_human_bytes(free_bytes)}\n"
            f"  shortfall: {_human_bytes(estimated_bytes - free_bytes)}"
        )
    return True, (
        f"disk OK: {_human_bytes(free_bytes)} free, "
        f"estimated {_human_bytes(estimated_bytes)} needed"
    )


def _human_bytes(b: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(b)
    for u in units:
        if f < 1024.0:
            return f"{f:.1f} {u}"
        f /= 1024.0
    return f"{f:.1f} PB"


# ---------------------------------------------------------------------------
# Snapshot resolver — production wiring
# ---------------------------------------------------------------------------


async def _default_snapshot_for(
    _client: ThetaDataClient, contract: ContractSpec, day: date,
) -> ContextSnapshot:
    """Fallback snapshot: zero spot + None OI/IV.

    Phase 3.3.4.5 ships this as a safe default — the per-contract
    snapshot endpoint is wired in Phase 3.4. Operators running the
    full historical fetch should plug in a real snapshot provider
    (e.g., a stock-quote-fetch wrapper) via the orchestrator
    constructor.
    """
    del _client, contract, day
    from decimal import Decimal
    return ContextSnapshot(
        spot_price=Decimal("0"),
        open_interest=None,
        implied_volatility=None,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="download_tier2",
        description="Tier-N historical bulk download driver.",
    )
    p.add_argument(
        "--tier", default="tier2_starter",
        help="Tier name (controls default CSV and state.tier).",
    )
    p.add_argument(
        "--tier-csv", type=Path, default=None,
        help="Override CSV path. Default: data/universes/{tier}.csv",
    )
    p.add_argument(
        "--start-date", type=_iso_date, required=True,
        help="Inclusive start date YYYY-MM-DD.",
    )
    p.add_argument(
        "--end-date", type=_iso_date, required=True,
        help="Inclusive end date YYYY-MM-DD.",
    )
    p.add_argument(
        "--output-dir", type=Path,
        default=Path("data/historical/thetadata"),
        help="Base output directory for parquet files.",
    )
    p.add_argument(
        "--state-dir", type=Path, default=Path("data/historical"),
        help="Directory holding .download_state.json and .manifest.json.",
    )
    p.add_argument(
        "--concurrency", type=int, default=4,
        help="Max simultaneous (ticker, month) tasks. Pro plan = 4.",
    )
    p.add_argument(
        "--max-tasks", type=int, default=None,
        help="Cap to N tasks (sandbox sanity check).",
    )
    p.add_argument(
        "--max-contracts", type=int, default=None,
        help="Cap contracts per ticker (sandbox sanity check).",
    )
    p.add_argument(
        "--max-dte", type=int, default=None,
        help=(
            "Skip contracts whose expiry is further than MAX_DTE days "
            "from the download date. Track B's strategy profile "
            "penalises 60+ DTE to zero (v5_gamma_squeeze leap_threshold=60); "
            "passing --max-dte 60 cuts the download universe in half "
            "without losing any contract Track B would actually trade."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="List tasks; do not call any HTTP endpoints.",
    )
    p.add_argument(
        "--validate-only", action="store_true",
        help="Skip download; rebuild manifest from on-disk parquet files.",
    )
    p.add_argument(
        "--skip-disk-check", action="store_true",
        help="Skip the pre-flight free-space check.",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p.parse_args(argv)


def _iso_date(s: str) -> date:
    return date.fromisoformat(s)


def _resolve_tier_csv(args: argparse.Namespace) -> Path:
    if args.tier_csv is not None:
        return Path(args.tier_csv)
    return Path("data/universes") / f"{args.tier}.csv"


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 1. Read universe
    csv_path = _resolve_tier_csv(args)
    if not csv_path.exists():
        _logger.error("universe CSV not found: %s", csv_path)
        return 2
    entries = read_universe(csv_path)
    tickers = list(tickers_only(entries))
    if not tickers:
        _logger.error("no valid tickers in %s", csv_path)
        return 2
    _logger.info("universe loaded: %d ticker(s) from %s", len(tickers), csv_path)

    # 2. Expand tasks
    all_tasks = expand_universe_to_tasks(
        tickers=tickers,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    if args.max_tasks is not None:
        all_tasks = all_tasks[: args.max_tasks]
        _logger.info("--max-tasks %d cap applied; %d task(s)",
                     args.max_tasks, len(all_tasks))

    # 3. Initialise / resume state
    sp = state_path(args.state_dir)
    state = init_or_load_state(
        path=sp, tier=args.tier,
        start_date=args.start_date, end_date=args.end_date,
    )
    state.initialize_tasks(all_tasks)
    save_state(state, sp)
    summary_before = state.progress_summary()
    _logger.info("state loaded: %s", summary_before)

    # 4. Validate-only short-circuit
    if args.validate_only:
        _logger.info("--validate-only: skipping download, building manifest")
        failed_keys = {
            key for key, t in state.tasks.items() if t.status == "failed"
        }
        manifest = build_manifest(
            base_output_dir=args.output_dir,
            tickers=tickers,
            start_date=args.start_date,
            end_date=args.end_date,
            tier=args.tier,
            failed_keys=failed_keys,
        )
        mp = manifest_path(args.state_dir)
        save_manifest(manifest, mp)
        _logger.info("manifest saved to %s", mp)
        _print_manifest_summary(manifest)
        return 0

    # 5. Disk-space pre-check
    if not args.skip_disk_check:
        n_months = len({(t[1], t[2]) for t in all_tasks})
        n_tickers = len({t[0] for t in all_tasks})
        est = estimate_disk_bytes(n_tickers=n_tickers, n_months=n_months)
        ok, msg = check_disk_space(
            output_dir=args.output_dir, estimated_bytes=est,
        )
        if not ok:
            _logger.error("%s", msg)
            _logger.error("re-run with --skip-disk-check to proceed anyway")
            return 3
        _logger.info("%s", msg)

    # 6. Dry-run short-circuit
    if args.dry_run:
        _print_dry_run(state, args)
        return 0

    # 7. Production: load credentials + wire client + orchestrator
    creds = Credentials()
    api_key = creds.require_thetadata_api_key()
    profile = load_default_profile()
    client = ThetaDataClient(
        api_key=api_key,
        settings=profile.data_sources.thetadata,
        # Phase 3.5.3.4: the circuit breaker is a live-trading safety
        # device (Phase 3.3.2) — wrong tool for a multi-day batch
        # download. A handful of transient HTTP 500s (Terminal
        # hiccups under concurrency) would trip the breaker and
        # cascade-fail every remaining ticker-month with
        # CircuitBreakerOpenError. Transient errors are already
        # handled per-request by RetryPolicy. Set the breaker
        # threshold effectively infinite for the download path so
        # one bad contract never kills the whole run.
        circuit_breaker_threshold=10**9,
        # Phase 3.5.3.5: 5 attempts (was default 3) — transient
        # ThetaData HTTP 5xx under sustained concurrency clears on
        # retry the overwhelming majority of the time.
        retry=RetryPolicy(max_attempts=5),
    )
    lister = ThetaDataContractLister(client=client)
    # Phase 3.5.3.1: per-(ticker, month) contract resolution. Each
    # ticker-month task gets a freshly-anchored ContractListFilter
    # so the lister returns only contracts actually listed in that
    # month. Combined with the downloader's per-(contract, day) DTE
    # guard, this kills the 472 storm seen pre-3.5.3.1 (contracts
    # listed at end_date but not yet listed during the 18-month
    # backfill window).
    _contracts_cache: dict[tuple[str, int, int], list[ContractSpec]] = {}

    async def _contracts_for_ticker(
        ticker: str, asof: date,
    ) -> list[ContractSpec]:
        key = (ticker.upper(), asof.year, asof.month)
        cached = _contracts_cache.get(key)
        if cached is not None:
            return cached
        # Phase 3.5.3.8: ThetaData's contract-list endpoint returns
        # "no data" on non-trading days (weekends, market holidays).
        # The orchestrator anchors at the 15th of the month, which is
        # a weekend ~2/7 of the time — that yielded 0 contracts and
        # stamped the whole ticker-month "done" with zero rows. Walk
        # nearby days until one lands on a trading day with listings.
        contracts: list[ContractSpec] = []
        for delta in (0, 1, 2, 3, -1, -2, -3, 4, 5):
            cand = asof + timedelta(days=delta)
            f = ContractListFilter(
                max_contracts=args.max_contracts,
                max_dte=args.max_dte,
                as_of_date=cand,
            )
            contracts = await lister.list_contracts(ticker, filters=f)
            if contracts:
                break
        _contracts_cache[key] = contracts
        return contracts

    async def _snapshot_for(c: ContractSpec, d: date) -> ContextSnapshot:
        return await _default_snapshot_for(client, c, d)

    downloader = ThetaDataHistoricalDownloader(
        client=client,
        concurrency=args.concurrency,
        max_dte=args.max_dte,
    )
    orch = HistoricalOrchestrator(
        downloader=downloader,
        base_output_dir=args.output_dir,
        contracts_for_ticker=_contracts_for_ticker,
        snapshot_for=_snapshot_for,
        concurrency=args.concurrency,
    )

    try:
        outcomes = await orch.run(state=state, state_path=sp)
    finally:
        await client.aclose()

    summary_after = state.progress_summary()
    _logger.info("download complete: %s", summary_after)

    # 8. Manifest
    failed_keys = {
        key for key, t in state.tasks.items() if t.status == "failed"
    }
    manifest = build_manifest(
        base_output_dir=args.output_dir,
        tickers=tickers,
        start_date=args.start_date,
        end_date=args.end_date,
        tier=args.tier,
        failed_keys=failed_keys,
    )
    mp = manifest_path(args.state_dir)
    save_manifest(manifest, mp)
    _logger.info("manifest saved to %s", mp)
    _print_manifest_summary(manifest)

    # Exit code: 1 if any tasks failed, 0 otherwise.
    failed_count = sum(1 for o in outcomes if o.error is not None)
    return 1 if failed_count > 0 else 0


def _print_dry_run(state, args) -> None:  # type: ignore[no-untyped-def]
    pending = state.pending_tasks()
    print()
    print(f"DRY RUN — {len(pending)} pending task(s)")
    print(f"  tier:        {args.tier}")
    print(f"  date range:  {args.start_date} .. {args.end_date}")
    print(f"  output dir:  {args.output_dir}")
    print(f"  concurrency: {args.concurrency}")
    print()
    if pending:
        print("first 20 tasks:")
        for ticker, year, month in pending[:20]:
            print(f"  {ticker:<6}  {year:04d}-{month:02d}")
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
    print()


def _print_manifest_summary(manifest) -> None:  # type: ignore[no-untyped-def]
    s = manifest.summary
    print()
    print("MANIFEST SUMMARY")
    print(f"  tickers:         {s.total_tickers}")
    print(f"  months in range: {s.total_months}")
    print(f"  tasks done:      {s.total_tasks_done}")
    print(f"  tasks failed:    {s.total_tasks_failed}")
    print(f"  total rows:      {s.total_rows:,}")
    print(f"  total files:     {s.total_files:,}")
    print(f"  total size:      {_human_bytes(s.total_bytes)}")
    print()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
