"""Module 25 — Sector peer score.

Phase 3.4.5: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.5.

Hypothesis (acceptance doc): when peer tickers in the same sector
show same-direction unusual flow within a short window, the signal
carries sector-wide information; conviction is higher. A signal
alone in its sector is more likely idiosyncratic noise.

This is the FIRST M-module that uses TWO providers with DIFFERENT
roles:
  - SectorMapProvider: ticker → peers (reference data, slow)
  - PeerFlowProvider: recent flow on peer tickers (intraday)

(M24 also used two but both were UW; here the two have distinct
responsibilities — sector map is reference, peer flow is event
stream.)

Score branches (acceptance doc; sector_confirmation_score):
  - 1.0 — alignment > strong_alignment_threshold
  - 0.7 — alignment in [moderate, strong)
  - 0.3 — alignment in [weak, moderate)
  - 0.0 — alignment < weak (sector contrarian)
  - 0.5 — no sector mapping (fallback)
  - 0.5 — empty peer flow (small sector / quiet period)
  - 0.5 — provider timeout (telemetry-flagged)

Cross-cutting acceptance pinned (same as M21-M24):
  - Provider injection via constructor (TWO providers)
  - Idempotency-on-preset (uses sector_confirmation_score field)
  - asyncio.wait_for on each provider call (timeout = profile)
  - last_execution_metadata exposed for orchestrator telemetry

decision (peer selection: take first peer_count from peers_of):
  SectorMapProvider returns peers in implementation-defined
  order. M25 takes the first N (peer_count from profile). The
  Protocol contract is 'peers in same sector excluding self';
  M25 trusts the provider's ordering. Operators wanting strict
  ordering (e.g., by market cap) configure that at the provider
  level, not in M25.

decision (small sector — use what's available):
  Acceptance doc edge case: 'Small sector (< 5 peers) → use what's
  available, normalize by count'. If peers_of returns 3 peers
  for peer_count=5, M25 queries those 3. Alignment is computed
  as fraction of returned events, NOT capped at peer_count.
  Empty peer set → empty_peer_flow_score (0.5 default).

decision (alignment direction maps option_type → bullish/bearish):
  Event option_type is 'call' or 'put'. PeerFlowEvent.direction
  is 'bullish' / 'bearish' / 'neutral'. We map:
    - call → expected peer direction = 'bullish'
    - put → expected peer direction = 'bearish'
  Alignment % = same_direction_count / non_neutral_total
  Neutral peer events are excluded from the denominator (a
  neutral flow gives no directional information either way).

decision (alignment denominator excludes neutral events):
  If 5 peer events arrive — 2 bullish, 1 bearish, 2 neutral —
  alignment for a call signal = 2 / 3 = 0.67 (NOT 2/5 = 0.4).
  This matches the spirit of 'same direction confirmation' —
  neutral flow is not a counter-signal, just non-informative.
  Pinned by test.

decision (zero non-neutral events → empty_peer_flow_score):
  If all peer events are neutral, denominator is zero → can't
  compute alignment. Treat as empty peer flow (0.5 fallback).

decision (timeout on EITHER provider → timeout score):
  Acceptance doc: 'Provider timeout → 0.5 (neutral, telemetry
  "timeout")'. Either SectorMap timeout or PeerFlow timeout
  qualifies. Different telemetry sub-labels distinguish which
  one timed out.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from uoa_detector.providers.sector_map import (
    NoOpPeerFlowProvider,
    NoOpSectorMapProvider,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.calibration.profile import M25Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.sector_map import (
        PeerFlowEvent,
        PeerFlowProvider,
        SectorMapProvider,
    )


_logger = logging.getLogger(__name__)


class SectorPeerStage:
    """Module 25 — sector-peer flow confirmation.

    Two-provider stage: SectorMap for reference data, PeerFlow
    for the event stream that drives the alignment compute.
    """

    name = "m25_sector_peer"

    def __init__(
        self,
        sector_provider: SectorMapProvider | None = None,
        peer_flow_provider: PeerFlowProvider | None = None,
    ) -> None:
        self._sector_provider: SectorMapProvider = (
            sector_provider or NoOpSectorMapProvider()
        )
        self._peer_flow_provider: PeerFlowProvider = (
            peer_flow_provider or NoOpPeerFlowProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Idempotency-on-preset
        if event.sector_confirmation_score is not None:
            self.last_execution_metadata = {"branch": "preset_skip"}
            return event

        m25 = ctx.profile.scoring.modules.m25
        ticker = event.print_.ticker
        event_ts = event.print_.timestamp
        option_type = event.print_.option_type

        # Step 1: get peers via SectorMapProvider
        try:
            peers = await asyncio.wait_for(
                self._sector_provider.peers_of(ticker),
                timeout=m25.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m25: sector provider timeout for %s; using neutral score",
                ticker,
            )
            event.sector_confirmation_score = m25.timeout_score
            self.last_execution_metadata = {
                "branch": "timeout",
                "timeout_source": "sector_map",
            }
            return event

        if not peers:
            event.sector_confirmation_score = m25.no_sector_score
            self.last_execution_metadata = {
                "branch": "no_sector",
                "peer_count_returned": "0",
            }
            return event

        # Step 2: take top N peers
        top_peers = list(peers[:m25.peer_count])

        # Step 3: fetch peer flow
        try:
            peer_events = await asyncio.wait_for(
                self._peer_flow_provider.recent_flow(
                    tickers=top_peers,
                    before=event_ts,
                    window=timedelta(minutes=m25.peer_window_minutes),
                ),
                timeout=m25.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m25: peer flow timeout for %s; using neutral score",
                ticker,
            )
            event.sector_confirmation_score = m25.timeout_score
            self.last_execution_metadata = {
                "branch": "timeout",
                "timeout_source": "peer_flow",
                "peers_queried": str(len(top_peers)),
            }
            return event

        score, branch, alignment = _score_from_peer_flow(
            peer_events=peer_events,
            option_type=option_type,
            settings=m25,
        )
        event.sector_confirmation_score = score
        self.last_execution_metadata = {
            "branch": branch,
            "peers_queried": str(len(top_peers)),
            "peer_events_returned": str(len(peer_events)),
            "alignment_pct": (
                f"{alignment:.4f}" if alignment is not None else "n/a"
            ),
        }
        return event


def _score_from_peer_flow(
    *,
    peer_events: Sequence[PeerFlowEvent],
    option_type: str,
    settings: M25Settings,
) -> tuple[float, str, float | None]:
    """Compute the M25 score from peer flow events.

    Returns (score, branch_label, alignment_pct_or_none).
    Pure function — directly unit-testable.

    Algorithm:
      1. If peer_events is empty → empty_peer_flow_score
      2. Map option_type to expected peer direction:
         call → 'bullish'; put → 'bearish'; other → unknown
      3. Count non-neutral peer events; that's the denominator
      4. Count same-direction events; that's the numerator
      5. If denominator is zero → empty_peer_flow_score
      6. alignment_pct = numerator / denominator
      7. Map alignment to score branch
    """
    if not peer_events:
        return settings.empty_peer_flow_score, "empty_peer_flow", None

    # Map option_type to expected peer direction
    if option_type == "call":
        expected_direction = "bullish"
    elif option_type == "put":
        expected_direction = "bearish"
    else:
        # Unknown option_type — treat as no directional signal
        return (
            settings.empty_peer_flow_score,
            "unknown_option_type",
            None,
        )

    # Count non-neutral and same-direction events
    non_neutral = [e for e in peer_events if e.direction != "neutral"]
    if not non_neutral:
        return settings.empty_peer_flow_score, "all_neutral", None

    same_direction = [
        e for e in non_neutral if e.direction == expected_direction
    ]
    alignment = len(same_direction) / len(non_neutral)

    # Map alignment to score
    if alignment > settings.strong_alignment_threshold:
        return settings.strong_alignment_score, "strong", alignment
    if alignment >= settings.moderate_alignment_threshold:
        return settings.moderate_alignment_score, "moderate", alignment
    if alignment >= settings.weak_alignment_threshold:
        return settings.weak_alignment_score, "weak", alignment
    return settings.contrarian_score, "contrarian", alignment
