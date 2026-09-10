"""Live enrichment pipeline builder — Phase 3.8.

``default_stage_pipeline()`` constructs every M-stage with *no* provider, so
each falls back to its NoOp default (D7). That is correct for the offline
paths (synthetic, historical, backtest) which must never reach out to a
vendor. It is wrong for the live/REST screener: with NoOp providers M21–M27
are dormant and the daily digest is base-flow-only and mostly empty.

``build_live_stage_pipeline`` returns the **same stage order** as
``default_stage_pipeline`` but constructs each M-stage with its real Unusual
Whales provider, all sharing one ``UnusualWhalesClient`` and the profile's
``data_sources.unusual_whales`` settings. The wiring mirrors exactly how the
integration smokes construct each provider
(``tests/integration/test_m2X_smoke.py``).

UW-key-only: M23 uses ``UnusualWhalesPriceActionProvider`` (Phase 3.3.8), so
the whole enrichment pipeline is backed by the UW REST API — no ThetaData
Terminal is required for the daily list.

D7 preserved: this builder only supplies providers. Each stage still reads
its ``provider_timeout_s`` from ``ctx.profile.scoring.modules.mXX`` inside
``enrich()`` and keeps its own ``asyncio.wait_for`` wrapping. D8 preserved:
no numeric literal lives here — all thresholds and timeouts come from the
profile.

M28 is intentionally absent: it is not a ``PipelineStage``; it is the
overnight T+1 batch validator (``M28Validator``) that runs against a stored
run, not the live stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from uoa_detector.pipeline.stages.cluster_decay_stage import ClusterDecayStage
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
from uoa_detector.pipeline.stages.m34_sweep_block import SweepBlockStage
from uoa_detector.pipeline.stages.m35_dte_decay import DTEDecayStage
from uoa_detector.pipeline.stages.m37_relative_premium import RelativePremiumStage
from uoa_detector.pipeline.stages.m38_temporal_cluster import TemporalClusterStage
from uoa_detector.pipeline.stages.m39_time_of_day import TimeOfDayStage
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)

if TYPE_CHECKING:
    from uoa_detector.calibration import CalibrationProfile
    from uoa_detector.pipeline.stage import EnrichmentStage
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


def build_live_stage_pipeline(
    client: UnusualWhalesClient,
    profile: CalibrationProfile,
) -> list[EnrichmentStage]:
    """Return the canonical stage order with real UW providers wired.

    Same order and same non-provider stages as ``default_stage_pipeline``.
    The M-stages that take a provider get their real ``UnusualWhales*``
    provider, all sharing ``client`` and ``profile.data_sources.unusual_whales``.

    Args:
        client: One shared UW REST client. Its lifecycle (``aclose``) is
            owned by the caller that constructed it — this builder never
            closes it.
        profile: The calibration profile. ``data_sources.unusual_whales``
            supplies each provider's settings (cache TTLs, rate limits);
            per-module timeouts/thresholds are read by the stages from
            ``scoring.modules.mXX`` at ``enrich()`` time (D7/D8).

    Returns:
        A fresh list of stage instances, safe to hand to ``Pipeline``.
    """
    uw = profile.data_sources.unusual_whales

    return [
        TimeOfDayStage(),  # M39 — no external provider
        DTEDecayStage(),  # M35 — no external provider
        SweepBlockStage(),  # M34 — no external provider
        RelativePremiumStage(),  # M37 — no external provider
        DealerGammaStage(  # M21
            provider=UnusualWhalesDealerGammaProvider(
                client=client, settings=uw,
            ),
        ),
        EventCalendarStage(  # M22
            provider=UnusualWhalesCatalystCalendarProvider(
                client=client, settings=uw,
            ),
        ),
        PriceConfirmationStage(  # M23 — UW price-action (Phase 3.3.8)
            provider=UnusualWhalesPriceActionProvider(
                client=client, settings=uw,
            ),
        ),
        IVExhaustionStage(  # M24 — two providers
            iv_provider=UnusualWhalesIVHistoryProvider(
                client=client, settings=uw,
            ),
            catalyst_provider=UnusualWhalesCatalystCalendarProvider(
                client=client, settings=uw,
            ),
        ),
        SectorPeerStage(  # M25 — two providers
            sector_provider=UnusualWhalesSectorMapProvider(
                client=client, settings=uw,
            ),
            peer_flow_provider=UnusualWhalesPeerFlowProvider(
                client=client, settings=uw,
            ),
        ),
        DarkPoolStage(  # M26
            provider=UnusualWhalesDarkPoolProvider(
                client=client, settings=uw,
            ),
        ),
        OpeningClosingStage(  # M27
            provider=UnusualWhalesOpenInterestProvider(
                client=client, settings=uw,
            ),
        ),
        TemporalClusterStage(),  # M38 — must precede ClusterDecayStage
        ClusterDecayStage(),  # event-time decay check
    ]
