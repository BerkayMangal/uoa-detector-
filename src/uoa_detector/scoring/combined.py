"""Combined-score formula (v5, Part 3 of the spec) — Phase 2 calibrated.

Reads every weight, multiplier, and missing-data policy from the active
``CalibrationProfile``. Applies:
  - DTE multiplier (Module 35) to convexity and gamma sub-scores only
  - Score adjustments (Module 34's sweep bonuses) to their target sub-scores
  - Missing-sub-score policy: per-field, configurable in the profile

Contradiction-penalty note (Phase 1 decision, retained in Phase 2):
The contradiction is counted ONCE through the Module 36 penalty list
(``flow_contradicts_price``), with ``event.contradiction_penalty_applied``
set for telemetry. There is NO additional separate ``- contradiction_penalty``
term, to avoid double-counting.

Strict mode: pass ``strict=True`` to ``compute_combined_score`` to raise on
ANY ``None`` sub-score regardless of profile policy. Used by tests that need
to catch a stage bug rather than tolerate a data gap.
"""

from __future__ import annotations

from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.calibration.profile import SubScoreMissingPolicy
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.errors import MissingSubScoreError, ScoreOutOfRangeError


def _get_score(
    event: EnrichedEvent,
    name: str,
    profile: CalibrationProfile,
    *,
    strict: bool,
    allow_above_one: bool = False,
) -> float:
    """Read a sub-score with strict validation and per-profile missing-data policy."""
    val: float | None = getattr(event, name)
    if val is None:
        if strict:
            raise MissingSubScoreError(name)
        policy: SubScoreMissingPolicy = getattr(profile.sub_score_missing_behavior, name)
        if policy.on_missing == "raise":
            raise MissingSubScoreError(name)
        if name not in event.missing_sub_scores:
            event.missing_sub_scores.append(name)
        val = policy.default
    if val < 0.0:
        raise ScoreOutOfRangeError(name, val)
    if not allow_above_one and val > 1.0:
        raise ScoreOutOfRangeError(name, val)
    return val


def _adjustments_for(event: EnrichedEvent, target: str) -> float:
    """Sum of all score_adjustments targeting ``target``."""
    return sum(a.delta for a in event.score_adjustments if a.target == target)


def compute_combined_score(
    event: EnrichedEvent,
    profile: CalibrationProfile | None = None,
    *,
    strict: bool = False,
) -> tuple[float, float]:
    """Compute the v5 combined score, both pre- and post-penalty.

    Mutates ``event`` in place: ``combined_score_pre_penalty`` and
    ``combined_score_post_penalty`` are set; ``missing_sub_scores`` is updated
    when missing-sub-score policy substitutes a default.
    """
    p = profile or load_default_profile()
    w = p.scoring

    uoa = _get_score(event, "uoa_score", p, strict=strict)
    convexity = _get_score(event, "convexity_score", p, strict=strict)
    event_s = _get_score(event, "event_score", p, strict=strict)
    gamma = _get_score(event, "gamma_score", p, strict=strict)
    price = _get_score(event, "price_confirmation_score", p, strict=strict)
    sector = _get_score(event, "sector_confirmation_score", p, strict=strict)
    tod = _get_score(event, "time_of_day_weight", p, strict=strict)

    # Module 34-style additive adjustments — preserve original sub-score on
    # the event but use the adjusted value in the formula.
    uoa_adj = uoa + _adjustments_for(event, "uoa_score")

    multiplier = event.dte_multiplier_applied
    if multiplier is None:
        raise MissingSubScoreError("dte_multiplier_applied")
    if multiplier <= 0.0:
        raise ScoreOutOfRangeError("dte_multiplier_applied", multiplier)
    convexity_adj = convexity * multiplier
    gamma_adj = gamma * multiplier

    pre = (
        w.uoa * uoa_adj
        + w.convexity * convexity_adj
        + w.event * event_s
        + w.gamma * gamma_adj
        + w.price_confirmation * price
        + w.sector_confirmation * sector
        + w.time_of_day * tod
    )

    penalty_sum = sum(pen.value for pen in event.applied_penalties)
    post = pre + penalty_sum

    event.combined_score_pre_penalty = pre
    event.combined_score_post_penalty = post
    return pre, post


def compute_early_convexity_score(
    event: EnrichedEvent,
    profile: CalibrationProfile | None = None,
    *,
    strict: bool = False,
) -> float:
    """Compute the early-convexity score (used pre-UOA confirmation)."""
    p = profile or load_default_profile()
    w = p.early_scoring

    convexity = _get_score(event, "convexity_score", p, strict=strict)
    cluster = _get_score(event, "cluster_density_score", p, strict=strict)
    event_s = _get_score(event, "event_score", p, strict=strict)
    gamma = _get_score(event, "gamma_score", p, strict=strict)
    tod = _get_score(event, "time_of_day_weight", p, strict=strict)

    return (
        w.convexity * convexity
        + w.cluster_density * cluster
        + w.event * event_s
        + w.gamma * gamma
        + w.time_of_day * tod
    )


def score_breakdown(
    event: EnrichedEvent,
    profile: CalibrationProfile | None = None,
) -> dict[str, float]:
    """Return per-component weighted contributions for the decision record.

    Components sum to ``combined_score_pre_penalty``. Penalty totals are
    returned separately as ``penalty_<name>`` keys (negative).
    """
    p = profile or load_default_profile()
    w = p.scoring

    def _read(name: str) -> float:
        v = getattr(event, name)
        return 0.0 if v is None else float(v)

    multiplier = event.dte_multiplier_applied or 1.0
    uoa = _read("uoa_score") + _adjustments_for(event, "uoa_score")
    convexity = _read("convexity_score") * multiplier
    event_s = _read("event_score")
    gamma = _read("gamma_score") * multiplier
    price = _read("price_confirmation_score")
    sector = _read("sector_confirmation_score")
    tod = _read("time_of_day_weight")

    breakdown: dict[str, float] = {
        "uoa": w.uoa * uoa,
        "convexity": w.convexity * convexity,
        "event": w.event * event_s,
        "gamma": w.gamma * gamma,
        "price_confirmation": w.price_confirmation * price,
        "sector_confirmation": w.sector_confirmation * sector,
        "time_of_day": w.time_of_day * tod,
    }
    for pen in event.applied_penalties:
        breakdown[f"penalty_{pen.name}"] = pen.value
    return breakdown
