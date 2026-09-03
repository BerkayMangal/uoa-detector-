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


@dataclass(frozen=True)
class BacktestDataHandle:
    """The data inputs a real (replay-driven) trade producer needs.

    Phase 3.5.0.2: ``run_4cell_backtest``'s producer callable was
    widened from ``(cell, windows, profile)`` to
    ``(cell, windows, profile, data_handle)`` so a producer can reach
    the historical parquet the cell replays. The ``noop`` / ``fixture``
    producers ignore the handle (they accept it and default it to
    ``None``); the ``historical`` producer requires it.

    ``data_dir`` is the per-source parquet root
    (``{data_dir}/{ticker}/{YYYY-MM}.parquet``), the same layout
    ``ParquetReplaySource`` and ``ParquetExitQuoteProvider`` consume.
    ``source_id`` labels the replay source dimension. ``from_month`` /
    ``to_month`` (``"YYYY-MM"``) optionally clip the replay + the
    exit-quote lookup to the same slice.
    """

    data_dir: Path
    source_id: str = "historical"
    from_month: str | None = None
    to_month: str | None = None


# Type alias for the callable shape. Widened in Phase 3.5.0.2 to carry
# the optional data handle a replay-driven producer needs.
if TYPE_CHECKING:
    TradeProducer = Callable[
        [
            CellSpec,
            "tuple[WalkForwardWindow, ...]",
            "CalibrationProfile",
            "BacktestDataHandle | None",
        ],
        list[RealizedTrade],
    ]


def fixture_trade_producer(
    fixtures: dict[str, list[RealizedTrade]],
) -> Callable[
    [
        CellSpec,
        tuple[WalkForwardWindow, ...],
        CalibrationProfile,
        BacktestDataHandle | None,
    ],
    list[RealizedTrade],
]:
    """Build a TradeProducer that returns the fixture trades for a cell.

    Used by tests + the CLI smoke path. Ignores the ``data`` handle.
    """

    def _producer(
        cell: CellSpec,
        windows: tuple[WalkForwardWindow, ...],
        profile: CalibrationProfile,
        data: BacktestDataHandle | None = None,
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
        [
            CellSpec,
            tuple[WalkForwardWindow, ...],
            CalibrationProfile,
            BacktestDataHandle | None,
        ],
        list[RealizedTrade],
    ],
    data_handle: BacktestDataHandle | None = None,
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

        trades = trade_producer(cell, windows, profile, data_handle)
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
    data: BacktestDataHandle | None = None,
) -> list[RealizedTrade]:
    """Return zero trades for any cell.

    Used by the CLI smoke path when no real trade source is wired.
    Cell runs produce empty metrics (insufficient sample) but the
    plumbing — start_run / finish_run / RunMetadata — is exercised.
    Ignores the ``data`` handle.
    """
    return []


def historical_trade_producer(
    cell: CellSpec,
    windows: tuple[WalkForwardWindow, ...],
    profile: CalibrationProfile,
    data: BacktestDataHandle | None = None,
) -> list[RealizedTrade]:
    """Produce real ``RealizedTrade``s for a cell from historical parquet.

    Phase 3.5.0.2 — the replay-driven producer the Phase 3.5 blocker
    flagged as missing. Per cell:

      1. Resolve the cell's universe tickers (Tier-1 anchor / Tier-2
         starter) and replay the matching parquet under
         ``data.data_dir`` through the full detection pipeline. The
         cell's fusion coordinate drives ``force_multi_source``
         (``single`` → fast path, ``fusion`` → windowed fusion).
      2. Take every stored signal that took a position
         (``max_r > 0``) — pinned decision #1: one trade per
         positioned signal, no cross-signal dedup.
      3. Price each with ``SimplePnLProvider`` fed by a
         ``ParquetExitQuoteProvider`` over the same parquet slice.
         Signals whose exit bid is unavailable round-trip as ``open``.

    ``windows`` is unused (walk-forward slicing happens in the metric
    calculator); it is part of the pinned producer signature.

    A ``BacktestDataHandle`` is required — this producer cannot run
    without data, unlike the ``noop`` / ``fixture`` producers.
    """
    import asyncio

    from uoa_detector.backtest.simple_pnl import (
        ParquetExitQuoteProvider,
        SimplePnLProvider,
    )
    from uoa_detector.pipeline.orchestrator import Pipeline
    from uoa_detector.pipeline.stages import default_stage_pipeline
    from uoa_detector.sources.parquet_replay import ParquetReplaySource

    if data is None:
        msg = (
            "historical_trade_producer requires a BacktestDataHandle "
            "(pass data_handle= to run_4cell_backtest). It replays parquet "
            "historical data; there is nothing to replay without it."
        )
        raise ValueError(msg)

    tickers = load_universe_tickers(cell.universe)

    src = ParquetReplaySource(
        data.source_id,
        data.data_dir,
        tickers=tickers,
        from_month=data.from_month,
        to_month=data.to_month,
    )
    # Let the pipeline own its store: an empty in-memory BacktestStore is
    # falsy (``__len__ == 0``), so passing one in would be discarded by the
    # ``store or BacktestStore()`` default. We read signals back off
    # ``pipeline.store`` instead.
    pipeline = Pipeline(
        [src],
        list(default_stage_pipeline()),
        profile=profile,
        force_multi_source=(cell.fusion == "fusion"),
    )
    asyncio.run(pipeline.run())

    # Read positioned signals back off the store the pipeline actually
    # used. (An empty in-memory store is falsy, so the pipeline replaced
    # any we passed with its own — we go through the Protocol surface
    # rather than assume a concrete type.) The pipeline drove exactly one
    # run; iterate every run defensively so this stays correct if that
    # ever changes.
    signals: list[StoredSignal] = [
        s
        for run in pipeline.store.list_runs()
        for s in pipeline.store.iter_records(run.run_id)
        if s.max_r > 0.0
    ]

    exit_quotes = ParquetExitQuoteProvider(
        data.data_dir,
        tickers=tickers,
        from_month=data.from_month,
        to_month=data.to_month,
    )
    pnl = SimplePnLProvider(profile.backtest, exit_quotes)
    return [pnl.provide(s) for s in signals]


def noop_pnl_signal_to_trade(signal: StoredSignal) -> RealizedTrade:
    """Convert a StoredSignal to a NoOp open trade (helper for callers)."""
    return NoOpPnLProvider().provide(signal)
