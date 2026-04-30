"""Unit tests for ``fusion.classify_agreement``.

Covers all four ``confidence_tier`` paths plus the edge cases that came up
during design — modal-classification ties, zero-premium fallback, exchange
deduplication, premium-disagreement reporting independent of tier.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration.profile import TierThresholds
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.fusion import classify_agreement

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def _raw(
    *,
    source_id: str,
    premium: str = "100",
    is_iso: bool = False,
    exchange: str = "CBOE",
    ts_offset_ms: int = 0,
    source_event_id: str = "e",
) -> RawPrint:
    """Build a ``RawPrint`` with sensible defaults; override only what the
    test cares about."""
    ts = _BASE_TS + timedelta(milliseconds=ts_offset_ms)
    return RawPrint(
        source_id=source_id,
        source_event_id=f"{source_id}-{source_event_id}",
        timestamp=ts,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal(premium),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange=exchange,
        is_iso=is_iso,
    )


def _params(
    *,
    unanimous_min_sources: int = 2,
    majority_fraction: float = 0.5,
    premium_disagreement_tolerance_pct: float = 0.05,
) -> TierThresholds:
    return TierThresholds(
        unanimous_min_sources=unanimous_min_sources,
        majority_fraction=majority_fraction,
        premium_disagreement_tolerance_pct=premium_disagreement_tolerance_pct,
    )


# ---------------------------------------------------------------------------
# Single-source path
# ---------------------------------------------------------------------------

def test_single_source_emits_single_tier() -> None:
    a = classify_agreement([_raw(source_id="polygon")], _params())
    assert a.confidence_tier == "single"
    assert a.sources_seen == ("polygon",)
    assert a.premium_disagreement == Decimal("0")
    assert a.timestamp_skew_ms == 0
    assert a.classification_disagreement is False
    assert a.exchanges_seen == ("CBOE",)


def test_single_source_with_no_exchange() -> None:
    """Empty exchange string is dropped from exchanges_seen tuple."""
    a = classify_agreement([_raw(source_id="x", exchange="")], _params())
    assert a.exchanges_seen == ()


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        classify_agreement([], _params())


# ---------------------------------------------------------------------------
# Unanimous tier — all sources match consensus
# ---------------------------------------------------------------------------

def test_two_sources_identical_premium_and_iso_unanimous() -> None:
    a = classify_agreement(
        [
            _raw(source_id="polygon", premium="100"),
            _raw(source_id="unusual_whales", premium="100"),
        ],
        _params(),
    )
    assert a.confidence_tier == "unanimous"
    assert set(a.sources_seen) == {"polygon", "unusual_whales"}
    assert a.classification_disagreement is False


def test_three_sources_premium_within_tolerance_unanimous() -> None:
    """All three within 5% of median — unanimous."""
    a = classify_agreement(
        [
            _raw(source_id="a", premium="100"),
            _raw(source_id="b", premium="103"),  # 3% above median (100)
            _raw(source_id="c", premium="98"),   # 2% below
        ],
        _params(),
    )
    assert a.confidence_tier == "unanimous"


def test_unanimous_min_sources_three_blocks_unanimous_with_two() -> None:
    """If unanimous_min_sources=3, two-agreeing-sources is NOT unanimous."""
    a = classify_agreement(
        [
            _raw(source_id="a", premium="100"),
            _raw(source_id="b", premium="100"),
        ],
        _params(unanimous_min_sources=3),
    )
    # All sources match consensus, but n=2 < unanimous_min_sources=3.
    # Falls through to majority (fraction 1.0 >= 0.5).
    assert a.confidence_tier == "majority"


# ---------------------------------------------------------------------------
# Majority tier — most agree, not all
# ---------------------------------------------------------------------------

def test_three_sources_two_agree_one_outside_tolerance_majority() -> None:
    """Two sources at 100, one at 200 (way outside 5% tolerance)."""
    a = classify_agreement(
        [
            _raw(source_id="a", premium="100"),
            _raw(source_id="b", premium="100"),
            _raw(source_id="c", premium="200"),
        ],
        _params(),
    )
    # Median of [100, 100, 200] is 100; tolerance ±5. Two within, one out.
    # agreement_fraction = 2/3 ≈ 0.667 ≥ 0.5 → majority.
    assert a.confidence_tier == "majority"
    assert a.premium_disagreement == Decimal("100")  # 200 - 100


def test_iso_disagreement_one_outlier_majority() -> None:
    """Three sources, one disagreeing on ISO flag → majority."""
    a = classify_agreement(
        [
            _raw(source_id="a", is_iso=True),
            _raw(source_id="b", is_iso=True),
            _raw(source_id="c", is_iso=False),
        ],
        _params(),
    )
    # Modal is_iso = True. 2/3 agree → majority.
    assert a.confidence_tier == "majority"
    assert a.classification_disagreement is True


# ---------------------------------------------------------------------------
# Conflicted tier — agreement below majority threshold
# ---------------------------------------------------------------------------

def test_four_sources_split_evenly_conflicted_when_majority_strict() -> None:
    """4 sources, 2 each on different is_iso, with strict majority>0.5."""
    a = classify_agreement(
        [
            _raw(source_id="a", is_iso=True),
            _raw(source_id="b", is_iso=True),
            _raw(source_id="c", is_iso=False),
            _raw(source_id="d", is_iso=False),
        ],
        _params(majority_fraction=0.6),  # require 60% agreement
    )
    # Modal tie on is_iso: tie-breaks to False (deterministic). 2/4 = 0.5 < 0.6.
    assert a.confidence_tier == "conflicted"


def test_two_sources_far_apart_premium_low_majority_threshold() -> None:
    """Two sources, premiums differ wildly. Even at majority_fraction=0.5,
    only one source matches consensus → fraction 0.5 → still majority by
    default user-spec semantics. Bump majority_fraction to force conflicted.
    """
    a = classify_agreement(
        [
            _raw(source_id="a", premium="100"),
            _raw(source_id="b", premium="200"),
        ],
        _params(majority_fraction=0.6),  # need >60% agreement
    )
    # Median of [100, 200] = 150; tolerance ±7.5. Both 50 away → both DON'T
    # match consensus. 0/2 agree → conflicted.
    assert a.confidence_tier == "conflicted"


# ---------------------------------------------------------------------------
# Reported scalars — premium_disagreement, timestamp_skew_ms, exchanges_seen
# ---------------------------------------------------------------------------

def test_premium_disagreement_reported_even_when_unanimous() -> None:
    """premium_disagreement is max-min; reported regardless of tier."""
    a = classify_agreement(
        [
            _raw(source_id="a", premium="100"),
            _raw(source_id="b", premium="103"),
        ],
        _params(),
    )
    assert a.confidence_tier == "unanimous"  # within 5%
    assert a.premium_disagreement == Decimal("3")  # but disagreement still recorded


def test_timestamp_skew_ms_recorded() -> None:
    a = classify_agreement(
        [
            _raw(source_id="a", ts_offset_ms=0),
            _raw(source_id="b", ts_offset_ms=120),
            _raw(source_id="c", ts_offset_ms=-50),
        ],
        _params(),
    )
    assert a.timestamp_skew_ms == 170  # 120 - (-50)


def test_exchanges_seen_dedup_and_sorted() -> None:
    a = classify_agreement(
        [
            _raw(source_id="a", exchange="CBOE"),
            _raw(source_id="b", exchange="ISE"),
            _raw(source_id="c", exchange="CBOE"),  # dup
        ],
        _params(),
    )
    assert a.exchanges_seen == ("CBOE", "ISE")  # sorted, deduped


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_zero_premium_falls_back_to_exact_match() -> None:
    """When the median premium is zero, relative tolerance is meaningless;
    function falls back to exact-equality. Both at 0 → unanimous; one at 0
    one at 1 → conflicted (only the zero one matches consensus).
    """
    both_zero = classify_agreement(
        [
            _raw(source_id="a", premium="0"),
            _raw(source_id="b", premium="0"),
        ],
        _params(),
    )
    assert both_zero.confidence_tier == "unanimous"

    mixed = classify_agreement(
        [
            _raw(source_id="a", premium="0"),
            _raw(source_id="b", premium="0"),
            _raw(source_id="c", premium="1"),
        ],
        _params(),
    )
    # Median is 0; only sources with premium == 0 match; 2/3 agree.
    assert mixed.confidence_tier == "majority"


def test_modal_iso_tie_breaks_to_false() -> None:
    """Two sources, one True one False — tie on is_iso. Tie-break
    deterministically picks False, so the True source disagrees.
    """
    a = classify_agreement(
        [
            _raw(source_id="a", is_iso=True),
            _raw(source_id="b", is_iso=False),
        ],
        _params(majority_fraction=0.6),
    )
    # Modal = False (tie-break). 1/2 agree, but 0.5 < majority_fraction 0.6.
    assert a.confidence_tier == "conflicted"
    assert a.classification_disagreement is True


def test_sources_seen_preserves_input_order() -> None:
    """Caller may want sources_seen in arrival order for logging."""
    a = classify_agreement(
        [
            _raw(source_id="zebra"),
            _raw(source_id="apple"),
            _raw(source_id="mango"),
        ],
        _params(),
    )
    assert a.sources_seen == ("zebra", "apple", "mango")


def test_unanimous_requires_all_match_not_just_majority() -> None:
    """If 99/100 match consensus, that's majority — NOT unanimous."""
    prints = [_raw(source_id=f"src_{i}", premium="100") for i in range(99)]
    prints.append(_raw(source_id="outlier", premium="500"))
    a = classify_agreement(prints, _params())
    assert a.confidence_tier == "majority"  # 99/100 = 0.99, but not 1.0


def test_unanimous_min_sources_validator_rejects_one() -> None:
    """unanimous_min_sources must be >=2; 1 doesn't make sense (that's 'single')."""
    with pytest.raises(Exception, match="greater than or equal to 2"):
        TierThresholds(
            unanimous_min_sources=1,
            majority_fraction=0.5,
            premium_disagreement_tolerance_pct=0.05,
        )


def test_majority_fraction_validator_rejects_below_half() -> None:
    """majority_fraction must be >=0.5 (a 'plurality' isn't a majority)."""
    with pytest.raises(Exception, match=r"greater than or equal to 0\.5"):
        TierThresholds(
            unanimous_min_sources=2,
            majority_fraction=0.4,
            premium_disagreement_tolerance_pct=0.05,
        )


def test_premium_tolerance_validator_rejects_above_one() -> None:
    """premium_disagreement_tolerance_pct must be in [0, 1]."""
    with pytest.raises(Exception, match="less than or equal to 1"):
        TierThresholds(
            unanimous_min_sources=2,
            majority_fraction=0.5,
            premium_disagreement_tolerance_pct=1.5,
        )
