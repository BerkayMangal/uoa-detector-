"""Typed Protocols for Phase-3 data dependencies, with NoOp fallbacks.

Each Protocol exists so Phase 2 stages can declare what they need without
binding to a particular vendor or implementation. NoOp fallbacks let the
pipeline run end-to-end against the synthetic source without any external
data wired up — they return ``None`` / empty / neutral values, and stages
treat ``None`` per the Phase 2 prompt's spec ("missing data is 0.0, not
0.5") plus the labeler's None-tolerant logic.

Real implementations land in Phase 3.
"""

from uoa_detector.providers.catalyst_calendar import (
    CatalystCalendarProvider,
    CatalystEvent,
    CatalystKind,
    NoOpCatalystCalendarProvider,
)
from uoa_detector.providers.dark_pool import (
    DarkPoolPrint,
    DarkPoolPrintProvider,
    NoOpDarkPoolPrintProvider,
)
from uoa_detector.providers.dealer_positioning import (
    DealerPositioning,
    DealerPositioningProvider,
    NoOpDealerPositioningProvider,
)
from uoa_detector.providers.iv_history import (
    IVHistoryProvider,
    IVRankSnapshot,
    NoOpIVHistoryProvider,
)
from uoa_detector.providers.median_trade_size import (
    CSVMedianTradeSizeProvider,
    InMemoryMedianTradeSizeProvider,
    MedianTradeSizeProvider,
    NoOpMedianTradeSizeProvider,
)
from uoa_detector.providers.open_interest import (
    NoOpOpenInterestProvider,
    OpenInterestProvider,
    OpenInterestSnapshot,
)
from uoa_detector.providers.price_action import (
    NoOpPriceActionProvider,
    PriceActionProvider,
    PriceActionSnapshot,
)
from uoa_detector.providers.sector_map import (
    InMemorySectorMapProvider,
    NoOpPeerFlowProvider,
    NoOpSectorMapProvider,
    PeerFlowEvent,
    PeerFlowProvider,
    SectorMapProvider,
)

__all__ = [
    "CSVMedianTradeSizeProvider",
    "CatalystCalendarProvider",
    "CatalystEvent",
    "CatalystKind",
    "DarkPoolPrint",
    "DarkPoolPrintProvider",
    "DealerPositioning",
    "DealerPositioningProvider",
    "IVHistoryProvider",
    "IVRankSnapshot",
    "InMemoryMedianTradeSizeProvider",
    "InMemorySectorMapProvider",
    "MedianTradeSizeProvider",
    "NoOpCatalystCalendarProvider",
    "NoOpDarkPoolPrintProvider",
    "NoOpDealerPositioningProvider",
    "NoOpIVHistoryProvider",
    "NoOpMedianTradeSizeProvider",
    "NoOpOpenInterestProvider",
    "NoOpPeerFlowProvider",
    "NoOpPriceActionProvider",
    "NoOpSectorMapProvider",
    "OpenInterestProvider",
    "OpenInterestSnapshot",
    "PeerFlowEvent",
    "PeerFlowProvider",
    "PriceActionProvider",
    "PriceActionSnapshot",
    "SectorMapProvider",
]
