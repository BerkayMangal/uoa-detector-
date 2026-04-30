"""Pluggable options-flow sources. Implementations: synthetic (Phase 1),
polygon/unusual_whales/csv_replay (later phases).
"""

from uoa_detector.sources.base import FlowDataSource
from uoa_detector.sources.synthetic import SyntheticFlowSource

__all__ = ["FlowDataSource", "SyntheticFlowSource"]
