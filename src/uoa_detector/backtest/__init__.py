"""Backtest store layer.

Phase 3.2.1: introduces ``BacktestStoreProtocol`` plus ``RunMetadata`` and
``ErrorRecord`` domain models. Both the in-memory ``BacktestStore`` and
the ``SqliteBacktestStore`` satisfy the Protocol; pick the one that fits
the workload. Phase 1-2 callers see no breaking change.
"""

from uoa_detector.backtest.models import ErrorRecord, RunMetadata
from uoa_detector.backtest.protocol import BacktestStoreProtocol
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore, StoredSignal

__all__ = [
    "BacktestStore",
    "BacktestStoreProtocol",
    "ErrorRecord",
    "RunMetadata",
    "SqliteBacktestStore",
    "StoredSignal",
]
