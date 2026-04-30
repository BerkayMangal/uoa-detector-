"""Combined score formula (v5, Part 3 of the spec).

::

    combined_score =
        0.30 * uoa_score
      + 0.25 * convexity_score      ← scaled by DTE multiplier (Module 35)
      + 0.15 * event_score
      + 0.10 * gamma_score          ← scaled by DTE multiplier (Module 35)
      + 0.10 * price_confirmation_score
      + 0.05 * sector_confirmation_score
      + 0.05 * time_of_day_weight
      − contradiction_penalty       ← see note below
      − sum(applicable_penalties)   ← Module 36 deductions

**Contradiction-penalty note (Phase 1 decision, flagged in summary).**
The v5 spec lists the contradiction penalty in TWO places: Part 3's formula
and Module 36's penalty table. We apply the −0.15 deduction exactly once,
through the Module 36 penalty list, and also surface
``event.contradiction_penalty_applied = True`` for telemetry. There is no
additional separate ``- contradiction_penalty`` term, to avoid
double-counting.

The DTE multiplier (Module 35) is applied here, not in the M35 stage —
the spec restricts it to convexity_score and gamma_score only.
"""

from __future__ import annotations

from uoa_detector.config import AppConfig, default_config
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.errors import MissingSubScoreError, ScoreOutOfRangeError


def _get_score(event: EnrichedEvent, name: str, *, allow_above_one: bool = False) -> float:
    """Read a sub-score off the event with strict validation.

    Sub-scores are required to be in ``[0.0, 1.0]``. When the DTE multiplier
    has been applied (convexity, gamma) the post-multiplier value can exceed
    1.0; pass ``allow_above_one=True`` for those.
    """
    val: float | None = getattr(event, name)
    if val is None:
        raise MissingSubScoreError(name)
    if val < 0.0:
        raise ScoreOutOfRangeError(name, val)
    if not allow_above_one and val > 1.0:
        raise ScoreOutOfRangeError(name, val)
    return val


def compute_combined_score(
    event: EnrichedEvent,
    config: AppConfig | None = None,
) -> tuple[float, float]:
    """Compute the v5 combined score, both pre- and post-penalty.

    Mutates ``event`` in place: ``combined_score_pre_penalty`` and
    ``combined_score_post_penalty`` are set. Sub-scores must already be
    populated by the pipeline; this function does not invent values.

    Returns ``(pre_penalty, post_penalty)`` for caller convenience.
    """
    cfg = config or default_config()
    w = cfg.weights

    # Required sub-scores; raise if any are missing.
    uoa = _get_score(event, "uoa_score")
    convexity = _get_score(event, "convexity_score")
    event_s = _get_score(event, "event_score")
    gamma = _get_score(event, "gamma_score")
    price = _get_score(event, "price_confirmation_score")
    sector = _get_score(event, "sector_confirmation_score")
    tod = _get_score(event, "time_of_day_weight")

    # Apply the DTE multiplier to convexity and gamma only (Module 35).
    multiplier = event.dte_multiplier_applied
    if multiplier is None:
        raise MissingSubScoreError("dte_multiplier_applied")
    if multiplier <= 0.0:
        raise ScoreOutOfRangeError("dte_multiplier_applied", multiplier)
    convexity_adj = convexity * multiplier
    gamma_adj = gamma * multiplier

    pre = (
        w.uoa * uoa
        + w.convexity * convexity_adj
        + w.event * event_s
        + w.gamma * gamma_adj
        + w.price_confirmation * price
        + w.sector_confirmation * sector
        + w.time_of_day * tod
    )

    # Penalties are negative numbers already; add them.
    penalty_sum = sum(p.value for p in event.applied_penalties)
    post = pre + penalty_sum

    event.combined_score_pre_penalty = pre
    event.combined_score_post_penalty = post
    return pre, post


def compute_early_convexity_score(
    event: EnrichedEvent,
    config: AppConfig | None = None,
) -> float:
    """Compute the early-convexity score (used pre-UOA confirmation).

    Per spec Part 3:
        0.40 * convexity + 0.20 * cluster_density + 0.15 * event
        + 0.15 * gamma  + 0.10 * time_of_day
    """
    cfg = config or default_config()
    w = cfg.early_weights

    convexity = _get_score(event, "convexity_score")
    cluster = _get_score(event, "cluster_density_score")
    event_s = _get_score(event, "event_score")
    gamma = _get_score(event, "gamma_score")
    tod = _get_score(event, "time_of_day_weight")

    return (
        w.convexity * convexity
        + w.cluster_density * cluster
        + w.event * event_s
        + w.gamma * gamma
        + w.time_of_day * tod
    )
