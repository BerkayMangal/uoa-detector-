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
import logging
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import typer

from uoa_detector.backtest import BacktestStore, SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.loader import load_profile
from uoa_detector.observability import (
    NDJSONWriter,
    ParquetWriter,
    PrettyWriter,
)
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.multi_source_scenario import multi_source_scenario
from uoa_detector.sources.scenarios import (
    ScenarioOverrideStage,
    default_scenario,
    overrides_lookup,
)
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

if TYPE_CHECKING:
    from uoa_detector.backtest import BacktestStoreProtocol
    from uoa_detector.calibration import CalibrationProfile
    from uoa_detector.observability import DecisionRecordWriter


class OutputFormat(StrEnum):
    """Supported decision-record output formats."""

    PRETTY = "pretty"
    JSON = "json"
    PARQUET = "parquet"


app = typer.Typer(help="UOA + Convexity Detector v5 CLI", add_completion=False)


@app.callback()
def _root() -> None:
    """Force typer into multi-command mode so ``run`` is an explicit subcommand."""


def _configure_logging(*, log_to_stderr: bool) -> None:
    """Wire up structlog. ``log_to_stderr=True`` keeps stdout clean for NDJSON."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr if log_to_stderr else sys.stdout,
        level=logging.INFO,
        force=True,  # override any prior basicConfig in tests
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.KeyValueRenderer(
                key_order=["event", "ticker", "label", "combined_score", "max_r"],
                drop_missing=True,
            ),
        ],
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
    source: str = typer.Option("synthetic", help="Flow source name."),
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
) -> None:
    """Run the pipeline against a flow source and emit labeled signal records."""
    if source != "synthetic":
        msg = f"Only --source synthetic is supported in this build; got {source!r}"
        raise typer.BadParameter(msg, param_hint="--source")
    if scenario != "default":
        msg = f"Only the 'default' scenario name is supported in this build; got {scenario!r}"
        raise typer.BadParameter(msg, param_hint="--scenario")

    writer, log_to_stderr = _build_writer(output, output_file)
    _configure_logging(log_to_stderr=log_to_stderr)
    profile = _resolve_profile(profile_path)
    store = _build_store(store_url)

    # CLI owns the store's lifecycle. Pipeline.run() finalises the run
    # it drove (calls finish_run()) but does NOT close the store —
    # closing is the responsibility of whoever owns the store. Phase
    # 3.2.4's 4-cell runner will own a store across four pipelines.
    try:
        if multi_source:
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


def main() -> None:
    """Module entry point: ``python -m uoa_detector``."""
    app()


if __name__ == "__main__":
    main()
