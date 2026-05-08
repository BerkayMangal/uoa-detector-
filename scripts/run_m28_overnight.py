#!/usr/bin/env python
"""M28 next-day OI confirmation — nightly batch driver.

Phase 3.4.8.3 CLI entry point. Reads a list of run_id values from
the BacktestStore, drives ``M28Validator`` to validate each,
prints per-run ValidationStats, and exits with status 0 on success
(any per-signal errors are reported but don't fail the process).

USAGE
=====

  # Validate one specific run:
  uv run python scripts/run_m28_overnight.py \\
      --run-id <run-id> \\
      --store-url sqlite:///./backtest.db

  # Validate all runs from prior session day (default
  # behaviour at scheduled batch time):
  uv run python scripts/run_m28_overnight.py \\
      --since-yesterday \\
      --store-url sqlite:///./backtest.db

  # Dry run — list runs that WOULD be validated, no provider calls:
  uv run python scripts/run_m28_overnight.py \\
      --since-yesterday --dry-run

OPERATOR-FACING FEATURES
========================

  --run-id ID         Validate one specific run.
  --since-yesterday   Validate all runs whose finished_at is
                      after (now - 1 day). Default scheduling target.
  --since DATETIME    Validate all runs since the given ISO timestamp.
  --dry-run           Enumerate runs without calling the provider.
  --store-url URL     SQLite URL for the backtest store. Required
                      unless --in-memory passed (testing only).
  --in-memory         Use BacktestStore (testing only — no signals
                      will exist unless added programmatically).
  --max-runs N        Cap to N runs (sandbox sanity check).
  --json-output       Print stats as JSON instead of formatted text
                      (machine-readable for scheduler integration).

ENV VAR
=======

  UNUSUAL_WHALES_API_KEY  Required for live validation. Without this
                          the script can only run in --dry-run mode.

EXIT CODES
==========

  0 — All requested runs processed (per-signal errors counted but
      do not fail the process; the operator inspects stats).
  2 — Configuration error (no UW key when validation requested,
      invalid arguments, store unreachable).
  3 — No runs matched the filter (operator may want to know).

SCHEDULER INTEGRATION
=====================

The default Phase 3.4.8 batch_run_time_et is 09:31 ET — one minute
after market open ensures CBOE has published the prior settle OI.
A typical cron entry:

  31 9 * * 1-5  cd /path/to/uoa_detector && \\
                .venv/bin/python scripts/run_m28_overnight.py \\
                  --since-yesterday \\
                  --store-url sqlite:///./backtest.db \\
                  --json-output \\
                  >> /var/log/uoa_m28.log 2>&1

The 09:31 default lives in profile.scoring.modules.m28.batch_run_time_et;
this script reads it for telemetry but doesn't enforce timing
(scheduling is the operator's responsibility).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import SecretStr

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.pipeline.validators import M28Validator, ValidationStats
from uoa_detector.providers.open_interest import (
    NoOpOpenInterestProvider,
    OpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)

if TYPE_CHECKING:
    from uoa_detector.backtest.protocol import BacktestStoreProtocol


_logger = logging.getLogger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="M28 next-day OI confirmation batch driver.",
    )
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--run-id",
        type=str,
        help="Validate exactly this run_id.",
    )
    selector.add_argument(
        "--since-yesterday",
        action="store_true",
        help="Validate all runs whose finished_at is after now - 1 day.",
    )
    selector.add_argument(
        "--since",
        type=str,
        help="Validate runs since this ISO datetime (UTC).",
    )

    storage = p.add_mutually_exclusive_group(required=True)
    storage.add_argument(
        "--store-url",
        type=str,
        help="SQLite URL: 'sqlite:///./backtest.db'.",
    )
    storage.add_argument(
        "--in-memory",
        action="store_true",
        help="Use empty in-memory store (testing only).",
    )

    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate runs without calling the provider.",
    )
    p.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Cap to N runs (sandbox).",
    )
    p.add_argument(
        "--json-output",
        action="store_true",
        help="Print stats as JSON.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def _select_runs(
    store: BacktestStoreProtocol,
    args: argparse.Namespace,
) -> list[str]:
    """Pick run_ids to validate per CLI flags."""
    if args.run_id:
        return [args.run_id]

    runs = store.list_runs()
    if args.since_yesterday:
        cutoff = datetime.now(UTC) - timedelta(days=1)
    elif args.since:
        cutoff = datetime.fromisoformat(args.since)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
    else:  # pragma: no cover — argparse enforces
        msg = "no run selector specified (argparse should have caught)"
        raise RuntimeError(msg)

    selected = [
        r.run_id
        for r in runs
        if r.finished_at is not None and r.finished_at >= cutoff
    ]
    if args.max_runs is not None:
        selected = selected[: args.max_runs]
    return selected


def _build_provider(
    *,
    dry_run: bool,
) -> tuple[OpenInterestProvider, UnusualWhalesClient | None]:
    """Construct OpenInterestProvider for live validation, or NoOp on
    dry-run.

    Returns (provider, client). The client may be None when dry-run
    or NoOp; it should be closed after validation if non-None.
    """
    if dry_run:
        return NoOpOpenInterestProvider(), None

    api_key = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not api_key:
        msg = (
            "UNUSUAL_WHALES_API_KEY not set; cannot fetch T+1 OI. "
            "Use --dry-run to enumerate runs without validation."
        )
        raise RuntimeError(msg)

    settings = UnusualWhalesSettings()
    client = UnusualWhalesClient(
        api_key=SecretStr(api_key), settings=settings,
    )
    provider = UnusualWhalesOpenInterestProvider(
        client=client, settings=settings,
    )
    return provider, client


def _format_stats(stats: ValidationStats) -> str:
    return (
        f"run_id={stats.run_id} "
        f"total={stats.total_signals} "
        f"validated={stats.validated_count} "
        f"(confirmed={stats.confirmed} "
        f"ambiguous={stats.ambiguous} "
        f"closing={stats.closing}) "
        f"skipped_no_m27={stats.skipped_no_m27_score} "
        f"skipped_below_thr={stats.skipped_below_threshold} "
        f"skipped_expired={stats.skipped_contract_expired} "
        f"pending={stats.pending_no_data} "
        f"errors={stats.errors}"
    )


def _stats_as_dict(stats: ValidationStats) -> dict[str, object]:
    """ValidationStats → JSON-friendly dict (dataclasses.asdict)."""
    return asdict(stats)


async def _main_async(args: argparse.Namespace) -> int:
    profile = load_default_profile()
    _logger.info(
        "M28 batch starting; configured batch_run_time_et=%s",
        profile.scoring.modules.m28.batch_run_time_et,
    )

    # Build store
    store: BacktestStoreProtocol
    if args.in_memory:
        store = BacktestStore()
        _logger.warning(
            "--in-memory selected: store has no persisted signals.",
        )
    else:
        store = SqliteBacktestStore(database_url=args.store_url)

    # Pick runs
    run_ids = _select_runs(store, args)
    if not run_ids:
        _logger.info("No runs matched the filter; nothing to do.")
        if hasattr(store, "close"):
            store.close()
        return 3

    _logger.info("Selected %d run(s) for validation.", len(run_ids))

    # Build provider
    try:
        provider, client = _build_provider(dry_run=args.dry_run)
    except RuntimeError as exc:
        _logger.error("%s", exc)
        store.close()
        return 2

    all_stats: list[ValidationStats] = []
    try:
        if args.dry_run:
            for rid in run_ids:
                signal_count = sum(1 for _ in store.iter_records(rid))
                _logger.info(
                    "DRY-RUN run_id=%s would_validate=%d signals",
                    rid, signal_count,
                )
                all_stats.append(ValidationStats(
                    run_id=rid, total_signals=signal_count,
                ))
        else:
            validator = M28Validator(
                provider=provider, store=store, profile=profile,
            )
            for rid in run_ids:
                stats = await validator.validate_run(rid)
                _logger.info("%s", _format_stats(stats))
                all_stats.append(stats)
    finally:
        if client is not None:
            await client.aclose()
        store.close()

    # Output
    if args.json_output:
        print(
            json.dumps(
                [_stats_as_dict(s) for s in all_stats],
                default=str, indent=2,
            ),
        )
    else:
        for s in all_stats:
            print(_format_stats(s))

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


# Re-export so tests can import the helpers
__all__ = [
    "_build_arg_parser",
    "_build_provider",
    "_format_stats",
    "_main_async",
    "_select_runs",
    "_stats_as_dict",
    "main",
]


# Suppress unused-import warning for dataclasses when no asdict path executed
_ = dataclasses
