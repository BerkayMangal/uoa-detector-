"""4-cell combinatorial runner — Phase 3.2.4.2.

Runs the Formülasyon A 4-cell backtest matrix:

  +-----------+--------+----------+
  |           | single | fusion   |
  +-----------+--------+----------+
  | Tier-1    |   A    |    B     |
  | Tier-2    |   C    |    D     |
  +-----------+--------+----------+

Each cell is a separate ``run_id`` in the SQLite store, with
``RunMetadata`` flagging its (universe, fusion_mode) coordinates.
The cell runner produces the four runs back-to-back; the comparison
report (3.2.4.3) consumes them.

decision (cell-name strings):
  ``tier1_single``, ``tier1_fusion``, ``tier2_single``,
  ``tier2_fusion`` — flat, snake_case. The 4-cell matrix is fixed
  at four cells in 3.2.4 and the names go into ``run_id`` plus the
  ``RunMetadata.universe_id``. Future runners (e.g. a 9-cell or
  16-cell variant) will use longer names; flat strings keep the
  4-cell case readable.

decision (universe loader belongs here, not in calibration/):
  ``data/universes/*.csv`` are calibration artifacts — they're
  pinned, version-controlled inputs to the 4-cell backtest. But
  the loader itself is backtest-only (Phase 3.2.4 is the first
  consumer). When live trading needs to filter by universe in
  Phase 3.5+, that's a new use that may want a richer loader; for
  now this stays scoped to the cell runner.

decision (cell isolation = separate run_ids, not separate stores):
  All four cells write to the same SQLite store with distinct
  ``run_id`` values. Caller passes a single ``--store sqlite:...``
  flag and gets back a 4-row ``backtest_run`` table. The metric
  calculator already knows how to slice by ``run_id``; the report
  diffs by querying the same store four times.

Phase 3.2.4 contract:
  Cell runner returns a ``CellRunResult`` per cell with the
  computed ``BacktestMetrics``. The comparison report consumes the
  list. Determinism: same inputs → identical results across runs.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from uoa_detector.backtest.metrics import (
    BacktestMetrics,
    MetricThresholds,
    compute_metrics,
)
from uoa_detector.backtest.pnl_provider import NoOpPnLProvider, RealizedTrade
from uoa_detector.backtest.walk_forward import (
    WalkForwardWindow,
    no_op_tuner,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from uoa_detector.backtest.protocol import BacktestStoreProtocol
    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration.profile import CalibrationProfile
    from uoa_detector.pipeline.stage import EnrichmentStage


_logger = logging.getLogger(__name__)


CellUniverse = Literal["tier1", "tier2"]
CellFusion = Literal["single", "fusion"]


# The four canonical cell names. Pinned by test_cell_names_match_acceptance.
CELL_NAMES: tuple[str, ...] = (
    "tier1_single",
    "tier1_fusion",
    "tier2_single",
    "tier2_fusion",
)


def cell_name(universe: CellUniverse, fusion: CellFusion) -> str:
    """Compose the canonical cell name from coordinates."""
    return f"{universe}_{fusion}"


def universe_path(universe: CellUniverse) -> Path:
    """Resolve a universe coordinate to its CSV path."""
    if universe == "tier1":
        return Path("data/universes/tier1_anchor.csv")
    return Path("data/universes/tier2_starter.csv")


def load_universe_tickers(universe: CellUniverse) -> tuple[str, ...]:
    """Load the ticker list for a universe coordinate.

    Returns tickers in file order (which is the curated order, not
    alphabetical — Phase 3 prep approved the ordering).
    Tier-1 is fixed at 20; Tier-2 is the broader Phase 3.2.0a list.
    """
    path = universe_path(universe)
    if not path.exists():
        msg = (
            f"Universe file missing: {path}. The 4-cell runner "
            "expects both tier1_anchor.csv and tier2_starter.csv to "
            "be committed under data/universes/."
        )
        raise FileNotFoundError(msg)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        tickers = tuple(row["ticker"].strip().upper() for row in reader)
    if not tickers:
        msg = f"Universe file {path} has no rows"
        raise ValueError(msg)
    return tickers


# ---------------------------------------------------------------------------
# Cell specification + result
# ---------------------------------------------------------------------------


class CellSpec(BaseModel):
    """A single cell of the 4-cell matrix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    universe: CellUniverse
    fusion: CellFusion

    @property
    def name(self) -> str:
        return cell_name(self.universe, self.fusion)


