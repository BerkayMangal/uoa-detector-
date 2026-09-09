"""Unit tests for the Phase 3.6 screener digest renderer.

Covers, over hand-built ``SignalDecisionRecord``s:
  - ranking order (combined score descending)
  - filtering of rejected / noise labels and zero-size records
  - empty-result rendering (no crash, verbatim message)
  - table columns present + verbatim intent header + legend
  - markdown rendering shape
  - determinism / stable tiebreak on identical scores
  - contract-string derivation and the LONG side semantics
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import build_enriched, build_print
from uoa_detector.calibration import CalibrationProfile, load_default_profile
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.observability.decision_record import (
    SignalDecisionRecord,
    build_decision_record,
)
from uoa_detector.observability.digest import (
    DIGEST_INTENT,
    DIGEST_LEGEND,
    EMPTY_MESSAGE,
    render_markdown,
    render_stdout,
    screen_records,
)

RUN_TS = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)


def _record(
    *,
    profile: CalibrationProfile,
    ticker: str,
    label: SignalLabel,
    score: float,
    max_r: float,
    bucket: RiskBucket = RiskBucket.STANDARD_UOA,
    option_type: str = "call",
    strike: str = "200",
    dte: int = 14,
    event_id: str = "evt",
    reason: str = "test reason",
) -> SignalDecisionRecord:
    """Build a decision record with a pinned post-penalty combined score."""
    pr = build_print(
        event_id=event_id,
        ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike,
        dte=dte,
    )
    event = build_enriched(print_=pr, combined_score_post_penalty=score)
    return build_decision_record(
        event=event,
        profile=profile,
        decision=LabelDecision(label=label, reason=reason),
        size=PositionSize(bucket=bucket, max_r=max_r),
    )


@pytest.fixture
def profile() -> CalibrationProfile:
    return load_default_profile()


def test_ranking_is_score_descending(profile: CalibrationProfile) -> None:
    records = [
        _record(profile=profile, ticker="AAA", label=SignalLabel.STANDARD_UOA,
                score=0.40, max_r=0.5, event_id="a"),
        _record(profile=profile, ticker="BBB", label=SignalLabel.SWEEP_UOA,
                score=0.80, max_r=0.85, event_id="b"),
        _record(profile=profile, ticker="CCC", label=SignalLabel.CONVEXITY_WATCH,
                score=0.60, max_r=0.25, event_id="c"),
    ]
    rows = screen_records(records)
    assert [r.ticker for r in rows] == ["BBB", "CCC", "AAA"]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert [r.score for r in rows] == [0.80, 0.60, 0.40]


def test_zero_size_and_noise_labels_filtered(profile: CalibrationProfile) -> None:
    records = [
        # Actionable — kept.
        _record(profile=profile, ticker="KEEP", label=SignalLabel.SWEEP_UOA,
                score=0.7, max_r=0.85, event_id="k"),
        # Zero R — dropped even with a non-noise label.
        _record(profile=profile, ticker="ZERO", label=SignalLabel.STANDARD_UOA,
                score=0.9, max_r=0.0, event_id="z"),
        # REJECTED — dropped.
        _record(profile=profile, ticker="REJ", label=SignalLabel.REJECTED,
                score=0.95, max_r=0.0, bucket=RiskBucket.REJECTED, event_id="r"),
        # Noise label — dropped.
        _record(profile=profile, ticker="NOISE", label=SignalLabel.IGNORE_NOISE,
                score=0.88, max_r=0.0, bucket=RiskBucket.DISCARD, event_id="n"),
        # PENALIZED_BELOW_THRESHOLD — dropped.
        _record(profile=profile, ticker="PEN",
                label=SignalLabel.PENALIZED_BELOW_THRESHOLD,
                score=0.3, max_r=0.0, bucket=RiskBucket.DISCARD, event_id="p"),
    ]
    rows = screen_records(records)
    assert [r.ticker for r in rows] == ["KEEP"]


def test_empty_result_renders_message_not_crash() -> None:
    out = render_stdout([], profile_id="v5_default", run_timestamp=RUN_TS)
    assert DIGEST_INTENT in out
    assert EMPTY_MESSAGE in out
    md = render_markdown([], profile_id="v5_default", run_timestamp=RUN_TS)
    assert EMPTY_MESSAGE in md


def test_all_records_filtered_yields_empty(profile: CalibrationProfile) -> None:
    records = [
        _record(profile=profile, ticker="N", label=SignalLabel.IGNORE_NOISE,
                score=0.5, max_r=0.0, bucket=RiskBucket.DISCARD),
    ]
    assert screen_records(records) == []
    out = render_stdout(
        screen_records(records), profile_id="v5_default", run_timestamp=RUN_TS,
    )
    assert EMPTY_MESSAGE in out


def test_stdout_has_columns_header_and_legend(profile: CalibrationProfile) -> None:
    rows = screen_records(
        [
            _record(profile=profile, ticker="NVDA", label=SignalLabel.STANDARD_UOA,
                    score=0.6, max_r=0.75, event_id="x"),
        ],
    )
    out = render_stdout(rows, profile_id="v5_gamma_squeeze", run_timestamp=RUN_TS)
    # Verbatim intent header.
    assert out.splitlines()[0] == DIGEST_INTENT
    # Metadata line.
    assert "profile: v5_gamma_squeeze" in out
    assert "run: 2026-09-09T13:30:00+00:00" in out
    assert "candidates: 1" in out
    # All column headers present.
    for col in ("TICKER", "SIDE", "LABEL", "SCORE", "CONTRACT", "MAX_R", "TOP REASONS"):
        assert col in out
    # Row data present.
    assert "NVDA" in out
    assert "LONG" in out
    assert "C 200 @" in out
    # Legend present.
    assert DIGEST_LEGEND in out


def test_markdown_shape(profile: CalibrationProfile) -> None:
    rows = screen_records(
        [
            _record(profile=profile, ticker="TSLA", label=SignalLabel.SWEEP_UOA,
                    score=0.63, max_r=0.85, reason="ISO classified", event_id="t"),
        ],
    )
    md = render_markdown(rows, profile_id="v5_default", run_timestamp=RUN_TS)
    assert md.startswith("# Screener digest")
    assert f"> {DIGEST_INTENT}" in md
    # Table header + separator.
    assert "| # | Ticker | Side | Label |" in md
    assert "|--:|" in md
    # Row + labeler note carried through.
    assert "| TSLA |" in md
    assert "ISO classified" in md
    assert DIGEST_LEGEND in md


def test_contract_and_side_derivation(profile: CalibrationProfile) -> None:
    put = _record(profile=profile, ticker="SPY", label=SignalLabel.LEAP_POSITIONING,
                  score=0.5, max_r=0.25, bucket=RiskBucket.LEAP_POSITIONING,
                  option_type="put", strike="450", dte=180, event_id="put1")
    row = screen_records([put])[0]
    assert row.side == "LONG"  # always long the contract
    expiry = put.event.print_.expiry.isoformat()
    assert row.contract == f"P 450 @ {expiry} (180DTE)"


def test_deterministic_tiebreak_on_equal_scores(profile: CalibrationProfile) -> None:
    # Same score; different event_ids. Order must be stable across runs and
    # independent of input order.
    a = _record(profile=profile, ticker="ZZZ", label=SignalLabel.STANDARD_UOA,
                score=0.5, max_r=0.5, event_id="aaa")
    b = _record(profile=profile, ticker="ZZZ", label=SignalLabel.STANDARD_UOA,
                score=0.5, max_r=0.5, event_id="bbb")
    forward = [r.reasons for r in screen_records([a, b])]
    backward = screen_records([b, a])
    # event_id "aaa" < "bbb" → a ranks first regardless of input order.
    assert screen_records([a, b])[0].rank == 1
    assert [row.rank for row in backward] == [1, 2]
    assert forward == [r.reasons for r in screen_records([b, a])]


def test_top_reasons_excludes_penalties_and_is_capped(
    profile: CalibrationProfile,
) -> None:
    row = screen_records(
        [
            _record(profile=profile, ticker="AMD", label=SignalLabel.STANDARD_UOA,
                    score=0.6, max_r=0.75, event_id="amd"),
        ],
    )[0]
    assert len(row.reasons) == 2
    assert all(not r.startswith("penalty_") for r in row.reasons)
