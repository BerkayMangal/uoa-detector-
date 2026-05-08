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
    Not selected for production; ThetaData (Phase 3.3.2) is the canonical
    OPRA source. Polygon stub retained pending an explicit retire decision.
  - ``IBKRQuoteSource`` (Phase 2 STUB): point-in-time IV/OI snapshots.
    Not selected for production; UW (Phase 3.3.3) provides IV history.
    IBKR stub retained pending an explicit retire decision.

For real production sources see:
  - ``ThetaDataLiveSource`` and ``ThetaDataHistoricalDownloader`` in
    ``uoa_detector.sources.thetadata`` (Phase 3.3.2).
  - ``UnusualWhalesLiveSource`` and the six providers in
    ``uoa_detector.sources.unusual_whales`` (Phase 3.3.3).

Phase 3.3.6 retired the Phase 2 ``UnusualWhalesConfig`` /
``UnusualWhalesFlowSource`` stubs; the live source is the source of
truth for the UW feed now.

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
from uoa_detector.sources.parquet_replay import ParquetReplaySource
from uoa_detector.sources.polygon import PolygonConfig, PolygonFlowSource
from uoa_detector.sources.synthetic import (
    StallingRawFlowSource,
    SyntheticRawFlowSource,
    to_raw_print,
)

__all__ = [
    "FlowDataSource",
    "IBKRConfig",
    "IBKRQuoteSource",
    "ParquetReplaySource",
    "PolygonConfig",
    "PolygonFlowSource",
    "QuoteSnapshot",
    "QuoteSnapshotSource",
    "RawFlowSource",
    "StallingRawFlowSource",
    "SyntheticRawFlowSource",
    "to_raw_print",
]
