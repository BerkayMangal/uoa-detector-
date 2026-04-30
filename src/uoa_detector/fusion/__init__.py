"""Source-fusion layer.

Reconciles ``RawPrint`` events from one or more ``RawFlowSource``s into
canonical ``OptionsPrint``s with attached ``SourceAgreement``. ``SourceFusion``
handles event-time watermark windowing; ``classify_agreement`` handles tier
resolution.
"""

from uoa_detector.fusion.agreement import classify_agreement
from uoa_detector.fusion.source import SourceFusion

__all__ = ["SourceFusion", "classify_agreement"]
