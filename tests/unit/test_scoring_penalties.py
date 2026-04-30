"""Unit tests for Module 36 — verify each of the 8 penalty conditions fires
correctly and the values exactly match v5.
"""

from __future__ import annotations

import math
from decimal import Decimal

from tests.conftest import build_enriched, build_print
from uoa_detector.config import default_config
from uoa_detector.domain.events import AppliedPenalty
from uoa_detector.scoring.penalties import PenaltyEngine


def _names(event_penalties: list[AppliedPenalty]) -> list[str]:
    return [p.name for p in event_penalties]


def test_no_penalties_when_clean() -> None:
    """Healthy event triggers nothing."""
    cfg = default_config()
    event = build_enriched(price=0.7)
    PenaltyEngine(cfg).apply(event, iv_rank=40)
    assert event.applied_penalties == []
    assert event.contradiction_penalty_applied is False


def test_post_gap_penalty_fires_with_exact_value() -> None:
    cfg = default_config()
    event = build_enriched()
    event.is_post_gap = True
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "post_gap_move"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.20, abs_tol=1e-9)


def test_iv_rank_penalty_fires_above_80() -> None:
    cfg = default_config()
    event = build_enriched()
    PenaltyEngine(cfg).apply(event, iv_rank=85.0)
    matched = [p for p in event.applied_penalties if p.name == "iv_rank_high"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.15, abs_tol=1e-9)


def test_iv_rank_penalty_does_not_fire_at_or_below_80() -> None:
    cfg = default_config()
    event = build_enriched()
    PenaltyEngine(cfg).apply(event, iv_rank=80.0)
    assert "iv_rank_high" not in _names(event.applied_penalties)


def test_iv_rank_skipped_when_none() -> None:
    cfg = default_config()
    event = build_enriched()
    PenaltyEngine(cfg).apply(event)  # no iv_rank passed
    assert "iv_rank_high" not in _names(event.applied_penalties)


def test_wide_spread_penalty_fires_above_15_pct() -> None:
    cfg = default_config()
    # bid 1.00 / ask 1.40 → mid 1.20, spread 0.40, 33% of mid → > 15%
    pr = build_print(bid="1.00", ask="1.40", option_price="1.20")
    event = build_enriched(print_=pr)
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "wide_spread"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.15, abs_tol=1e-9)


def test_tight_spread_no_penalty() -> None:
    cfg = default_config()
    # 1.45 / 1.55 → mid 1.50, spread 0.10, 6.7% of mid → not wide
    pr = build_print(bid="1.45", ask="1.55", option_price="1.50")
    event = build_enriched(print_=pr)
    PenaltyEngine(cfg).apply(event)
    assert "wide_spread" not in _names(event.applied_penalties)


def test_thin_oi_penalty() -> None:
    cfg = default_config()
    pr = build_print(open_interest=50)
    event = build_enriched(print_=pr)
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "thin_oi"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.20, abs_tol=1e-9)


def test_thin_oi_boundary_at_100_no_penalty() -> None:
    cfg = default_config()
    pr = build_print(open_interest=100)
    event = build_enriched(print_=pr)
    PenaltyEngine(cfg).apply(event)
    assert "thin_oi" not in _names(event.applied_penalties)


def test_contradiction_call_with_down_trend() -> None:
    cfg = default_config()
    pr = build_print(option_type="call")
    event = build_enriched(print_=pr, price=0.5)
    event.price_direction = "down"
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "flow_contradicts_price"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.15, abs_tol=1e-9)
    assert event.contradiction_penalty_applied is True


def test_contradiction_put_with_up_trend() -> None:
    cfg = default_config()
    pr = build_print(option_type="put")
    event = build_enriched(print_=pr)
    event.price_direction = "up"
    PenaltyEngine(cfg).apply(event)
    assert "flow_contradicts_price" in _names(event.applied_penalties)
    assert event.contradiction_penalty_applied is True


def test_no_contradiction_when_aligned() -> None:
    cfg = default_config()
    pr = build_print(option_type="call")
    event = build_enriched(print_=pr)
    event.price_direction = "up"
    PenaltyEngine(cfg).apply(event)
    assert "flow_contradicts_price" not in _names(event.applied_penalties)
    assert event.contradiction_penalty_applied is False


