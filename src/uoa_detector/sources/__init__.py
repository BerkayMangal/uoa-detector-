"""Pluggable options-flow sources.

Phase 2 split:
  - ``RawFlowSource`` Protocol — streams ``RawPrint`` events through fusion.
  - ``QuoteSnapshotSource`` Protocol — point-in-time IV/OI lookup.
  - ``FlowDataSource`` (legacy) Protocol — retained for adapters that
    legitimately deliver canonical prints (e.g., parquet replays).

Adapters:
  - ``SyntheticRawFlowSource`` (production-tested): in-memory scripted source.
  - ``StallingRawFlowSource`` (test-only): for the watermark-recovery test.
  - ``PolygonFlowSource`` (Phase 2 STUB): OPRA tape direct, raw trades + NBBO.
  - ``UnusualWhalesFlowSource`` (Phase 2 STUB): labeled aggregator.
  - ``IBKRQuoteSource`` (Phase 2 STUB): point-in-time IV/OI snapshots.

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
from uoa_detector.sources.ibkr_quotes import IBKRConfig, IBKRQuoteSource
from uoa_detector.sources.polygon import PolygonConfig, PolygonFlowSource
from uoa_detector.sources.synthetic import (
    StallingRawFlowSource,
    SyntheticRawFlowSource,
    to_raw_print,
)
from uoa_detector.sources.unusual_whales import (
    UnusualWhalesConfig,
    UnusualWhalesFlowSource,
)

__all__ = [
    "FlowDataSource",
    "IBKRConfig",
    "IBKRQuoteSource",
    "PolygonConfig",
    "PolygonFlowSource",
    "QuoteSnapshot",
    "QuoteSnapshotSource",
    "RawFlowSource",
    "StallingRawFlowSource",
    "SyntheticRawFlowSource",
    "UnusualWhalesConfig",
    "UnusualWhalesFlowSource",
    "to_raw_print",
]
