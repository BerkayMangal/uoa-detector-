"""Pluggable options-flow sources.

Phase 2 split:
  - ``RawFlowSource`` Protocol — streams ``RawPrint`` events through fusion.
  - ``QuoteSnapshotSource`` Protocol — point-in-time IV/OI lookup (Phase 3).
  - ``FlowDataSource`` (legacy) Protocol — retained for adapters that
    legitimately deliver canonical prints (e.g., parquet replays).

Phase 1's ``SyntheticFlowSource`` (canonical-print emitter) was retired in
Phase 2.3.4. New scenarios should construct ``RawPrint`` directly; legacy
scenarios that author ``OptionsPrint`` can convert via ``to_raw_print``.
"""

from uoa_detector.sources.base import (
    FlowDataSource,
    QuoteSnapshot,
    QuoteSnapshotSource,
    RawFlowSource,
)
from uoa_detector.sources.synthetic import (
    StallingRawFlowSource,
    SyntheticRawFlowSource,
    to_raw_print,
)

__all__ = [
    "FlowDataSource",
    "QuoteSnapshot",
    "QuoteSnapshotSource",
    "RawFlowSource",
    "StallingRawFlowSource",
    "SyntheticRawFlowSource",
    "to_raw_print",
]
