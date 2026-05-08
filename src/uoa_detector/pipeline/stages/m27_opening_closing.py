"""Module 27 — Opening/closing OI delta.

Phase 3.4.7: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.7.

Hypothesis (acceptance doc): when current OI is significantly
higher than prior session's close, the option is being newly
OPENED (institutional positioning); when current is below prior,
it's being CLOSED (unwinding existing). Opening flow carries
fresh information; closing flow is pre-existing positioning
being released.

Provider:
  - ``OpenInterestProvider.at(ticker, strike, expiry, option_type, when)``
    (Phase 3.3.3) called twice:
      1. when = prior_session_close (yesterday's 16:00 ET)
      2. when = event_ts (intraday estimate)

Score branches (acceptance doc; opening_closing_score; internal/telemetry):
  - 1.0 — oi_delta_pct > strong_opening_threshold (default 0.5)
  - 0.7 — moderate < oi_delta_pct <= strong (default 0.1-0.5)
  - 0.5 — closing <= oi_delta_pct <= moderate (default -0.1..0.1)
  - 0.0 — oi_delta_pct < closing_threshold (default <-0.1)
  - 1.0 — new strike (prior_oi = 0; opening by definition)
  - 0.5 — provider timeout / no current data

Cross-cutting acceptance pinned (same as M21-M26):
  - Provider injection via constructor
  - asyncio.wait_for on each provider call
  - last_execution_metadata exposed for orchestrator telemetry

decision (no idempotency-on-preset; M27 has no sub-score field):
  Unlike M21-M23/M25 (Optional[float] fields with None=not run),
  M27 has NO domain field for its score. Like M24 (no iv_score)
  and M26 (bool flag with default False), M27's score is
  internal/telemetry only. Idempotency is a no-op concern: M27
  always runs when called. The orchestrator already calls each
  stage exactly once per event, so idempotency-on-preset is
  defense against double-calls within a pipeline pass — not
  relevant here because M27's effects are entirely contained
  in last_execution_metadata. Documented as judgment call.

decision (prior session close = yesterday at session_close_utc_hour:00):
  Naive single-day-back computation. Doesn't handle weekends or
  holidays — if event is Monday, prior_close is Sunday at
  21:00 UTC (which has no OI data; provider returns None →
  no_data_score). Trading-calendar overlay deferred to Phase
  3.4.9 closeout (same pattern as M23).

decision (parallel fetch via asyncio.gather + wait_for):
  Both prior and current OI are independent fetches; we fan out
  in parallel under the timeout budget. If either times out,
  treat as timeout branch. Faster than sequential fetches and
  matches user's intent of 2.0s total budget.

decision (stale OI warning is telemetry-only):
  Acceptance doc edge case: 'Stale OI (provider returns same
  value) → log warning, use anyway'. When prior_oi == current_oi
  (and both > 0), log a warning. Score is computed normally
  (delta = 0% → neutral branch); warning surfaces in
  last_execution_metadata.

decision (prior_oi = 0 → new_strike branch, NOT division by zero):
  Acceptance doc: 'New strike/expiry — no prior OI → treat as
  opening'. Check prior_oi == 0 BEFORE division; map directly
  to new_strike_score (default 1.0). Pinned.

decision (current_oi None → no_data branch, distinct from timeout):
  If provider returns None for the current OI fetch (success
  but no data — illiquid contract, lag), score is no_data_score
  (default 0.5 same as timeout). Distinct branch label for
  Phase 3.5 cost auditing.

decision (prior_oi None → also no_data, NOT new_strike):
  Provider returning None for the prior fetch means we don't
  know prior OI — could be a new strike OR a data gap. We can't
  distinguish, so default to no_data (neutral) rather than
  new_strike (1.0). This is conservative: only TRULY zero-OI
  prior contracts get the opening bonus, not data-availability
  artifacts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from uoa_detector.providers.open_interest import NoOpOpenInterestProvider

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import M27Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.open_interest import (
        OpenInterestProvider,
        OpenInterestSnapshot,
    )


_logger = logging.getLogger(__name__)


class OpeningClosingStage:
    """Module 27 — opening vs closing OI delta scoring."""

    name = "m27_opening_closing"

    def __init__(
        self,
        provider: OpenInterestProvider | None = None,
    ) -> None:
        self._provider: OpenInterestProvider = (
            provider or NoOpOpenInterestProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        m27 = ctx.profile.scoring.modules.m27
        ticker = event.print_.ticker
        event_ts = event.print_.timestamp

        # Compute prior session close timestamp
        prior_close_ts = _compute_prior_session_close(
            event_ts=event_ts,
            session_close_hour=m27.session_close_utc_hour,
            session_close_minute=m27.session_close_utc_minute,
        )

        # Parallel fetch under timeout budget
        try:
            prior_snapshot, current_snapshot = await asyncio.wait_for(
                asyncio.gather(
                    self._provider.at(
                        ticker=ticker,
                        strike=event.print_.strike,
                        expiry=event.print_.expiry,
                        option_type=event.print_.option_type,
                        when=prior_close_ts,
                    ),
                    self._provider.at(
                        ticker=ticker,
                        strike=event.print_.strike,
                        expiry=event.print_.expiry,
                        option_type=event.print_.option_type,
                        when=event_ts,
                    ),
                ),
                timeout=m27.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m27: provider timeout for %s; using neutral score",
                ticker,
            )
            self.last_execution_metadata = {
                "branch": "timeout",
                "provider_returned": "no",
            }
            return event

        _score, branch, oi_delta_pct, prior_oi, current_oi = (
            _score_from_oi_snapshots(
                prior=prior_snapshot,
                current=current_snapshot,
                settings=m27,
            )
        )

        # Stale OI warning (telemetry-only)
        stale_warning = "no"
        if (
            prior_snapshot is not None
            and current_snapshot is not None
            and prior_oi is not None
            and current_oi is not None
            and prior_oi > 0
            and prior_oi == current_oi
        ):
            _logger.warning(
                "m27: stale OI for %s (prior == current = %d); "
                "using anyway",
                ticker, prior_oi,
            )
            stale_warning = "yes"

        self.last_execution_metadata = {
            "branch": branch,
            "provider_returned": (
                "yes" if (prior_snapshot or current_snapshot) else "no"
            ),
            "prior_oi": str(prior_oi) if prior_oi is not None else "n/a",
            "current_oi": (
                str(current_oi) if current_oi is not None else "n/a"
            ),
            "oi_delta_pct": (
                f"{oi_delta_pct:.6f}" if oi_delta_pct is not None else "n/a"
            ),
            "stale_warning_fired": stale_warning,
        }
        return event


def _compute_prior_session_close(
    *,
    event_ts: datetime,
    session_close_hour: int,
    session_close_minute: int,
) -> datetime:
    """Compute the prior session close timestamp.

    Pure function. Returns event_ts.date() - 1 day at
    session_close_hour:session_close_minute UTC. Doesn't handle
    weekends or holidays — provider returns None for those
    timestamps, which the stage maps to no_data_score branch.

    Trading-calendar overlay: deferred to Phase 3.4.9 closeout.
    """
    yesterday = event_ts - timedelta(days=1)
    return yesterday.replace(
        hour=session_close_hour,
        minute=session_close_minute,
        second=0,
        microsecond=0,
    )


def _score_from_oi_snapshots(
    *,
    prior: OpenInterestSnapshot | None,
    current: OpenInterestSnapshot | None,
    settings: M27Settings,
) -> tuple[float, str, float | None, int | None, int | None]:
    """Compute M27 score from prior + current OI snapshots.

    Returns (score, branch_label, oi_delta_pct_or_None,
             prior_oi_or_None, current_oi_or_None).
    Pure function — directly unit-testable.

    Algorithm:
      1. Current is None → no_data_score
      2. Prior is None → no_data_score (defensive: don't assume
         new_strike on data-availability artifact)
      3. Prior == 0 → new_strike_score (genuine new strike)
      4. Compute oi_delta_pct = (current - prior) / prior
      5. Map to score branch by threshold comparison
    """
    if current is None:
        prior_oi = prior.open_interest if prior else None
        return settings.no_data_score, "no_current_data", None, prior_oi, None

    current_oi = current.open_interest

    if prior is None:
        return (
            settings.no_data_score,
            "no_prior_data",
            None,
            None,
            current_oi,
        )

    prior_oi = prior.open_interest

    # New strike (prior OI is genuinely zero)
    if prior_oi == 0:
        return (
            settings.new_strike_score,
            "new_strike",
            None,
            prior_oi,
            current_oi,
        )

    oi_delta_pct = (current_oi - prior_oi) / prior_oi

    # Branch dispatch
    if oi_delta_pct > settings.strong_opening_threshold:
        return (
            settings.strong_opening_score,
            "strong_opening",
            oi_delta_pct,
            prior_oi,
            current_oi,
        )
    if oi_delta_pct > settings.moderate_opening_threshold:
        return (
            settings.moderate_opening_score,
            "moderate_opening",
            oi_delta_pct,
            prior_oi,
            current_oi,
        )
    if oi_delta_pct < settings.closing_threshold:
        return (
            settings.closing_score,
            "closing",
            oi_delta_pct,
            prior_oi,
            current_oi,
        )
    return (
        settings.neutral_score,
        "neutral",
        oi_delta_pct,
        prior_oi,
        current_oi,
    )
