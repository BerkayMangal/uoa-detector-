"""Module 23 — Price confirmation score.

Phase 3.4.3: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.3.

Hypothesis (acceptance doc): option flow without underlying price
confirmation is suspicious (could be hedge, dealer flow, or stale
signal). Strong directional spot move alongside option print =
high conviction.

Provider:
  - ``PriceActionProvider.get_intraday_price_movement`` (extended
    in Phase 3.4.3.2) returns a signed ``move_pct`` over the
    requested lookback window.

Score branches (acceptance doc):
  - 1.0 — call + spot up by confirmation_pct, OR
          put + spot down by confirmation_pct (CONFIRMED)
  - 0.3 — opposite direction, |move| > confirmation_pct
          (CONTRARIAN — could be hedge / dealer flow)
  - 0.5 — no clear signal either way (NEUTRAL)
  - 0.5 — provider returned None (NEUTRAL fallback per
          acceptance doc edge case 'Spot data missing →
          price_confirmation_score = 0.5 (neutral)')

Cross-cutting acceptance pinned (same as M21 / M22):
  - Provider injection via constructor
  - Idempotency-on-preset
  - asyncio.wait_for on provider call
  - last_execution_metadata exposed for orchestrator telemetry

decision (session-boundary clamp via _compute_effective_lookback):
  Acceptance doc edge case: 'Lookback crosses session boundary →
  reduce lookback to session start'. M23 stage owns the clamp
  policy: if (event_ts - lookback) is before session_open on
  the same day, reduce lookback so window starts at session_open.
  The provider receives the clamped lookback. The actual lookback
  is reported back via PriceMovement.lookback_minutes_actual.

decision (session_open_utc lives in profile, not hardcoded):
  Same rationale as M21's extreme_distance_pct (3.4.1.3): every
  number in profile per cross-cutting rule. Operator overrides
  for non-DST half of year (14:30 UTC default = 09:30 ET DST;
  override to 14:30 UTC for non-DST = 09:30 ET Standard. Same
  number? No — Standard 09:30 ET = 14:30 UTC; DST 09:30 ET =
  13:30 UTC). Profile default 14:30 UTC matches the Standard
  Time half of the year.

decision (event timestamp BEFORE session open: skip the clamp):
  If event_ts is before session_open (pre-market), the requested
  lookback is used as-is. This is intentional: pre-market hours
  have legitimate data on some sources; we don't want to clamp
  to a session start that's IN THE FUTURE relative to event_ts.
  Pinned by test.

decision (event timestamp on a different DATE than the lookback's
session_open):
  When event_ts crosses midnight (rare for US RTH but possible
  for futures / non-US tickers), we compute session_open on
  event_ts.date(), not on the lookback start's date. This means
  the clamp is anchored to the EVENT'S session, which is what
  matters for confirmation. Pre-market events crossing midnight
  are an edge case; if the clamp would result in a negative
  lookback, we skip the clamp.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from uoa_detector.providers.price_action import (
    NoOpPriceActionProvider,
    PriceMovement,
)

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import M23Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.price_action import PriceActionProvider


_logger = logging.getLogger(__name__)


class PriceConfirmationStage:
    """Module 23 — flow must be confirmed by price action."""

    name = "m23_price_confirmation"

    def __init__(
        self,
        provider: PriceActionProvider | None = None,
    ) -> None:
        self._provider: PriceActionProvider = (
            provider or NoOpPriceActionProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        if event.price_confirmation_score is not None:
            self.last_execution_metadata = {"branch": "preset_skip"}
            return event

        m23 = ctx.profile.scoring.modules.m23
        ticker = event.print_.ticker
        event_ts = event.print_.timestamp
        option_type = event.print_.option_type

        # Apply session-boundary clamp
        effective_lookback = _compute_effective_lookback(
            event_ts=event_ts,
            requested_lookback=m23.lookback_minutes,
            session_open_hour=m23.session_open_utc_hour,
            session_open_minute=m23.session_open_utc_minute,
        )

        try:
            movement = await asyncio.wait_for(
                self._provider.get_intraday_price_movement(
                    ticker, event_ts, effective_lookback,
                ),
                timeout=m23.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m23: provider timeout for %s; emitting neutral score",
                ticker,
            )
            event.price_confirmation_score = m23.neutral_score
            self.last_execution_metadata = {
                "branch": "timeout",
                "provider_returned": "no",
                "lookback_minutes_used": str(effective_lookback),
            }
            return event

        if movement is None:
            _logger.info(
                "m23: no spot data for %s; using neutral score",
                ticker,
            )
            event.price_confirmation_score = m23.neutral_score
            self.last_execution_metadata = {
                "branch": "data_missing_neutral",
                "provider_returned": "no",
                "lookback_minutes_used": str(effective_lookback),
            }
            return event

        score, branch = _score_from_movement(
            movement=movement,
            option_type=option_type,
            settings=m23,
        )
        event.price_confirmation_score = score
        self.last_execution_metadata = {
            "branch": branch,
            "provider_returned": "yes",
            "lookback_minutes_used": str(movement.lookback_minutes_actual),
            "move_pct": f"{movement.move_pct:.6f}",
        }
        return event


def _compute_effective_lookback(
    *,
    event_ts: datetime,
    requested_lookback: int,
    session_open_hour: int,
    session_open_minute: int,
) -> int:
    """Clamp the lookback so the window doesn't cross session_open.

    Pure function — directly unit-testable.

    Returns the effective lookback in minutes. If event_ts is at or
    after session_open AND (event_ts - lookback) would be before
    session_open, returns the smaller lookback that lands exactly
    at session_open. Otherwise returns the requested lookback.

    Pre-market events (event_ts before session_open) are NOT
    clamped — there's no past-session boundary to clamp to.
    """
    session_open_today = event_ts.replace(
        hour=session_open_hour,
        minute=session_open_minute,
        second=0,
        microsecond=0,
    )
    if event_ts < session_open_today:
        # Pre-market: don't clamp; provider may have or lack data
        return requested_lookback

    # Compute requested window start
    delta_minutes = requested_lookback
    minutes_to_open = (
        event_ts - session_open_today
    ).total_seconds() / 60.0

    if delta_minutes <= minutes_to_open:
        # Window stays inside session
        return requested_lookback

    # Clamp to session_open
    clamped = int(minutes_to_open)
    return max(clamped, 1)  # at least 1 minute


def _score_from_movement(
    *,
    movement: PriceMovement,
    option_type: str,
    settings: M23Settings,
) -> tuple[float, str]:
    """Compute the M23 score from a PriceMovement.

    Returns (score, branch_label). Pure function.

    Branch logic (acceptance doc):
      - For call: positive move > confirmation_pct = confirmed (1.0)
                  negative move > confirmation_pct = contrarian (0.3)
                  else neutral (0.5)
      - For put: NEGATIVE move > confirmation_pct = confirmed (1.0)
                 POSITIVE move > confirmation_pct = contrarian (0.3)
                 else neutral (0.5)
    """
    threshold = settings.confirmation_pct
    move = movement.move_pct

    if option_type == "call":
        if move > threshold:
            return settings.confirmed_score, "call_confirmed"
        if move < -threshold:
            return settings.contrarian_score, "call_contrarian"
        return settings.neutral_score, "neutral"

    if option_type == "put":
        if move < -threshold:
            return settings.confirmed_score, "put_confirmed"
        if move > threshold:
            return settings.contrarian_score, "put_contrarian"
        return settings.neutral_score, "neutral"

    # Unknown option_type (defensive — shouldn't happen given
    # OptionType Literal in domain layer)
    return settings.neutral_score, "neutral_unknown_type"
