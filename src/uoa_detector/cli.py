"""CLI entry point.

Usage:
    python -m uoa_detector run --source synthetic --scenario default
"""

from __future__ import annotations

import asyncio
import logging
import sys

import structlog
import typer

from uoa_detector.config import default_config
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.sources.scenarios import (
    ScenarioOverrideStage,
    default_scenario,
    overrides_lookup,
)
from uoa_detector.sources.synthetic import SyntheticFlowSource

app = typer.Typer(help="UOA + Convexity Detector v5 CLI", add_completion=False)


@app.callback()
def _root() -> None:
    """Force typer into multi-command mode so ``run`` is an explicit subcommand."""


def _configure_logging() -> None:
    """Wire up structlog to print one structured line per event."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
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


@app.command()
def run(
    source: str = typer.Option("synthetic", help="Flow source name."),
    scenario: str = typer.Option("default", help="Synthetic scenario name."),
) -> None:
    """Run the pipeline against a flow source and print labeled signals."""
    _configure_logging()

    if source != "synthetic":
        # Phase 2 will register polygon / unusual_whales / csv_replay here.
        msg = f"Phase 1 supports only --source synthetic; got {source!r}"
        raise typer.BadParameter(msg, param_hint="--source")
    if scenario != "default":
        msg = f"Phase 1 ships only the 'default' scenario; got {scenario!r}"
        raise typer.BadParameter(msg, param_hint="--scenario")

    asyncio.run(_run_default_synthetic())


async def _run_default_synthetic() -> None:
    """Drive the default scenario through the Phase 1 pipeline."""
    cfg = default_config()
    steps = default_scenario()
    prints = [s.print_ for s in steps]
    src = SyntheticFlowSource(prints)

    # Override stage runs FIRST; then the stub stages fill missing fields.
    stages = [ScenarioOverrideStage(overrides_lookup(iter(steps))), *default_stage_pipeline()]

    pipeline = Pipeline(src, stages, config=cfg)
    await pipeline.run()


def main() -> None:
    """Module entry point: ``python -m uoa_detector``."""
    app()


if __name__ == "__main__":
    main()
