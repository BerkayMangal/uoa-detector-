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
    SimplePnLProvider,
)
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore, StoredSignal

__all__ = [
    "RAWPRINT_PARQUET_SCHEMA",
    "BacktestMetrics",
    "BacktestStore",
    "BacktestStoreProtocol",
    "DataIntegrityError",
    "DictExitQuoteProvider",
    "ErrorRecord",
    "ExitQuoteProvider",
    "ExitReason",
    "MetricResult",
    "MetricThresholds",
    "MockPnLProvider",
    "NoOpPnLProvider",
    "ParquetSchemaMismatchError",
    "PnLProvider",
    "RealizedTrade",
    "RunMetadata",
    "SimplePnLProvider",
    "SqliteBacktestStore",
    "StoredSignal",
    "compute_metrics",
]
