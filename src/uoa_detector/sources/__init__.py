"""Pluggable options-flow sources.

Phase 2 split:
  - ``RawFlowSource`` Protocol — streams ``RawPrint`` events through fusion.
  - ``QuoteSnapshotSource`` Protocol — point-in-time IV/OI lookup.
  - ``FlowDataSource`` (legacy) Protocol — retained for adapters that
    legitimately deliver canonical prints (e.g., parquet replays).

Adapters:
  - ``SyntheticRawFlowSource`` (production-tested): in-memory scripted source.
  - ``StallingRawFlowSource`` (test-only): for the watermark-recovery test.

For real production sources see:
  - ``ThetaDataLiveSource`` and ``ThetaDataHistoricalDownloader`` in
    ``uoa_detector.sources.thetadata`` (Phase 3.3.2).
  - ``UnusualWhalesLiveSource`` and the six providers in
    ``uoa_detector.sources.unusual_whales`` (Phase 3.3.3).

Retirement history:
  - Phase 2 ``UnusualWhalesConfig`` / ``UnusualWhalesFlowSource`` stubs
    retired in Phase 3.3.6; live source is the source of truth.
  - Phase 1 ``SyntheticFlowSource`` (canonical-print emitter) retired
    in Phase 2.3.4. New scenarios construct ``RawPrint`` directly;
    legacy scenarios that author ``OptionsPrint`` convert via
    ``to_raw_print``.
  - Phase 2 ``PolygonFlowSource`` + ``IBKRQuoteSource`` stubs retired
    in Phase 3.4.9.1. ThetaData (Phase 3.3.2) is the canonical OPRA
    source; UW (Phase 3.3.3) is the canonical derived-feed source.
    Neither stub had a real consumer after Phase 3.3 wired the live
    sources; keeping unimplemented adapters in the tree was
    misleading. ``RawFlowSource`` Protocol preserved for future
    adapters.
"""

from uoa_detector.sources.base import (
    FlowDataSource,
    QuoteSnapshot,
    QuoteSnapshotSource,
    RawFlowSource,
)
from uoa_detector.sources.parquet_replay import ParquetReplaySource
from uoa_detector.sources.synthetic import (
    StallingRawFlowSource,
    SyntheticRawFlowSource,
    to_raw_print,
)

__all__ = [
    "FlowDataSource",
    "ParquetReplaySource",
    "QuoteSnapshot",
    "QuoteSnapshotSource",
    "RawFlowSource",
    "StallingRawFlowSource",
    "SyntheticRawFlowSource",
    "to_raw_print",
]
