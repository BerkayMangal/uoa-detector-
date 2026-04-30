"""Source-fusion layer.

Reconciles ``RawPrint`` events from one or more ``RawFlowSource``s into
canonical ``OptionsPrint``s with attached ``SourceAgreement``. The
``SourceFusion`` class (Phase 2.3.3) handles windowing; the
``classify_agreement`` helper (Phase 2.3.2) handles tier resolution.
"""

from uoa_detector.fusion.agreement import classify_agreement

__all__ = ["classify_agreement"]