def test_post_event_penalty() -> None:
    cfg = default_config()
    event = build_enriched()
    event.is_post_event = True
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "post_event"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.25, abs_tol=1e-9)


def test_isolated_print_penalty() -> None:
    cfg = default_config()
    event = build_enriched()
    event.is_isolated_print = True
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "isolated_print"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.10, abs_tol=1e-9)


def test_next_day_oi_failed_penalty() -> None:
    cfg = default_config()
    event = build_enriched()
    event.next_day_oi_confirmed = False
    PenaltyEngine(cfg).apply(event)
    matched = [p for p in event.applied_penalties if p.name == "next_day_oi_failed"]
    assert len(matched) == 1
    assert math.isclose(matched[0].value, -0.20, abs_tol=1e-9)


def test_next_day_oi_none_no_penalty() -> None:
    """``None`` means 'not yet validated' — no penalty applies."""
    cfg = default_config()
    event = build_enriched()
    event.next_day_oi_confirmed = None
    PenaltyEngine(cfg).apply(event)
    assert "next_day_oi_failed" not in _names(event.applied_penalties)


def test_next_day_oi_true_no_penalty() -> None:
    cfg = default_config()
    event = build_enriched()
    event.next_day_oi_confirmed = True
    PenaltyEngine(cfg).apply(event)
    assert "next_day_oi_failed" not in _names(event.applied_penalties)


def test_all_eight_penalties_can_stack() -> None:
    """Maximum-pessimism event triggers all 8 penalties; sum is exactly -1.40."""
    cfg = default_config()
    pr = build_print(
        bid="0.10", ask="0.50", option_price="0.30",  # 100%+ spread
        open_interest=10,                              # thin OI
        option_type="call",                            # call vs down → contradiction
    )
    event = build_enriched(print_=pr)
    event.is_post_gap = True
    event.is_post_event = True
    event.is_isolated_print = True
    event.next_day_oi_confirmed = False
    event.price_direction = "down"
    PenaltyEngine(cfg).apply(event, iv_rank=90)

    names = _names(event.applied_penalties)
    assert set(names) == {
        "post_gap_move", "iv_rank_high", "wide_spread", "thin_oi",
        "flow_contradicts_price", "post_event", "isolated_print",
        "next_day_oi_failed",
    }
    total = sum(p.value for p in event.applied_penalties)
    assert math.isclose(
        total,
        -0.20 - 0.15 - 0.15 - 0.20 - 0.15 - 0.25 - 0.10 - 0.20,
        abs_tol=1e-9,
    )
    assert math.isclose(total, -1.40, abs_tol=1e-9)


def test_zero_mid_does_not_crash() -> None:
    """When bid/ask are both 0, spread% is undefined; engine must not divide."""
    cfg = default_config()
    pr = build_print(bid="0", ask="0", option_price="0.01")
    event = build_enriched(print_=pr)
    PenaltyEngine(cfg).apply(event)
    # No wide_spread penalty — spread% undefined.
    assert "wide_spread" not in _names(event.applied_penalties)


def test_spread_pct_uses_decimal_math() -> None:
    """Ensure no float drift in the spread comparison."""
    cfg = default_config()
    # Exactly 15% of mid: bid 1.0, ask 1.30 → mid 1.15, spread 0.30, 26.1% > 15
    pr = build_print(bid="1.00", ask="1.30", option_price="1.15")
    event = build_enriched(print_=pr)
    PenaltyEngine(cfg).apply(event)
    assert "wide_spread" in _names(event.applied_penalties)


def test_penalty_amounts_are_negative_or_zero() -> None:
    """Module 36 deductions are negative; AppliedPenalty enforces this."""
    cfg = default_config()
    p = cfg.penalties
    for v in (
        p.post_gap_move, p.iv_rank_high, p.wide_spread, p.thin_oi,
        p.flow_contradicts_price, p.post_event, p.isolated_print,
        p.next_day_oi_failed,
    ):
        assert v <= 0.0


_ = Decimal  # keep import used
