"""CLI entry point for the UOA + Convexity Detector.

Usage examples:
    # Default smoke run — pretty output to stdout, structlog logs to stderr.
    python -m uoa_detector run

    # NDJSON output to stdout (one record per print, jq/pandas-friendly).
    python -m uoa_detector run --output json > decisions.jsonl

    # Parquet output (requires --output-file).
    python -m uoa_detector run --output parquet --output-file decisions.parquet

    # Multi-source synthetic scenario — exercises SourceFusion across 3 sources.
    python -m uoa_detector run --multi-source

    # Custom profile (e.g., partial override of v5_default).
    python -m uoa_detector run --profile profiles/example_ticker_override.yaml

Output routing
--------------
Structured logs (one per event, ``event=signal`` etc.) go to stderr by
default so they don't contaminate ``--output json`` stdout streams. The
record stream goes to:
  - stdout (if no ``--output-file``) for ``json`` and ``pretty``.
  - the file path for ``parquet`` (mandatory) or any other mode when
    ``--output-file`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import typer

from uoa_detector.backtest import (
    BacktestMetrics,
    BacktestStore,
    NoOpPnLProvider,
    SqliteBacktestStore,
    compute_metrics,
    noop_trade_producer,
    render_4cell_comparison_report,
    run_4cell_backtest,
)
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.loader import load_profile
from uoa_detector.observability import (
    NDJSONWriter,
    ParquetWriter,
    PrettyWriter,
    redact_secrets,
)
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.multi_source_scenario import multi_source_scenario
from uoa_detector.sources.parquet_replay import ParquetReplaySource
from uoa_detector.sources.scenarios import (
    ScenarioOverrideStage,
    default_scenario,
    overrides_lookup,
)
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

if TYPE_CHECKING:
    from collections.abc import Callable

    from uoa_detector.backtest import BacktestStoreProtocol
    from uoa_detector.backtest.cell_runner import CellSpec
    from uoa_detector.backtest.pnl_provider import RealizedTrade
    from uoa_detector.backtest.walk_forward import WalkForwardWindow
    from uoa_detector.calibration import CalibrationProfile
    from uoa_detector.observability import DecisionRecordWriter

    _TradeProducer = Callable[
        [CellSpec, tuple[WalkForwardWindow, ...], CalibrationProfile],
        list[RealizedTrade],
    ]


class OutputFormat(StrEnum):
    """Supported decision-record output formats."""

    PRETTY = "pretty"
    JSON = "json"
    PARQUET = "parquet"


app = typer.Typer(help="UOA + Convexity Detector v5 CLI", add_completion=False)
backtest_app = typer.Typer(
    help="Backtest reporting and tooling (Phase 3.2.3+).",
    add_completion=False,
)
app.add_typer(backtest_app, name="backtest")


@app.callback()
def _root() -> None:
    """Force typer into multi-command mode so ``run`` is an explicit subcommand."""


@backtest_app.callback()
def _backtest_root() -> None:
    """Force typer into multi-command mode for the backtest subcommands."""


def _configure_logging(*, log_to_stderr: bool) -> None:
    """Wire up structlog. ``log_to_stderr=True`` keeps stdout clean for NDJSON.

    The level comes from ``AppSettings.log_level`` (env ``UOA_LOG_LEVEL``,
    default ``INFO``). ``make_filtering_bound_logger`` drops below-level
    events *before* the processor chain runs, so a quiet level
    (``UOA_LOG_LEVEL=WARNING``) makes a large backtest fast — the
    per-signal INFO logs are never rendered. Pre-3.5 this level was
    hardcoded to INFO and ``log_level`` was dead config.

    Phase 3.3.1.2: ``redact_secrets`` runs as the FIRST processor so
    no downstream processor (timestamper, renderer, etc.) sees the
    secret values. If a future processor decided to copy event_dict
    to disk for debugging, that copy is already redacted.
    """
    from uoa_detector.config import default_settings

    level = logging.getLevelName(default_settings().log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr if log_to_stderr else sys.stdout,
        level=level,
        force=True,  # override any prior basicConfig in tests
    )
    structlog.configure(
        processors=[
            redact_secrets,  # MUST be first — defense in depth
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.KeyValueRenderer(
                key_order=["event", "ticker", "label", "combined_score", "max_r"],
                drop_missing=True,
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def _build_writer(
    output: OutputFormat,
    output_file: Path | None,
) -> tuple[DecisionRecordWriter, bool]:
    """Build the configured writer plus a ``log_to_stderr`` hint.

    Returns ``(writer, log_to_stderr)`` where ``log_to_stderr=True`` means
    the structlog stream should target stderr (because the record stream
    is going to stdout).
    """
    if output == OutputFormat.PARQUET:
        if output_file is None:
            msg = (
                "--output parquet requires --output-file (parquet is a binary "
                "format and cannot stream to stdout)"
            )
            raise typer.BadParameter(msg, param_hint="--output-file")
        return ParquetWriter(output_file), False  # logs can stay on stdout

    if output == OutputFormat.JSON:
        if output_file is not None:
            handle = output_file.open("w", encoding="utf-8")
            return NDJSONWriter(handle), False  # logs to stdout, NDJSON to file
        return NDJSONWriter(sys.stdout), True  # logs to stderr to keep stdout pure

    # OutputFormat.PRETTY
    if output_file is not None:
        handle = output_file.open("w", encoding="utf-8")
        return PrettyWriter(handle), False
    return PrettyWriter(sys.stdout), True


def _resolve_profile(profile_path: Path | None) -> CalibrationProfile:
    """Load the profile pinned by ``--profile`` or fall back to v5_default."""
    if profile_path is None:
        return load_default_profile()
    if not profile_path.exists():
        msg = f"profile file not found: {profile_path}"
        raise typer.BadParameter(msg, param_hint="--profile")
    return load_profile(profile_path)


@app.command()
def run(
    source: str = typer.Option(
        "synthetic",
        "--source",
        help=(
            "Flow source. 'synthetic' (default) drives the in-memory scenario; "
            "'historical' replays parquet files under --data-dir through the "
            "RawFlowSource Protocol — Phase 3.2.2; "
            "'live' connects ThetaData and/or Unusual Whales WebSocket feeds "
            "and runs the full pipeline against real market data — Phase 3.3.5."
        ),
    ),
    scenario: str = typer.Option("default", help="Synthetic scenario name."),
    multi_source: bool = typer.Option(
        False,
        "--multi-source",
        help="Run the multi-source synthetic scenario through SourceFusion.",
    ),
    output: OutputFormat = typer.Option(
        OutputFormat.PRETTY,
        "--output",
        help="Decision record output format.",
        case_sensitive=False,
    ),
    output_file: Path | None = typer.Option(
        None,
        "--output-file",
        help="Write decision records to this file (required for parquet).",
    ),
    profile_path: Path | None = typer.Option(
        None,
        "--profile",
        help="Path to a CalibrationProfile YAML; defaults to v5_default.",
    ),
    store_url: str = typer.Option(
        ":memory:",
        "--store",
        help=(
            "Backtest store URL. ':memory:' (default) uses the in-memory "
            "BacktestStore; 'sqlite:path/to/db' uses the persistent SQLite "
            "store. Phase 3.2.1."
        ),
    ),
    data_dir: Path | None = typer.Option(
        None,
        "--data-dir",
        help=(
            "Historical replay only: per-source parquet root, e.g., "
            "data/historical/synthetic/. Required when --source=historical. "
            "Phase 3.2.2."
        ),
    ),
    tickers: str | None = typer.Option(
        None,
        "--tickers",
        help=(
            "Historical replay only: comma-separated ticker filter. Default: "
            "all tickers found under --data-dir."
        ),
    ),
    from_month: str | None = typer.Option(
        None,
        "--from",
        help="Historical replay only: earliest month (YYYY-MM). Inclusive.",
    ),
    to_month: str | None = typer.Option(
        None,
        "--to",
        help="Historical replay only: latest month (YYYY-MM). Inclusive.",
    ),
    replay_speed: float = typer.Option(
        float("inf"),
        "--replay-speed",
        help=(
            "Historical replay only: pacing multiplier. inf (default) = batch "
            "as fast as possible; 1.0 = real-time; 10.0 = 10x real-time. "
            "Replay correctness is independent of this — fusion uses event-"
            "time watermarks (Phase 2.3.3)."
        ),
    ),
    source_id: str = typer.Option(
        "historical",
        "--source-id",
        help=(
            "Historical replay only: source_id tag for the harness. Defaults "
            "to 'historical'. Use this to label the source dimension in "
            "SQLite signals and 4-cell runs."
        ),
    ),
    feeds: str = typer.Option(
        "thetadata,unusual_whales",
        "--feeds",
        help=(
            "Live mode only: comma-separated list of feeds to connect. "
            "Supported: 'thetadata', 'unusual_whales'. Each requested feed "
            "needs its credential set in env (THETADATA_API_KEY / "
            "UNUSUAL_WHALES_API_KEY); fail-fast if missing."
        ),
    ),
    live_tickers: str | None = typer.Option(
        None,
        "--live-tickers",
        help=(
            "Live mode only: comma-separated tickers to subscribe. "
            "Required when --source=live. Example: 'AAPL,MSFT,SPY'."
        ),
    ),
) -> None:
    """Run the pipeline against a flow source and emit labeled signal records."""
    if source not in ("synthetic", "historical", "live"):
        msg = (
            f"--source must be 'synthetic', 'historical', or 'live'; "
            f"got {source!r}"
        )
        raise typer.BadParameter(msg, param_hint="--source")
    if source == "synthetic" and scenario != "default":
        msg = (
            f"Only the 'default' scenario name is supported for synthetic; "
            f"got {scenario!r}"
        )
        raise typer.BadParameter(msg, param_hint="--scenario")
    if source == "historical" and data_dir is None:
        msg = "--data-dir is required when --source=historical"
        raise typer.BadParameter(msg, param_hint="--data-dir")
    if source == "live" and not live_tickers:
        msg = "--live-tickers is required when --source=live"
        raise typer.BadParameter(msg, param_hint="--live-tickers")

    writer, log_to_stderr = _build_writer(output, output_file)
    _configure_logging(log_to_stderr=log_to_stderr)
    profile = _resolve_profile(profile_path)
    store = _build_store(store_url)

    # CLI owns the store's lifecycle. Pipeline.run() finalises the run
    # it drove (calls finish_run()) but does NOT close the store —
    # closing is the responsibility of whoever owns the store. Phase
    # 3.2.4's 4-cell runner will own a store across four pipelines.
    try:
        if source == "historical":
            assert data_dir is not None  # typer guarantees by check above
            ticker_list = (
                [t.strip().upper() for t in tickers.split(",") if t.strip()]
                if tickers
                else None
            )
            asyncio.run(
                _run_historical(
                    profile=profile,
                    writer=writer,
                    store=store,
                    data_dir=data_dir,
                    tickers=ticker_list,
                    from_month=from_month,
                    to_month=to_month,
                    replay_speed=replay_speed,
                    source_id=source_id,
                ),
            )
        elif source == "live":
            assert live_tickers is not None  # typer guarantees by check above
            ticker_list = [
                t.strip().upper()
                for t in live_tickers.split(",") if t.strip()
            ]
            # Clean ctrl-C exit; LiveObserver already drained.
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(
                    _run_live(
                        profile=profile,
                        writer=writer,
                        store=store,
                        feeds_arg=feeds,
                        tickers=ticker_list,
                    ),
                )
        elif multi_source:
            asyncio.run(_run_multi_source(profile, writer, store))
        else:
            asyncio.run(_run_default_synthetic(profile, writer, store))
    finally:
        store.close()


def _build_store(store_url: str) -> BacktestStoreProtocol:
    """Resolve ``--store`` flag to a BacktestStoreProtocol implementation.

    Accepts:
      - ``:memory:`` (default) → in-memory ``BacktestStore``
      - ``sqlite:<path>`` → persistent ``SqliteBacktestStore``; the path
        can be relative (``sqlite:backtest.db``) or absolute
        (``sqlite:/abs/path.db``).

    SQLAlchemy SQLite URL convention:
      - ``sqlite:///relative/path.db``  (3 slashes, then relative path)
      - ``sqlite:////absolute/path.db`` (4 slashes, then absolute path)
    The CLI accepts the ergonomic shortcut ``sqlite:foo.db`` /
    ``sqlite:/abs/foo.db`` and rewrites to the canonical form.
    """
    if store_url == ":memory:":
        return BacktestStore()
    if store_url.startswith("sqlite:"):
        # Already-canonical forms pass through.
        if store_url.startswith(("sqlite:///", "sqlite:////")):
            return SqliteBacktestStore(store_url)
        tail = store_url[len("sqlite:") :]
        # SQLAlchemy expects ``sqlite:///<path>``: 3 slashes for the URL
        # delimiter, then the path. If ``tail`` already starts with ``/``
        # (absolute path), the result naturally has 4 slashes; if not
        # (relative), 3. Same expression handles both cases.
        url = f"sqlite:///{tail}"
        return SqliteBacktestStore(url)
    msg = (
        f"Unrecognized --store value {store_url!r}. "
        "Use ':memory:' or 'sqlite:path/to/db'."
    )
    raise typer.BadParameter(msg, param_hint="--store")


async def _run_default_synthetic(
    profile: CalibrationProfile,
    writer: DecisionRecordWriter,
    store: BacktestStoreProtocol,
) -> None:
    """Drive the default single-source scenario."""
    steps = default_scenario()
    raw_prints = [to_raw_print(s.print_, source_id="synthetic") for s in steps]
    src = SyntheticRawFlowSource("synthetic", raw_prints)

    stages = [
        ScenarioOverrideStage(overrides_lookup(iter(steps))),
        *default_stage_pipeline(),
    ]
    pipeline = Pipeline(
        [src],
        stages,
        profile=profile,
        store=store,
        decision_record_writer=writer,
    )
    await pipeline.run()


async def _run_multi_source(
    profile: CalibrationProfile,
    writer: DecisionRecordWriter,
    store: BacktestStoreProtocol,
) -> None:
    """Drive the 3-source synthetic scenario through SourceFusion.

    Demonstrates ``confidence_tier`` variation across the four buckets
    (unanimous, majority, single, conflicted). No ``ScenarioOverrideStage``
    here — the multi-source scenario authors RawPrints directly with the
    right shape for fusion to classify, and downstream stages compute
    sub-scores from the actual fused OptionsPrints (M37 falls back to
    NoOpMedianProvider → relative_premium_score=None; M38 sees an empty
    cluster buffer for each new strike → density_isolated; etc).
    """
    a, b, c = multi_source_scenario()
    pipeline = Pipeline(
        [a, b, c],
        list(default_stage_pipeline()),
        profile=profile,
        store=store,
        decision_record_writer=writer,
    )
    await pipeline.run()


async def _run_historical(
    *,
    profile: CalibrationProfile,
    writer: DecisionRecordWriter,
    store: BacktestStoreProtocol,
    data_dir: Path,
    tickers: list[str] | None,
    from_month: str | None,
    to_month: str | None,
    replay_speed: float,
    source_id: str,
) -> None:
    """Drive the pipeline against a historical parquet replay source.

    Single ``ParquetReplaySource`` against the supplied ``data_dir``.
    Multi-source replay (Polygon + UW + IBKR) wires three harnesses
    by hand at the call site or by a Phase 3.2.4 4-cell runner — the
    CLI's role here is the single-source smoke path.
    """
    src = ParquetReplaySource(
        source_id,
        data_dir,
        tickers=tickers,
        from_month=from_month,
        to_month=to_month,
        replay_speed=replay_speed,
    )
    pipeline = Pipeline(
        [src],
        list(default_stage_pipeline()),
        profile=profile,
        store=store,
        decision_record_writer=writer,
    )
    await pipeline.run()


async def _run_live(
    *,
    profile: CalibrationProfile,
    writer: DecisionRecordWriter,
    store: BacktestStoreProtocol,
    feeds_arg: str,
    tickers: list[str],
) -> None:
    """Drive the live observer mode (Phase 3.3.5).

    Wires:
      - parse_feeds_arg(feeds_arg)         → tuple of feed names
      - Credentials (env / .env)           → API keys for each feed
      - build_live_sources(...)            → [RawFlowSource]
      - Pipeline(sources, default_stages)  → fused decision records
      - LiveObserver(pipeline, sources)    → SIGINT-graceful shutdown

    For Phase 3.3.5 the 'thetadata' feed is gated: it requires
    contract-level subscriptions + a snapshot resolver, neither of
    which is wired in 3.3.5 (Phase 4 work). Operators specifying
    --feeds containing 'thetadata' get a clear error directing
    them to use --feeds unusual_whales for now. The factory
    surface is in place; only the contract-enumeration glue is
    deferred.
    """
    from uoa_detector.config.credentials import Credentials
    from uoa_detector.live.factory import (
        FeedConfigurationError,
        build_live_sources,
        parse_feeds_arg,
    )
    from uoa_detector.live.observer import LiveObserver

    feeds = None
    try:
        feeds = parse_feeds_arg(feeds_arg)
    except FeedConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--feeds") from exc
    if "thetadata" in feeds:
        msg = (
            "Phase 3.3.5: --feeds thetadata is not yet wired through the "
            "CLI (per-contract subscription enumeration + snapshot "
            "resolver are Phase 4 work). Use --feeds unusual_whales "
            "or run two separate processes if you need both feeds. "
            "The factory surface (live/factory.py) supports both; "
            "only the CLI glue is deferred."
        )
        raise typer.BadParameter(msg, param_hint="--feeds")

    creds = Credentials()
    try:
        bundle = build_live_sources(
            feeds=feeds,
            credentials=creds,
            thetadata_settings=profile.data_sources.thetadata,
            unusual_whales_settings=profile.data_sources.unusual_whales,
            tickers=tickers,
        )
    except FeedConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--feeds") from exc

    pipeline = Pipeline(
        bundle.sources,
        list(default_stage_pipeline()),
        profile=profile,
        store=store,
        decision_record_writer=writer,
        force_multi_source=len(bundle.sources) > 1,
    )
    observer = LiveObserver(
        pipeline=pipeline,
        sources=bundle.sources,
        install_signal_handlers=True,
    )
    await observer.run()


# ---------------------------------------------------------------------------
# `backtest report` subcommand — Phase 3.2.3.5
# ---------------------------------------------------------------------------


@backtest_app.command("report")
def backtest_report(
    run_id: str = typer.Option(
        ...,
        "--run-id",
        help="The run_id to report on. Use 'implicit-default' for runs "
             "produced by the default CLI without explicit start_run.",
    ),
    store_url: str = typer.Option(
        ...,
        "--store",
        help="Backtest store URL: 'sqlite:path/to/db' for the persistent "
             "store, or ':memory:' (rare; the in-memory store empties at "
             "process exit so this only works inside a single-process "
             "test harness).",
    ),
    pnl: str = typer.Option(
        "noop",
        "--pnl",
        help="PnL provider for the report. 'noop' (default) treats every "
             "decision as an open trade — useful for confirming wiring and "
             "diagnosing whether decisions reached the store at all. "
             "'simple' (Phase 3.3+) needs an exit-quote source; not "
             "available in 3.2.3.",
    ),
    walk_forward_windows: int = typer.Option(
        8,
        "--walk-forward-windows",
        help="Number of equal-trade-count windows for walk-forward "
             "consistency. Default 8.",
    ),
) -> None:
    """Read signals for a run from the store, compute metrics, print
    a pass/fail table to stdout.

    Phase 3.2.3.5 scope: report rendering only. The default ``--pnl
    noop`` reports every decision as open and is intended to confirm
    that the run made it to the store. ``--pnl simple`` requires an
    exit-quote source from the replay stream (Phase 3.3).
    """
    if pnl == "simple":
        msg = (
            "--pnl simple requires an exit-quote source wired from the "
            "replay stream. Not available in Phase 3.2.3 — the first "
            "real-quote-driven backtest report lands in Phase 3.3."
        )
        raise typer.BadParameter(msg, param_hint="--pnl")
    if pnl != "noop":
        msg = f"--pnl must be 'noop' (or 'simple', not yet wired); got {pnl!r}"
        raise typer.BadParameter(msg, param_hint="--pnl")

    store = _build_store(store_url)
    try:
        signals = list(store.iter_records(run_id))
    finally:
        store.close()

    if not signals:
        typer.echo(f"No signals found for run_id={run_id!r} in {store_url!r}.")
        raise typer.Exit(code=1)

    provider = NoOpPnLProvider()
    trades = [provider.provide(s) for s in signals]
    metrics = compute_metrics(
        trades, walk_forward_windows=walk_forward_windows,
    )

    _render_metric_report(run_id=run_id, metrics=metrics)


def _render_metric_report(*, run_id: str, metrics: BacktestMetrics) -> None:
    """Print a text-table view of BacktestMetrics to stdout.

    Format is plain text by design — the report is consumed by humans
    reviewing a backtest at the terminal. JSON / structured output is
    deferred until 3.2.4's 4-cell runner needs to diff metrics across
    cells programmatically.
    """
    typer.echo(f"=== Backtest report — run_id={run_id} ===")
    typer.echo(f"Total trades:       {metrics.total_trades}")
    typer.echo(f"Open trades:        {metrics.open_trades}")
    typer.echo(f"Hit rate:           {metrics.hit_rate:.3f}")
    typer.echo(
        f"Expectancy E:       {metrics.expectancy:+.4f} R",
    )
    if metrics.sharpe is not None:
        bonus = "  [BONUS]" if metrics.sharpe_bonus_flag else ""
        typer.echo(f"Sharpe (annual):    {metrics.sharpe:+.4f}{bonus}")
    else:
        typer.echo("Sharpe (annual):    n/a (insufficient sample)")
    if metrics.walk_forward_consistency is not None:
        typer.echo(
            f"Walk-forward:       {metrics.walk_forward_consistency:.3f} "
            "(fraction of windows with E > 0)",
        )
    else:
        typer.echo(
            "Walk-forward:       n/a (insufficient sample for window count)",
        )
    typer.echo(f"Max drawdown:       {metrics.max_drawdown:.3f}")
    if metrics.avg_winner_r is not None:
        typer.echo(f"Avg winner R:       {metrics.avg_winner_r:+.4f}")
    if metrics.avg_loser_r is not None:
        typer.echo(f"Avg loser R:        {metrics.avg_loser_r:+.4f}")
    typer.echo("")
    typer.echo("--- Pass/fail vs thresholds ---")
    for r in metrics.results:
        value_str = "n/a" if r.value is None else f"{r.value:.4f}"
        threshold_str = (
            "n/a" if r.threshold is None else f"{r.threshold:.4f}"
        )
        typer.echo(
            f"  {r.name:<28} value={value_str:<10} "
            f"threshold={threshold_str:<10} {r.pass_fail.upper()}",
        )
    typer.echo("")
    label = "PASS" if metrics.overall_pass else "FAIL"
    typer.echo(f"OVERALL: {label}")


# ---------------------------------------------------------------------------
# `backtest run-4cell` subcommand — Phase 3.2.4.4
# ---------------------------------------------------------------------------


@backtest_app.command("run-4cell")
def backtest_run_4cell(
    store_url: str = typer.Option(
        ...,
        "--store",
        help="Backtest store URL: 'sqlite:path/to/db' for the persistent "
             "store, or ':memory:' (rare; results lost at process exit "
             "unless --report-path captures them).",
    ),
    report_path: Path = typer.Option(
        ...,
        "--report-path",
        help="Path where the markdown comparison report is written.",
    ),
    period_start: str = typer.Option(
        ...,
        "--from",
        help="Backtest period start, ISO date (e.g. 2024-01-01).",
    ),
    period_end: str = typer.Option(
        ...,
        "--to",
        help="Backtest period end, ISO date (e.g. 2026-01-01).",
    ),
    walk_forward_windows: int = typer.Option(
        8,
        "--walk-forward-windows",
        help="Number of equal-time walk-forward slices over the period. "
             "Default 8 (3-month slices for a 2-year period).",
    ),
    profile_path: Path | None = typer.Option(
        None,
        "--profile",
        help="Path to a CalibrationProfile YAML; defaults to v5_default.",
    ),
    seed: int = typer.Option(
        0,
        "--seed",
        help="Determinism seed. Phase 3.2.x is fully deterministic so this "
             "currently has no effect; wired for Phase 3.4+ tuning that "
             "will introduce randomness (grid jitter, Bayesian opt, etc.).",
    ),
    trades: str = typer.Option(
        "noop",
        "--trades",
        help="Trade producer. 'noop' (default) returns zero trades for "
             "every cell — the 4-cell plumbing runs end-to-end (start_run, "
             "finish_run, RunMetadata persisted, comparison report "
             "rendered) but every cell reports 0 trades. 'synthetic' "
             "drives the default scripted scenario through the full "
             "Phase 3.4 pipeline and emits one NoOp open trade per "
             "stored signal — used by Phase 3.5.4 to validate the "
             "wiring end-to-end before the real-data run in 3.5.5. "
             "'replay' streams the downloaded historical parquet data "
             "(--replay-data) through the pipeline and realizes trades "
             "via SimplePnLProvider — the Phase 3.5.5 real-data run.",
    ),
    replay_data: Path | None = typer.Option(
        None,
        "--replay-data",
        help="Replay parquet root (e.g. data/historical/bulk) — the "
             "per-source dir holding {TICKER}/{YYYY-MM}.parquet. "
             "Required when --trades replay.",
    ),
    uw_enrichment: bool = typer.Option(
        False,
        "--uw-enrichment",
        help="Wire real Unusual Whales providers into the fusion cells' "
             "M21-M27 enrichment (Phase 3.5.5 B3). Default off — fusion "
             "runs on NoOp enrichment (wiring smoke). Enable only once UW "
             "historical data access is granted; before that the "
             "providers return only the last few days.",
    ),
    chain_snapshots: Path | None = typer.Option(
        None,
        "--chain-snapshots",
        help="Phase 3.6: directory of per-ticker daily chain snapshots "
             "(scripts/download_chain_snapshots.py). When set, the fusion "
             "cells' dealer-gamma axis (M21) is fed by the self-derived "
             "GEX provider instead of Unusual Whales — a real confluence "
             "verdict without UW. Takes precedence over --uw-enrichment.",
    ),
    min_premium: float | None = typer.Option(
        None,
        "--min-premium",
        help="Candidate pre-filter (Phase 3.5.5 A5): skip replayed "
             "prints whose premium (USD) is below this, so a full run "
             "processes unusual-size trades rather than all ~245M "
             "prints. Default: no filter. The value is a modelling "
             "choice — it interacts with M38 cluster counts.",
    ),
    verdict_path: Path | None = typer.Option(
        None,
        "--verdict-path",
        help="If set, apply the Phase 3.5.6 falsification framework to "
             "the four cells and write the mechanical verdict (edge "
             "proven / rejected / insufficient) here as markdown — by "
             "convention docs/phase-3.5-results.md. Default: skip the "
             "verdict step (the comparison report is still written).",
    ),
) -> None:
    """Run the Formülasyon A 4-cell combinatorial backtest end-to-end.

    Four runs back-to-back: (Tier-1, single), (Tier-1, fusion),
    (Tier-2, single), (Tier-2, fusion). Each cell is a separate
    run_id in the SQLite store with universe_id = cell name. The
    comparison report is written to ``--report-path`` as markdown.

    Phase 3.2.4 scope: orchestration + windowing + report rendering.
    Real trade producers (driving the full pipeline through
    fusion+orchestrator and writing signals to the store) land in
    Phase 3.3 alongside the SimplePnL exit-quote source.
    """
    # Configure logging up front (to stderr — stdout carries the console
    # summary). Level honours UOA_LOG_LEVEL; a full-universe replay should
    # run with UOA_LOG_LEVEL=WARNING or the per-signal INFO logs dominate.
    _configure_logging(log_to_stderr=True)

    if trades not in ("noop", "synthetic", "replay"):
        msg = (
            f"--trades must be 'noop', 'synthetic', or 'replay'; "
            f"got {trades!r}."
        )
        raise typer.BadParameter(msg, param_hint="--trades")
    if trades == "replay" and replay_data is None:
        msg = "--trades replay requires --replay-data PATH."
        raise typer.BadParameter(msg, param_hint="--replay-data")

    # Parse period dates as UTC midnight.
    try:
        start_dt = datetime.fromisoformat(period_start).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(period_end).replace(tzinfo=UTC)
    except ValueError as exc:
        msg = f"--from / --to must be ISO date (YYYY-MM-DD); got {exc}"
        raise typer.BadParameter(msg, param_hint="--from") from exc

    if end_dt <= start_dt:
        msg = f"--to ({period_end}) must be after --from ({period_start})"
        raise typer.BadParameter(msg, param_hint="--to")

    profile = _resolve_profile(profile_path)
    store = _build_store(store_url)

    producer: _TradeProducer
    if trades == "synthetic":
        from uoa_detector.backtest.cell_runner import synthetic_trade_producer
        producer = synthetic_trade_producer
    elif trades == "replay":
        from uoa_detector.backtest.cell_runner import replay_trade_producer
        from uoa_detector.historical.universe import (
            read_universe,
            tickers_only,
        )

        # The downloaded universes — the reduced 9 / 15-name lists,
        # not the cell runner's default tier1_anchor / tier2_starter.
        tier1_csv = Path("data/universes/tier1_reduced.csv")
        tier2_csv = Path("data/universes/tier2_top15.csv")
        for csv_path in (tier1_csv, tier2_csv):
            if not csv_path.exists():
                msg = f"universe CSV not found: {csv_path}"
                raise typer.BadParameter(msg, param_hint="--trades")
        assert replay_data is not None  # guarded above
        # M37 relative-premium baseline. Optional: absent → M37 NoOp.
        medians_csv: Path | None = Path("data/medians_bulk.csv")
        if medians_csv is not None and not medians_csv.exists():
            medians_csv = None
        producer = replay_trade_producer(
            replay_data,
            tier1_tickers=tickers_only(read_universe(tier1_csv)),
            tier2_tickers=tickers_only(read_universe(tier2_csv)),
            medians_csv=medians_csv,
            use_uw_enrichment=uw_enrichment,
            chain_snapshots_dir=chain_snapshots,
            min_premium_usd=(
                Decimal(str(min_premium)) if min_premium is not None else None
            ),
        )
    else:
        producer = noop_trade_producer

    try:
        results = run_4cell_backtest(
            profile=profile,
            store=store,
            period_start=start_dt,
            period_end=end_dt,
            walk_forward_windows=walk_forward_windows,
            trade_producer=producer,
            seed=seed,
        )
    finally:
        store.close()

    report = render_4cell_comparison_report(results)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    # Phase 3.5.6 — mechanical falsification verdict, written only when
    # the operator asks for it (--verdict-path). The four pinned reject
    # scenarios + the sample-size gate; no judgment-call wiggle room.
    if verdict_path is not None:
        from uoa_detector.backtest.falsification import (
            apply_falsification,
            write_verdict_report,
        )
        falsification_verdict = apply_falsification(results)
        write_verdict_report(
            falsification_verdict,
            results,
            verdict_path,
            period_label=f"{period_start} → {period_end}",
        )
        typer.echo(
            f"=== Phase 3.5.6 verdict: "
            f"{falsification_verdict.verdict.upper()} "
            f"— wrote {verdict_path} ===",
        )
        typer.echo(f"  {falsification_verdict.rationale}")

    # Console summary so the operator sees the headline at the terminal.
    typer.echo(f"=== 4-cell backtest complete — wrote {report_path} ===")
    for r in results:
        verdict = "PASS" if r.metrics.overall_pass else "FAIL"
        typer.echo(
            f"  {r.cell.name:<14} "
            f"trades={r.metrics.total_trades:>4} "
            f"open={r.metrics.open_trades:>4} "
            f"E={r.metrics.expectancy:+.4f} "
            f"{verdict}",
        )


def main() -> None:
    """Module entry point: ``python -m uoa_detector``."""
    app()


if __name__ == "__main__":
    main()
