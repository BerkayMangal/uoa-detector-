"""Module 22 — Event calendar score.

Phase 3.4.2: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.2.

Hypothesis (acceptance doc): pre-event option flow (earnings, FDA,
Fed) carries information; post-event flow is noise. M22 down-
weights signals near recent catalysts and up-weights signals before
scheduled ones. The KEY DTE-versus-catalyst test: an option that
EXPIRES BEFORE the catalyst is a speculative position (lower
score); an option that SURVIVES the catalyst is a genuine
information-trade (full score).

Provider:
  - ``CatalystCalendarProvider.catalysts_in_window`` (extended in
    Phase 3.4.2.2) returns events within ±N days of event time.

Score branches (acceptance doc):
  - 0.0 — catalyst within post_event_blackout_days (just happened
          OR same day)
  - 1.0 — catalyst within pre_event_window_days AND DTE >
          days_to_catalyst (option survives the event)
  - 0.5 — catalyst in pre-event window but DTE < days_to_catalyst
          (option expires before catalyst — speculative position)
  - 0.3 — no catalyst in either window (NEUTRAL, not zero)

Cross-cutting acceptance pinned (same as M21):
  - Provider injection via constructor
  - Idempotency-on-preset
  - asyncio.wait_for on provider call (timeout = profile)
  - last_execution_metadata exposed for orchestrator telemetry copy

decision (closest-catalyst priority on overlap):
  Acceptance doc edge case: 'Multiple catalysts overlap → use
  closest in time'. After classification (post-blackout vs pre-
  window vs no-catalyst) the CLOSEST event drives the branch;
  other events in the window are irrelevant once the closest is
  classified.

decision (post-event takes precedence over pre-event when both
match):
  Acceptance doc edge case: 'Catalyst on same day as event → treat
  as post-event blackout'. The same logic generalises: when a
  catalyst is within blackout AND another is within pre-event
  window, the past catalyst's IV crush + recent-noise risk
  dominates; we report post-event blackout. Pinned by tests.

decision (window math uses calendar days, not trading days):
  Acceptance doc literal 'days' (not 'sessions'). Calendar days
  are stable across timezones and don't need a market-calendar
  lookup. Phase 3.4 doesn't ship a trading-calendar provider;
  Phase 3.4.9 closeout may revisit if the difference matters.

decision (DTE comparison: option.expiry vs catalyst.when in
calendar days):
  EnrichedEvent.print_.expiry is a date; catalyst.when is a
  datetime. We compare dates: if expiry > catalyst-date, the
  option survives. Same-date expiry (expiry == catalyst-date) is
  treated as 'expires before' because options stop trading at
  catalyst announcement time (almost always after-hours), so the
  position is functionally closed before the catalyst impact
  lands.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from uoa_detector.providers.catalyst_calendar import (
    CatalystEvent,
    NoOpCatalystCalendarProvider,
)

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import M22Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.catalyst_calendar import (
        CatalystCalendarProvider,
    )


_logger = logging.getLogger(__name__)


class EventCalendarStage:
    """Module 22 — checks every signal against catalyst timing."""

    name = "m22_event_calendar"

    def __init__(
        self,
        provider: CatalystCalendarProvider | None = None,
    ) -> None:
        """Construct with an injected provider.

        Default is the NoOp provider (returns empty tuple) so
        existing pipelines without a real provider get the
        no_catalyst_neutral_score (0.3 default).
        """
        self._provider: CatalystCalendarProvider = (
            provider or NoOpCatalystCalendarProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Idempotency-on-preset (acceptance rule #5)
        if event.event_score is not None:
            self.last_execution_metadata = {"branch": "preset_skip"}
            return event

        m22 = ctx.profile.scoring.modules.m22
        ticker = event.print_.ticker
        event_ts = event.print_.timestamp
        expiry = event.print_.expiry

        # Window: from -post_event_blackout_days to +pre_event_window_days
        window_start = event_ts - timedelta(days=m22.post_event_blackout_days)
        window_end = event_ts + timedelta(days=m22.pre_event_window_days)

        try:
            catalysts = await asyncio.wait_for(
                self._provider.catalysts_in_window(
                    ticker, window_start, window_end,
                ),
                timeout=m22.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m22: provider timeout for %s; emitting zero score",
                ticker,
            )
            event.event_score = 0.0
            self.last_execution_metadata = {
                "branch": "timeout",
                "provider_returned": "no",
            }
            return event

        score, branch = _score_from_catalysts(
            catalysts=catalysts,
            event_ts=event_ts,
            expiry=expiry,
            settings=m22,
        )
        event.event_score = score
        self.last_execution_metadata = {
            "branch": branch,
            "provider_returned": "yes" if catalysts else "empty",
            "catalysts_count": str(len(catalysts)),
        }
        return event


def _score_from_catalysts(
    *,
    catalysts: tuple[CatalystEvent, ...],
    event_ts: datetime,
    expiry: date,
    settings: M22Settings,
) -> tuple[float, str]:
    """Compute the M22 score from a window of catalyst events.

    Returns (score, branch_label). Pure function — directly unit-
    testable without async setup.

    Decision flow (acceptance doc):
      1. If empty window  → no_catalyst_neutral_score
                            (branch=no_catalyst)
      2. Find catalyst within post-event blackout
                          → post_event_score
                            (branch=post_event_blackout)
         (Post-event takes precedence over pre-event when both apply.)
      3. Find closest future catalyst in pre-event window:
         - DTE > days_to_catalyst → dte_survives_score
                                    (branch=pre_event_dte_survives)
         - DTE <= days_to_catalyst → dte_expires_before_score
                                     (branch=pre_event_dte_expires_before)
      4. Defensive fallback (shouldn't happen given window
         construction, but be safe):
                          → no_catalyst_neutral_score
                            (branch=neutral_fallback)
    """
    if not catalysts:
        return settings.no_catalyst_neutral_score, "no_catalyst"

    event_date = event_ts.date()
    blackout_days = settings.post_event_blackout_days

    # --- Pass 1: post-event blackout has priority ---
    # A catalyst is in the post-event blackout if:
    #   catalyst.when.date() in [event_date - blackout_days, event_date]
    blackout_floor = event_date - timedelta(days=blackout_days)
    post_event_catalysts = [
        c for c in catalysts
        if blackout_floor <= c.when.date() <= event_date
    ]
    if post_event_catalysts:
        return settings.post_event_score, "post_event_blackout"

    # --- Pass 2: pre-event window (closest future catalyst) ---
    future_catalysts = [c for c in catalysts if c.when.date() > event_date]
    if not future_catalysts:
        # Defensive fallback (shouldn't happen given window covers
        # ±N days, but be safe if window math is misconfigured)
        return settings.no_catalyst_neutral_score, "neutral_fallback"

    closest = min(future_catalysts, key=lambda c: c.when)
    catalyst_date = closest.when.date()
    if expiry > catalyst_date:
        return settings.dte_survives_score, "pre_event_dte_survives"
    return settings.dte_expires_before_score, "pre_event_dte_expires_before"