CANONICAL_CELLS: tuple[CellSpec, ...] = (
    CellSpec(universe="tier1", fusion="single"),
    CellSpec(universe="tier1", fusion="fusion"),
    CellSpec(universe="tier2", fusion="single"),
    CellSpec(universe="tier2", fusion="fusion"),
)


@dataclass(frozen=True)
class CellRunResult:
    """One cell's run output: (CellSpec, run_id, BacktestMetrics)."""

    cell: CellSpec
    run_id: str
    metrics: BacktestMetrics
    windows: tuple[WalkForwardWindow, ...]


# ---------------------------------------------------------------------------
# Trade producer Protocol
# ---------------------------------------------------------------------------


class _TradeProducerProto:
    """Internal documentation for the trade-producer callable shape.

    The cell runner is intentionally agnostic about how trades come
    out of the pipeline. A trade producer is a callable that takes
    a CellSpec + a list of WalkForwardWindow + a CalibrationProfile,
    runs the pipeline, and returns the closed/open trades to feed
    the metric calculator. Real producers (live replay) land in
    Phase 3.3 alongside the SimplePnL exit-quote source; for 3.2.4
    the only producer is a fixture-driven mock used by tests and
    by the CLI's smoke run.
    """


# Type alias for the callable shape.
if TYPE_CHECKING:
    TradeProducer = Callable[
        [CellSpec, "tuple[WalkForwardWindow, ...]", "CalibrationProfile"],
        list[RealizedTrade],
    ]


def fixture_trade_producer(
    fixtures: dict[str, list[RealizedTrade]],
) -> Callable[
    [CellSpec, tuple[WalkForwardWindow, ...], CalibrationProfile],
    list[RealizedTrade],
]:
    """Build a TradeProducer that returns the fixture trades for a cell.

    Used by tests + the CLI smoke path. Production-shape producers
    that drive the full pipeline land in Phase 3.3.
    """

    def _producer(
        cell: CellSpec,
        windows: tuple[WalkForwardWindow, ...],
        profile: CalibrationProfile,
    ) -> list[RealizedTrade]:
        return list(fixtures.get(cell.name, []))

    return _producer


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_4cell_backtest(
    *,
    profile: CalibrationProfile,
    store: BacktestStoreProtocol,
    period_start: datetime,
    period_end: datetime,
    walk_forward_windows: int,
    trade_producer: Callable[
        [CellSpec, tuple[WalkForwardWindow, ...], CalibrationProfile],
        list[RealizedTrade],
    ],
    thresholds: MetricThresholds | None = None,
    cells: tuple[CellSpec, ...] | None = None,
    seed: int = 0,
) -> tuple[CellRunResult, ...]:
    """Run the 4-cell matrix end-to-end.

    For each cell:
      1. Compute the walk-forward windows for the period.
      2. Apply the no-op tuner (Phase 3.2.4 placeholder).
      3. Produce trades via ``trade_producer``.
      4. Persist the cell as a separate run in ``store`` with
         universe_id set to the cell's name.
      5. Compute metrics via ``compute_metrics``.

    Returns the four ``CellRunResult``s in canonical order. Caller
    owns the store's lifecycle (consistent with the Phase 3.2.1
    'caller-owned store' contract).

    decision (one start_run per cell):
      Each cell calls store.start_run with its own universe_id and
      run_id. This is the first explicit ``start_run`` consumer the
      acceptance doc names — Phase 1-2 callers used the implicit-
      default path; the 4-cell runner owns its own runs.
    """
    cells_to_run = cells if cells is not None else CANONICAL_CELLS
    th = thresholds or MetricThresholds()

    from uoa_detector.backtest.walk_forward import equal_time_slices

    windows = equal_time_slices(period_start, period_end, walk_forward_windows)

    results: list[CellRunResult] = []
    for cell in cells_to_run:
        run_id = f"4cell-{cell.name}"
        # Apply the no-op tuner per window. Phase 3.2.4 returns the
        # same profile every time; structuring it this way means
        # Phase 3.4+ swaps in a real tuner without touching here.
        tuned_per_window = [no_op_tuner(profile, w) for w in windows]
        # The tuned profile is the same instance everywhere in 3.2.4;
        # keep the variable so flake doesn't complain and so future
        # readers see the data flow.
        _ = tuned_per_window

        store.start_run(
            profile,
            run_id=run_id,
            universe_id=cell.name,
            source_config_hash=f"cell-{cell.fusion}",
        )

        trades = trade_producer(cell, windows, profile)
        # Note: we DON'T write StoredSignals here — the trade producer
        # is the integration boundary. In 3.3 the producer will drive
        # the full pipeline (which writes signals to the store as a
        # side effect); for 3.2.4 fixture testing the producer just
        # hands back trades and the store carries the run metadata.

        store.finish_run()

        metrics = compute_metrics(
            trades, thresholds=th, walk_forward_windows=walk_forward_windows,
        )
        results.append(
            CellRunResult(
                cell=cell,
                run_id=run_id,
                metrics=metrics,
                windows=windows,
            ),
        )
        _logger.info(
            "4-cell run: cell=%s run_id=%s total=%d open=%d overall=%s",
            cell.name, run_id,
            metrics.total_trades, metrics.open_trades,
            "PASS" if metrics.overall_pass else "FAIL",
        )

    return tuple(results)


