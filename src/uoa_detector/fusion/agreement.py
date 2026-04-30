"""``classify_agreement`` — turns a fusion bucket of ``RawPrint``s into a
``SourceAgreement``.

This module contains the agreement-tier resolution logic the user specified
in Phase 2.3.2:

  Tier resolution order:
    1. ``len(sources_seen) == 1`` → ``"single"`` (single-source path).
    2. All sources match consensus AND ``len >= unanimous_min_sources`` → ``"unanimous"``.
    3. Agreement fraction ``>= majority_fraction`` → ``"majority"``.
    4. Otherwise → ``"conflicted"``.

  "Match consensus" is judged on:
    - ``premium_paid`` within ``premium_disagreement_tolerance_pct`` of the
      median across reporting sources.
    - ``is_iso`` matches the modal value (ties broken deterministically by
      ``False`` < ``True`` so two-way ties classify as ``False``).
    - ``(strike, expiry, option_type)`` agreement is implicit — these are the
      bucket key, so all prints in a single fusion bucket already share them.

Fields not all sources report (e.g., ``open_interest`` on Polygon vs. UW)
do NOT count toward disagreement — only ``premium_paid`` and ``is_iso`` are
factored in for Phase 2.3.2. Future sources that ship a richer
``sweep_classification`` directly will get added to the consensus check
through this same function.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from statistics import median

from uoa_detector.calibration.profile import TierThresholds
from uoa_detector.domain.agreement import ConfidenceTier, SourceAgreement
from uoa_detector.domain.raw_print import RawPrint


def classify_agreement(
    prints: Sequence[RawPrint],
    params: TierThresholds,
) -> SourceAgreement:
    """Build a ``SourceAgreement`` from one or more ``RawPrint``s in a bucket.

    :param prints: Non-empty sequence of prints that share the same
        ``(ticker, strike, expiry, option_type)`` (the fusion bucket key).
    :param params: Tier-resolution thresholds from
        ``profile.fusion.tier_thresholds``.
    :raises ValueError: if ``prints`` is empty.
    """
    n = len(prints)
    if n == 0:
        msg = "classify_agreement requires at least one print"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Single-source path — fast, no windowing, deterministic tier.
    # ------------------------------------------------------------------
    if n == 1:
        only = prints[0]
        return SourceAgreement(
            sources_seen=(only.source_id,),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
            exchanges_seen=(only.exchange,) if only.exchange else (),
        )

    # ------------------------------------------------------------------
    # Multi-source path
    # ------------------------------------------------------------------
    sources_seen = tuple(p.source_id for p in prints)
    exchanges_seen = tuple(sorted({p.exchange for p in prints if p.exchange}))

    premiums = [p.premium_paid for p in prints]
    premium_disagreement = max(premiums) - min(premiums)

    timestamps_ms = [int(p.timestamp.timestamp() * 1000) for p in prints]
    timestamp_skew_ms = max(timestamps_ms) - min(timestamps_ms)

    # ISO classification disagreement is reported even if the agreement
    # fraction is high — it's useful telemetry on its own.
    iso_values = {p.is_iso for p in prints}
    classification_disagreement = len(iso_values) > 1

    # ---- Per-source consensus check ----
    median_premium = median(premiums)  # statistics.median handles Decimal
    tol_pct = Decimal(str(params.premium_disagreement_tolerance_pct))

    if median_premium == 0:
        # Edge case: median is zero. Relative tolerance is meaningless;
        # require exact match. This is unusual in practice (premium 0 means
        # a print at zero notional) but the fallback keeps the function total.
        premium_agrees = [p.premium_paid == 0 for p in prints]
    else:
        premium_tolerance = abs(median_premium) * tol_pct
        premium_agrees = [
            abs(p.premium_paid - median_premium) <= premium_tolerance
            for p in prints
        ]

    # Modal is_iso, with deterministic tie-breaking: pick False on a tie.
    iso_counter = Counter(p.is_iso for p in prints)
    modal_iso = sorted(
        iso_counter.items(),
        key=lambda kv: (-kv[1], kv[0]),  # most common first; ties → False < True
    )[0][0]
    iso_agrees = [p.is_iso == modal_iso for p in prints]

    # A source agrees iff it matches BOTH the premium consensus AND the
    # classification consensus.
    per_source_agrees = [
        prem_ok and iso_ok
        for prem_ok, iso_ok in zip(premium_agrees, iso_agrees, strict=True)
    ]
    agreement_count = sum(per_source_agrees)
    agreement_fraction = agreement_count / n

    # ---- Tier resolution ----
    tier: ConfidenceTier
    if agreement_count == n and n >= params.unanimous_min_sources:
        tier = "unanimous"
    elif agreement_fraction >= params.majority_fraction:
        tier = "majority"
    else:
        tier = "conflicted"

    return SourceAgreement(
        sources_seen=sources_seen,
        premium_disagreement=premium_disagreement,
        timestamp_skew_ms=timestamp_skew_ms,
        classification_disagreement=classification_disagreement,
        confidence_tier=tier,
        exchanges_seen=exchanges_seen,
    )
