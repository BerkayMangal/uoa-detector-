"""Backtest store layer.

Phase 3.2.1: introduces ``BacktestStoreProtocol`` plus ``RunMetadata`` and
``ErrorRecord`` domain models. Both the in-memory ``BacktestStore`` and
the ``SqliteBacktestStore`` satisfy the Protocol; pick the one that fits
the workload. Phase 1-2 callers see no breaking change.

Phase 3.2.2: adds the replay harness's parquet schema layer
(``RAWPRINT_PARQUET_SCHEMA``, ``DataIntegrityError``,
``ParquetSchemaMismatchError``, round-trip helpers).

Phase 3.2.3: adds the ``PnLProvider`` Protocol and trivial
implementations (``MockPnLProvider``, ``NoOpPnLProvider``).
``SimplePnLProvider`` lands in 3.2.3.3; the metric calculator in
3.2.3.4.
"""

from uoa_detector.backtest.cell_report import render_4cell_comparison_report
from uoa_detector.backtest.cell_runner import (
    CANONICAL_CELLS,
    CELL_NAMES,
    BacktestDataHandle,
    CellRunResult,
    CellSpec,
    cell_name,
    fixture_trade_producer,
    historical_trade_producer,
    load_universe_tickers,
    noop_trade_producer,
    run_4cell_backtest,
    universe_path,
)
from uoa_detector.backtest.metrics import (
    BacktestMetrics,
    MetricResult,
    MetricThresholds,
    compute_metrics,
)
from uoa_detector.backtest.models import ErrorRecord, RunMetadata
from uoa_detector.backtest.parquet_schema import (
    RAWPRINT_PARQUET_SCHEMA,
    DataIntegrityError,
    ParquetSchemaMismatchError,
)
from uoa_detector.backtest.pnl_provider import (
    ExitReason,
    MockPnLProvider,
    NoOpPnLProvider,
    PnLProvider,
    RealizedTrade,
)
from uoa_detector.backtest.protocol import BacktestStoreProtocol
from uoa_detector.backtest.simple_pnl import (
    DictExitQuoteProvider,
    ExitQuoteProvider,
    ParquetExitQuoteProvider,
    SimplePnLProvider,
)
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.backtest.walk_forward import (
    WalkForwardWindow,
    equal_time_slices,
    equal_trade_count_slices,
    no_op_tuner,
)

__all__ = [
    "CANONICAL_CELLS",
    "CELL_NAMES",
    "RAWPRINT_PARQUET_SCHEMA",
    "BacktestDataHandle",
    "BacktestMetrics",
    "BacktestStore",
    "BacktestStoreProtocol",
    "CellRunResult",
    "CellSpec",
    "DataIntegrityError",
    "DictExitQuoteProvider",
    "ErrorRecord",
    "ExitQuoteProvider",
    "ExitReason",
    "MetricResult",
    "MetricThresholds",
    "MockPnLProvider",
    "NoOpPnLProvider",
    "ParquetExitQuoteProvider",
    "ParquetSchemaMismatchError",
    "PnLProvider",
    "RealizedTrade",
    "RunMetadata",
    "SimplePnLProvider",
    "SqliteBacktestStore",
    "StoredSignal",
    "WalkForwardWindow",
    "cell_name",
    "compute_metrics",
    "equal_time_slices",
    "equal_trade_count_slices",
    "fixture_trade_producer",
    "historical_trade_producer",
    "load_universe_tickers",
    "no_op_tuner",
    "noop_trade_producer",
    "render_4cell_comparison_report",
    "run_4cell_backtest",
    "universe_path",
]