# ---------------------------------------------------------------------------
# NoOp trade producer — drives the 4-cell smoke path with all-open trades
# ---------------------------------------------------------------------------


def noop_trade_producer(
    cell: CellSpec,
    windows: tuple[WalkForwardWindow, ...],
    profile: CalibrationProfile,
) -> list[RealizedTrade]:
    """Return zero trades for any cell.

    Used by the CLI smoke path when no real trade source is wired.
    Cell runs produce empty metrics (insufficient sample) but the
    plumbing — start_run / finish_run / RunMetadata — is exercised.
    """
    return []


def noop_pnl_signal_to_trade(signal: StoredSignal) -> RealizedTrade:
    """Convert a StoredSignal to a NoOp open trade (helper for callers)."""
    return NoOpPnLProvider().provide(signal)


# ---------------------------------------------------------------------------
# Synthetic trade producer — Phase 3.5.4 4-cell pre-flight backtest
# ---------------------------------------------------------------------------


def synthetic_trade_producer(
    cell: CellSpec,
    windows: tuple[WalkForwardWindow, ...],
    profile: CalibrationProfile,
) -> list[RealizedTrade]:
    """Drive the default synthetic scenario through the full Phase 3.4
    pipeline, then convert every StoredSignal into a NoOp open trade.

    Phase 3.5.4: this is a **wiring smoke** producer, not a real PnL
    engine. The four cells (tier1_single, tier1_fusion, tier2_single,
    tier2_fusion) share the same synthetic prints; the trade count
    is identical across cells. The 4-cell runner exercises:

      - SyntheticRawFlowSource → SourceFusion → all 13 stages
      - StoredSignal write-through per event
      - RealizedTrade construction via NoOpPnLProvider
        (``realized_r=None``, ``exit_reason="holding_window_open"``)
      - Walk-forward windowing + metric aggregation surface

    Real PnL is Phase 3.5.5's job (the historical replay run that
    consumes the Phase 3.5.3 download). For 3.5.4 the only
    invariant we test is "≥ 1 trade reaches metrics per cell so the
    pipeline → store → producer → metrics chain works end-to-end".
    """
    # Imports are scoped here so this module's import graph stays
    # narrow (cli.py already pulls cell_runner at startup; we don't
    # want the synthetic-source machinery loaded for every CLI run).
    import asyncio

    from uoa_detector.backtest.store import BacktestStore
    from uoa_detector.pipeline.orchestrator import Pipeline
    from uoa_detector.pipeline.stages import default_stage_pipeline
    from uoa_detector.sources.scenarios import default_scenario_prints
    from uoa_detector.sources.synthetic import (
        SyntheticRawFlowSource,
        to_raw_print,
    )

    del cell, windows  # producer is cell-agnostic in 3.5.4

    raw_prints = [
        to_raw_print(p, source_id="synthetic")
        for p in default_scenario_prints()
    ]
    source = SyntheticRawFlowSource("synthetic", raw_prints)
    store = BacktestStore()
    pipeline = Pipeline(
        sources=[source],
        stages=default_stage_pipeline(),
        profile=profile,
        store=store,
    )
    asyncio.run(pipeline.run())

    pnl = NoOpPnLProvider()
    return [pnl.provide(sig) for sig in store.all()]


