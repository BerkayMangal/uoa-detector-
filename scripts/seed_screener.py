"""Seed the screener DB with real detector signals (Phase 4).

Runs the replay pipeline (self-derived ThetaData fusion: GEX + IV + OI +
price + catalyst) over a short slice into a SQLite store, so the dashboard
has real signals to show before the live detector is wired. The same code
path the live detector uses — only the store + slice differ.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/seed_screener.py \\
        --out webapp/seed.db --from 2025-07-07 --to 2025-07-09
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from uoa_detector.backtest.cell_runner import _replay_stages
    from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
    from uoa_detector.calibration import load_profile
    from uoa_detector.historical.universe import read_universe, tickers_only
    from uoa_detector.pipeline.orchestrator import Pipeline
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.median_trade_size import (
        CSVMedianTradeSizeProvider,
    )
    from uoa_detector.sources.parquet_replay import ParquetReplaySource

    p = argparse.ArgumentParser(prog="seed_screener")
    p.add_argument("--out", type=Path, default=Path("webapp/seed.db"))
    p.add_argument("--from", dest="start", default="2025-07-07")
    p.add_argument("--to", dest="end", default="2025-07-09")
    p.add_argument("--profile", type=Path, default=Path("profiles/v5_default.yaml"))
    p.add_argument("--min-premium", type=float, default=100000.0)
    args = p.parse_args(argv)

    if args.out.exists():
        args.out.unlink()
    profile = load_profile(args.profile)
    tier1 = tickers_only(read_universe(Path("data/universes/tier1_reduced.csv")))

    source = ParquetReplaySource(
        "thetadata", Path("data/historical/bulk"),
        tickers=list(tier1),
        from_month=args.start[:7], to_month=args.end[:7],
        min_premium_usd=Decimal(str(args.min_premium)),
    )
    context = PipelineContext(
        profile=profile,
        median_trade_size_provider=CSVMedianTradeSizeProvider(
            Path("data/medians_bulk.csv"),
        ),
    )
    stages = _replay_stages(
        "fusion",
        chain_snapshots_dir=Path("data/chain_snapshots"),
        spot_series_dir=Path("data/spot_series"),
        catalyst_csv=Path("data/earnings_calendar.csv"),
    )
    store = SqliteBacktestStore(database_url=f"sqlite:///{args.out}")
    store.start_run(
        profile=profile, universe_id="screener_seed", run_id="seed",
        dataset_window_start=datetime.fromisoformat(args.start).replace(tzinfo=UTC),
        dataset_window_end=datetime.fromisoformat(args.end).replace(tzinfo=UTC),
    )
    pipeline = Pipeline(
        sources=[source], stages=stages, profile=profile,
        store=store, context=context,
    )
    asyncio.run(pipeline.run())
    store.close()
    print(f"seeded {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
