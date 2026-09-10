"""Enrichment stages — one per spec module.

Phase 1: all stages here are stubs that set neutral defaults and carry a
``# TODO(phase-2):`` marker. Real implementations land in Phase 2.

Exception: ``m39_time_of_day`` is fully implemented because the rest of the
system depends on a correct time-of-day weight (see task spec).
"""

from uoa_detector.pipeline.stage import EnrichmentStage
from uoa_detector.pipeline.stages.cluster_decay_stage import ClusterDecayStage
from uoa_detector.pipeline.stages.live_stages import build_live_stage_pipeline
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.pipeline.stages.m23_price_confirmation import PriceConfirmationStage
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
from uoa_detector.pipeline.stages.m34_sweep_block import SweepBlockStage
from uoa_detector.pipeline.stages.m35_dte_decay import DTEDecayStage
from uoa_detector.pipeline.stages.m37_relative_premium import RelativePremiumStage
from uoa_detector.pipeline.stages.m38_temporal_cluster import TemporalClusterStage
from uoa_detector.pipeline.stages.m39_time_of_day import TimeOfDayStage

__all__ = [
    "ClusterDecayStage",
    "DTEDecayStage",
    "DarkPoolStage",
    "DealerGammaStage",
    "EventCalendarStage",
    "IVExhaustionStage",
    "OpeningClosingStage",
    "PriceConfirmationStage",
    "RelativePremiumStage",
    "SectorPeerStage",
    "SweepBlockStage",
    "TemporalClusterStage",
    "TimeOfDayStage",
    "build_live_stage_pipeline",
]


def default_stage_pipeline() -> list[EnrichmentStage]:
    """Return all stages in their canonical order.

    Order matters where stages depend on each other:
      - M37 (relative premium) feeds the UOA score that M21/M34 may further adjust.
      - M34 (sweep) feeds the UOA score boost.
      - M35 (DTE multiplier) is recorded here but actually applied inside the
        scoring engine (per spec — only to convexity/gamma).
      - M39 (time of day) must run before scoring.
      - ``ClusterDecayStage`` runs AFTER M38 (TemporalClusterStage) because
        M38 must have appended the incoming event to its cluster buffer first;
        the decay stage then reads that and any older buffers to flip stale
        ones. Phase 3.1.1: this stage replaces the walltime asyncio
        ClusterDecayWatcher, fixing backtest-replay correctness.
    """
    return [
        TimeOfDayStage(),  # M39 — full impl
        DTEDecayStage(),  # M35 — records multiplier; applied in scoring engine
        SweepBlockStage(),  # M34
        RelativePremiumStage(),  # M37
        DealerGammaStage(),  # M21
        EventCalendarStage(),  # M22
        PriceConfirmationStage(),  # M23
        IVExhaustionStage(),  # M24
        SectorPeerStage(),  # M25
        DarkPoolStage(),  # M26
        OpeningClosingStage(),  # M27
        TemporalClusterStage(),  # M38 — must precede ClusterDecayStage
        ClusterDecayStage(),  # Phase 3.1.1 — event-time decay check
    ]