# ---------------------------------------------------------------------------
# Replay trade producer — Phase 3.5.5 real-data 4-cell backtest
# ---------------------------------------------------------------------------


def _replay_stages(fusion: CellFusion) -> list[EnrichmentStage]:
    """Return the stage list for a cell's fusion coordinate.

    ``single`` runs only the core-flow stages — the ones that score
    the raw UOA print without external-source corroboration
    (time-of-day, DTE decay, sweep/block, relative premium, temporal
    clustering + decay). ``fusion`` runs the full Phase 3.4 pipeline,
    adding the M21-M27 enrichment confluence on top.

    NOTE (Phase 3.5.5.2): the M21-M27 enrichment stages in the fusion
    set are constructed with their default NoOp providers — no real
    Unusual Whales provider is wired into the enrichment stages
    anywhere in the codebase yet. So a fusion run today exercises the
    enrichment *plumbing* but the enrichment *scores* are neutral.
    Wiring real UW providers into M21-M27 is a follow-up sub-phase;
    Phase 3.5.6's falsification verdict must not be read off a
    NoOp-enrichment run.
    """
    from uoa_detector.pipeline.stages import (
        ClusterDecayStage,
        DTEDecayStage,
        RelativePremiumStage,
        SweepBlockStage,
        TemporalClusterStage,
        TimeOfDayStage,
        default_stage_pipeline,
    )

    if fusion == "fusion":
        return list(default_stage_pipeline())
    # Core-flow subset, in the same relative order as the full pipeline.
    return [
        TimeOfDayStage(),
        DTEDecayStage(),
        SweepBlockStage(),
        RelativePremiumStage(),
        TemporalClusterStage(),
        ClusterDecayStage(),
    ]


def replay_trade_producer(
    data_dir: Path,
    *,
    tier1_tickers: tuple[str, ...],
    tier2_tickers: tuple[str, ...],
    source_id: str = "thetadata",
) -> Callable[
    [CellSpec, tuple[WalkForwardWindow, ...], CalibrationProfile],
    list[RealizedTrade],
]:
    """Build a TradeProducer that replays historical parquet data.

    Phase 3.5.5: the real-data counterpart of ``synthetic_trade_producer``.
    For each cell it streams the cell's universe through a
    ``ParquetReplaySource``, drives the Phase 3.4 pipeline (stage set
    chosen by ``cell.fusion`` — see ``_replay_stages``), then realizes
    every stored signal into a ``RealizedTrade`` via ``SimplePnLProvider``
    with exit bids from ``ParquetExitQuoteProvider``.

    ``data_dir`` is the per-source replay root
    (``data/historical/bulk``); ``tier1_tickers`` / ``tier2_tickers``
    are the downloaded universes (the reduced 9 / 15-name lists, NOT
    the cell runner's default tier1_anchor / tier2_starter — those are
    not what was downloaded).
    """

    def _producer(
        cell: CellSpec,
        windows: tuple[WalkForwardWindow, ...],
        profile: CalibrationProfile,
    ) -> list[RealizedTrade]:
        import asyncio

        from uoa_detector.backtest.parquet_exit_quote import (
            ParquetExitQuoteProvider,
        )
        from uoa_detector.backtest.simple_pnl import SimplePnLProvider
        from uoa_detector.backtest.store import BacktestStore
        from uoa_detector.pipeline.orchestrator import Pipeline
        from uoa_detector.sources.parquet_replay import ParquetReplaySource

        tickers = (
            tier1_tickers if cell.universe == "tier1" else tier2_tickers
        )
        period_start = windows[0].start
        period_end = windows[-1].end

        source = ParquetReplaySource(
            source_id,
            data_dir,
            tickers=list(tickers),
            from_month=f"{period_start:%Y-%m}",
            to_month=f"{period_end:%Y-%m}",
        )
        store = BacktestStore()
        pipeline = Pipeline(
            sources=[source],
            stages=_replay_stages(cell.fusion),
            profile=profile,
            store=store,
        )
        asyncio.run(pipeline.run())

        pnl = SimplePnLProvider(
            profile.backtest,
            ParquetExitQuoteProvider(data_dir),
        )
        trades = [pnl.provide(sig) for sig in store.all()]
        _logger.info(
            "replay producer: cell=%s tickers=%d signals=%d",
            cell.name, len(tickers), len(trades),
        )
        return trades

    return _producer
