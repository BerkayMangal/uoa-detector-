"""Module 26 — Dark pool corroboration.

Phase 3.4.6: replaces the Phase 2 stub with the real implementation
per acceptance doc §3.4.6.

Hypothesis (acceptance doc): large dark-pool equity prints near
the time of an option flow event indicate institutional
positioning. When DP direction matches option direction, the
options signal is corroborated; when no qualifying DP exists,
mildly low confidence (NOT zero — DP data is sparse by nature).

Provider:
  - ``DarkPoolPrintProvider.recent_prints`` (Phase 3.3.3.6) returns
    a sequence of ``DarkPoolPrint`` (price, size, side_estimate)
    over a lookback window.

Side estimate convention (acceptance doc):
  - 'at_or_below_bid' — bullish DP (buyers absorbing off-exchange)
  - 'above_ask'       — bearish DP (sellers distributing)
  - 'midpoint'        — direction unclear
  - 'unknown'         — direction unclear

Score branches (dark_pool_score; internal/telemetry):
  - 1.0 — qualifying DP + direction matches event option_type
  - 0.5 — qualifying DP but direction unclear (midpoint/unknown)
  - 0.2 — no qualifying prints in window (sparse-data fallback)
  - 0.5 — provider timeout (telemetry-flagged)

Side effect:
  event.has_dark_pool_confirmation = True iff
    dark_pool_score == confirmed_match_score (default 1.0)

Cross-cutting acceptance pinned (same as M21-M25):
  - Provider injection via constructor
  - Idempotency-on-preset
  - asyncio.wait_for on provider call
  - last_execution_metadata exposed for orchestrator telemetry

decision (idempotency uses has_dark_pool_confirmation == True):
  Unlike M21-M23/M25 (which have Optional[float] sub-score fields
  where None means 'not yet computed'), M26's domain field is
  bool with default False. So 'preset' = explicitly True. False
  is ambiguous (could be 'not run' OR 'ran and didn't confirm').
  Pragmatic interpretation: skip only on explicit preset
  (==True); otherwise run unconditionally. Same approach M24
  used (different mechanism — score_adjustments[source_module]
  there because M24 has no field).

decision (largest-by-notional wins on mixed directions):
  Acceptance doc edge case: 'Multiple prints with mixed
  directions → use largest by notional'. Notional = price * size.
  After filtering by min_print_size_usd, sort qualifying prints
  by notional descending; take the first. The largest print
  drives the score.

decision (filter then largest, not largest then filter):
  We filter qualifying (>= min_print_size_usd) FIRST, then take
  largest among qualifying. If we took largest first then
  filtered, a single $1M print would be picked even with
  qualifying $5M prints in the window.

decision (delay warning is telemetry-only, not score branch):
  Acceptance doc edge case: 'DP data delayed > 5 min relative
  to event → log warning, use anyway'. The warning fires when
  the LATEST qualifying print's timestamp is older than
  delay_warning_minutes from event_ts. Score is unchanged;
  warning surfaces in last_execution_metadata + log.

decision (sets has_dark_pool_confirmation only on confirmed_match):
  The boolean field flips True only when score reaches
  confirmed_match_score (default 1.0). Direction-unclear (0.5)
  is not 'confirmation' — it's 'we saw a print but can't say'.
  Pinned by tests.

decision (option_type → expected DP side mapping):
  call signal expects 'at_or_below_bid' DP (bullish absorption)
  put signal expects 'above_ask' DP (bearish distribution)
  Same convention as M25's bullish/bearish mapping but in
  DP-side-classification vocabulary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from uoa_detector.providers.dark_pool import (
    DarkPoolPrint,
    NoOpDarkPoolPrintProvider,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.calibration.profile import M26Settings
    from uoa_detector.domain.events import EnrichedEvent
    from uoa_detector.pipeline.stage import PipelineContext
    from uoa_detector.providers.dark_pool import DarkPoolPrintProvider


_logger = logging.getLogger(__name__)


# Map option_type → expected DP side_estimate for confirmation
_DIRECTION_MAP: dict[str, str] = {
    "call": "at_or_below_bid",  # bullish absorption
    "put": "above_ask",          # bearish distribution
}


class DarkPoolStage:
    """Module 26 — corroborates options flow with dark-pool prints."""

    name = "m26_dark_pool"

    def __init__(
        self,
        provider: DarkPoolPrintProvider | None = None,
    ) -> None:
        self._provider: DarkPoolPrintProvider = (
            provider or NoOpDarkPoolPrintProvider()
        )
        self.last_execution_metadata: dict[str, str] | None = None

    async def enrich(
        self, event: EnrichedEvent, ctx: PipelineContext,
    ) -> EnrichedEvent:
        # Idempotency-on-preset: True means M26 already confirmed.
        # False is ambiguous (not run / ran without confirm); run.
        if event.has_dark_pool_confirmation:
            self.last_execution_metadata = {"branch": "preset_skip"}
            return event

        m26 = ctx.profile.scoring.modules.m26
        ticker = event.print_.ticker
        event_ts = event.print_.timestamp
        option_type = event.print_.option_type

        try:
            prints = await asyncio.wait_for(
                self._provider.recent_prints(
                    ticker=ticker,
                    before=event_ts,
                    window=timedelta(
                        minutes=m26.dark_pool_lookback_minutes,
                    ),
                ),
                timeout=m26.provider_timeout_s,
            )
        except TimeoutError:
            _logger.warning(
                "m26: provider timeout for %s; using neutral score",
                ticker,
            )
            self.last_execution_metadata = {
                "branch": "timeout",
                "provider_returned": "no",
            }
            return event

        score, branch, qualifying_count, largest = _score_from_prints(
            prints=prints,
            option_type=option_type,
            settings=m26,
        )

        # Set has_dark_pool_confirmation if score == confirmed_match_score
        if score == m26.confirmed_match_score:
            event.has_dark_pool_confirmation = True

        # Delay warning telemetry (acceptance doc edge case)
        delay_warning = "no"
        if largest is not None:
            delay = (event_ts - largest.when).total_seconds() / 60.0
            if delay > m26.delay_warning_minutes:
                _logger.warning(
                    "m26: largest qualifying DP print for %s is %.1f "
                    "minutes old (> %d delay threshold); using anyway",
                    ticker, delay, m26.delay_warning_minutes,
                )
                delay_warning = "yes"

        self.last_execution_metadata = {
            "branch": branch,
            "provider_returned": "yes" if prints else "empty",
            "qualifying_print_count": str(qualifying_count),
            "delay_warning_fired": delay_warning,
        }
        if largest is not None:
            notional = int(largest.price) * largest.size
            self.last_execution_metadata["largest_notional_usd"] = str(notional)
            self.last_execution_metadata["largest_side_estimate"] = (
                largest.side_estimate
            )
        return event


def _score_from_prints(
    *,
    prints: Sequence[DarkPoolPrint],
    option_type: str,
    settings: M26Settings,
) -> tuple[float, str, int, DarkPoolPrint | None]:
    """Compute M26 score from a sequence of DP prints.

    Returns (score, branch_label, qualifying_count, largest_or_None).
    Pure function — directly unit-testable.

    Algorithm:
      1. Filter prints with notional (price * size) >= min_print_size_usd
      2. If empty → no_qualifying_prints
      3. Sort by notional descending; take largest
      4. Compare largest.side_estimate to expected direction:
         call → 'at_or_below_bid'; put → 'above_ask'
         match → confirmed_match_score (1.0)
         mismatch (above_ask for call, at_or_below_bid for put) →
                  no_qualifying_prints_score (treat as no qualifying)
                  — directional MISMATCH is its own signal but
                  acceptance doc only has 3 score branches; the
                  no_qualifying fallback is the closest match
         midpoint / unknown → direction_unclear_score (0.5)
      5. Unknown option_type → direction_unclear_score (defensive)
    """
    qualifying = [
        p for p in prints
        if int(p.price) * p.size >= settings.min_print_size_usd
    ]
    if not qualifying:
        return (
            settings.no_qualifying_prints_score,
            "no_qualifying_prints",
            0,
            None,
        )

    # Sort by notional descending, take largest
    qualifying_sorted = sorted(
        qualifying,
        key=lambda p: int(p.price) * p.size,
        reverse=True,
    )
    largest = qualifying_sorted[0]

    expected_side = _DIRECTION_MAP.get(option_type)
    if expected_side is None:
        # Unknown option_type — defensive, score as direction unclear
        return (
            settings.direction_unclear_score,
            "unknown_option_type",
            len(qualifying),
            largest,
        )

    if largest.side_estimate == expected_side:
        return (
            settings.confirmed_match_score,
            "confirmed_match",
            len(qualifying),
            largest,
        )
    if largest.side_estimate in ("midpoint", "unknown"):
        return (
            settings.direction_unclear_score,
            "direction_unclear",
            len(qualifying),
            largest,
        )
    # Direction MISMATCH (e.g., call signal but largest DP is above_ask).
    # Treat as no-qualifying-prints score — the print exists but
    # actively counter-signals the option flow. Acceptance doc has 3
    # branches and doesn't explicitly call out mismatch; we map it to
    # the lowest-score branch (no_qualifying_prints_score = 0.2).
    return (
        settings.no_qualifying_prints_score,
        "direction_mismatch",
        len(qualifying),
        largest,
    )
