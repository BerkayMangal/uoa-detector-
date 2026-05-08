"""Live observer mode (Phase 3.3.5).

Wraps the Phase 3.2 ``Pipeline`` with graceful shutdown for long-
running live runs. Both ThetaData and Unusual Whales live sources
feed into ``SourceFusion`` (multi-source mode); fused
``OptionsPrint``s flow through the standard scoring/labeling stages
and emerge as ``SignalDecisionRecord``s on the configured writer.

Modules:
  - observer.py — LiveObserver (graceful shutdown wrapper)
  - factory.py  — turns --feeds string into [RawFlowSource]
"""
