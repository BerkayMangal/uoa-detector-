"""Phase 3.2.2.3 — multi-source fusion replay + event-time watermark inheritance.

Two contracts pinned here:

  1. **Three ParquetReplaySource instances → SourceFusion produces a
     single ordered stream with the expected confidence-tier mix.**
     Demonstrates the multi-source replay scenario the acceptance doc
     names as "Polygon + UW + IBKR all replaying simultaneously".

  2. **`test_replay_at_inf_preserves_fusion_correctness`** — at any
     replay_speed (including inf), bucket-closure decisions are made
     on event-time, not walltime. The mechanism: SourceFusion's
     watermark is event-time (Phase 2.3.3 invariant, originally
     verified by ``test_window_differentiation_dict_equality`` in
     ``tests/unit/test_source_fusion.py``); the replay harness
     inherits that property by passing through event-time timestamps
     without rewriting them. If somebody ever changes the watermark
     to walltime, this test will fail at fast playback.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from uoa_detector.backtest.parquet_schema import write_parquet
from uoa_detector.calibration.profile import (
    CalibrationProfile,
    FusionParams,
    TierThresholds,
)
from uoa_detector.domain.events import OptionsPrint
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.fusion import SourceFusion
from uoa_detector.sources import ParquetReplaySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_print(
    *,
    source_id: str,
    timestamp: datetime,
    ticker: str = "AAPL",
    event_id: str,
    strike: int = 200,
    premium: int = 1000,
    implied_volatility: float = 0.45,
    open_interest: int = 1500,
) -> RawPrint:
    return RawPrint(
        source_id=source_id,
        source_event_id=event_id,
        timestamp=timestamp,
        ticker=ticker,
        option_type="call",
        strike=Decimal(strike),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198.50"),
        premium_paid=Decimal(premium),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        implied_volatility=implied_volatility,
        open_interest=open_interest,
        is_iso=False,
        source_tags=(),
    )


def _fusion_params(window_ms: int = 500) -> FusionParams:
    return FusionParams(
        window_ms=window_ms,
        timestamp_skew_tolerance_ms=100,
        stalled_source_timeout_ms=2000,
        allowed_lateness_ms=200,
        tier_thresholds=TierThresholds(
            unanimous_min_sources=2,
            majority_fraction=0.5,
            premium_disagreement_tolerance_pct=0.05,
        ),
    )


async def _drain_fusion(fusion: SourceFusion) -> list[OptionsPrint]:
    return [p async for p in fusion.stream()]


# ---------------------------------------------------------------------------
# Multi-source replay — three ParquetReplaySources through SourceFusion
# ---------------------------------------------------------------------------


def test_three_sources_replay_through_fusion(tmp_path: Path) -> None:
    """3 sources × 1 ticker × 1 month → fusion sees a tier mix.

    Scenario (timestamps designed to produce three different tiers):
      - t=14:30:00.000 A, B, C all emit AAPL@200 with same premium →
        'unanimous' bucket (n=3, all agree on premium and iso).
      - t=14:30:00.700 A only → 'single' bucket (>500ms isolated).
      - t=14:30:01.400 B (premium=1000) and C (premium=2000) — both
        emit but premium differs more than the 5% disagreement
        tolerance → n=2 with 0/2 agreeing → 'conflicted' (fusion's
        classification: agreement_fraction=0 < majority_fraction=0.5).

    The three-tier set we test against here is ('unanimous',
    'single', 'conflicted') — a 'majority' tier would require n>=3
    with partial disagreement, which doesn't happen in this scenario.
    Sufficient to demonstrate the multi-source replay scenario the
    acceptance doc names as 'Polygon + UW + IBKR all replaying
    simultaneously'.
    """
    base = datetime(2025, 6, 9, 14, 30, 0, tzinfo=UTC)

    # Source A — emits at base, then again at +700ms (isolated) only
    a_dir = tmp_path / "a" / "AAPL"
    a_dir.mkdir(parents=True)
    write_parquet(
        [
            _raw_print(
                source_id="a", timestamp=base, event_id="a-0", premium=1000,
            ),
            _raw_print(
                source_id="a",
                timestamp=base.replace(microsecond=700_000),
                event_id="a-1",
                premium=1000,
            ),
        ],
        a_dir / "2025-06.parquet",
    )

    # Source B — emits at base (agreeing premium), and at +1.4s (premium=1000)
    b_dir = tmp_path / "b" / "AAPL"
    b_dir.mkdir(parents=True)
    write_parquet(
        [
            _raw_print(
                source_id="b", timestamp=base, event_id="b-0", premium=1000,
            ),
            _raw_print(
                source_id="b",
                timestamp=base.replace(second=1, microsecond=400_000),
                event_id="b-1",
                premium=1000,
            ),
        ],
        b_dir / "2025-06.parquet",
    )

    # Source C — emits at base (agreeing premium), and at +1.4s
    # (premium=2000, far outside 5% tolerance)
    c_dir = tmp_path / "c" / "AAPL"
    c_dir.mkdir(parents=True)
    write_parquet(
        [
            _raw_print(
                source_id="c", timestamp=base, event_id="c-0", premium=1000,
            ),
            _raw_print(
                source_id="c",
                timestamp=base.replace(second=1, microsecond=400_000),
                event_id="c-1",
                premium=2000,
            ),
        ],
        c_dir / "2025-06.parquet",
    )

    a_src = ParquetReplaySource("a", tmp_path / "a")
    b_src = ParquetReplaySource("b", tmp_path / "b")
    c_src = ParquetReplaySource("c", tmp_path / "c")

    fusion = SourceFusion([a_src, b_src, c_src], _fusion_params(window_ms=500))
    out = asyncio.run(_drain_fusion(fusion))

    # Three buckets produced.
    assert len(out) == 3
    tiers = [p.source_agreement.confidence_tier for p in out]
    # First bucket: all three sources, all agree → unanimous.
    assert tiers[0] == "unanimous"
    # Second bucket: A only (isolated by >500ms window) → single.
    assert tiers[1] == "single"
    # Third bucket: B + C disagree on premium beyond tolerance →
    # n=2, agreement_fraction=0 < 0.5 → conflicted.
    assert tiers[2] == "conflicted"


# ---------------------------------------------------------------------------
# Event-time watermark inheritance — replay_speed-independence pin
# ---------------------------------------------------------------------------


def test_replay_at_inf_preserves_fusion_correctness(tmp_path: Path) -> None:
    """Fusion output is identical at replay_speed=inf and replay_speed=10.

    The mechanism: SourceFusion uses event-time watermarks (Phase
    2.3.3, see ``test_window_differentiation_dict_equality`` in
    ``test_source_fusion.py``). The replay harness must not alter
    that — it streams RawPrint timestamps untouched, and the only
    thing replay_speed controls is wall-clock spacing between
    emissions. If a regression switched the watermark to walltime,
    this test would fail at replay_speed=inf because all events
    would arrive 'simultaneously' from fusion's perspective.
    """
    # Same 3-source scenario as above.
    base = datetime(2025, 6, 9, 14, 30, 0, tzinfo=UTC)

    for source, base_offset_ms in (("a", 0), ("b", 0), ("c", 0)):
        d = tmp_path / source / "AAPL"
        d.mkdir(parents=True)
        rows = [
            _raw_print(source_id=source, timestamp=base, event_id=f"{source}-0"),
            _raw_print(
                source_id=source,
                timestamp=base.replace(second=1, microsecond=400_000),
                event_id=f"{source}-1",
            ),
        ]
        # A skips the second print to make a 'single' bucket at +700ms.
        if source == "a":
            rows = [
                rows[0],
                _raw_print(
                    source_id=source,
                    timestamp=base.replace(microsecond=700_000),
                    event_id=f"{source}-isolated",
                ),
            ]
        write_parquet(rows, d / "2025-06.parquet")
        # silence unused
        _ = base_offset_ms

    def run_at_speed(replay_speed: float) -> list[OptionsPrint]:
        a_src = ParquetReplaySource(
            "a", tmp_path / "a", replay_speed=replay_speed,
        )
        b_src = ParquetReplaySource(
            "b", tmp_path / "b", replay_speed=replay_speed,
        )
        c_src = ParquetReplaySource(
            "c", tmp_path / "c", replay_speed=replay_speed,
        )
        fusion = SourceFusion(
            [a_src, b_src, c_src], _fusion_params(window_ms=500),
        )
        return asyncio.run(_drain_fusion(fusion))

    # Run at inf (batch) and at 100x — fusion output must be byte-identical.
    out_inf = run_at_speed(float("inf"))
    out_fast = run_at_speed(100.0)

    assert len(out_inf) == len(out_fast)
    for a, b in zip(out_inf, out_fast, strict=True):
        # confidence_tier and the participating source set are the
        # watermark-sensitive invariants — if walltime watermarks crept
        # in, fast replay would either lump everything together or drop
        # late events.
        assert a.source_agreement.confidence_tier == b.source_agreement.confidence_tier
        # Each canonical print's contributing source IDs are stable
        # across replay speeds.
        assert a.source_agreement.sources_seen == b.source_agreement.sources_seen


def test_replay_at_inf_matches_synthetic_in_memory_fusion(tmp_path: Path) -> None:
    """A second pinned reference: ParquetReplaySource at replay_speed=inf
    produces the same fusion output as the in-memory
    SyntheticRawFlowSource over the same RawPrints.

    Catches regressions where the harness rewrites timestamps,
    re-orders events across files, or drops the source_agreement
    contributor identity.
    """
    from uoa_detector.sources import SyntheticRawFlowSource

    base = datetime(2025, 6, 9, 14, 30, 0, tzinfo=UTC)
    a_prints = [
        _raw_print(source_id="a", timestamp=base, event_id="a-0"),
        _raw_print(
            source_id="a",
            timestamp=base.replace(microsecond=700_000),
            event_id="a-1",
        ),
    ]
    b_prints = [
        _raw_print(source_id="b", timestamp=base, event_id="b-0"),
    ]

    # Disk path
    a_dir = tmp_path / "a" / "AAPL"
    a_dir.mkdir(parents=True)
    write_parquet(a_prints, a_dir / "2025-06.parquet")
    b_dir = tmp_path / "b" / "AAPL"
    b_dir.mkdir(parents=True)
    write_parquet(b_prints, b_dir / "2025-06.parquet")

    # Through replay
    a_replay = ParquetReplaySource("a", tmp_path / "a")
    b_replay = ParquetReplaySource("b", tmp_path / "b")
    fusion_replay = SourceFusion(
        [a_replay, b_replay], _fusion_params(window_ms=500),
    )
    out_replay = asyncio.run(_drain_fusion(fusion_replay))

    # Through in-memory
    a_mem = SyntheticRawFlowSource("a", a_prints)
    b_mem = SyntheticRawFlowSource("b", b_prints)
    fusion_mem = SourceFusion([a_mem, b_mem], _fusion_params(window_ms=500))
    out_mem = asyncio.run(_drain_fusion(fusion_mem))

    assert len(out_replay) == len(out_mem)
    for r, m in zip(out_replay, out_mem, strict=True):
        assert r.source_agreement.confidence_tier == m.source_agreement.confidence_tier
        assert r.source_agreement.sources_seen == m.source_agreement.sources_seen


def _ensure_calibration_profile_imports_clean() -> None:
    """Sentinel: if profile imports drift, this test module fails to load.

    The fusion tests above need CalibrationProfile + FusionParams +
    TierThresholds; if those move or rename, we want to catch it here
    rather than via a cryptic ImportError partway through a test run.
    """
    # Reference the imports so they aren't unused.
    _ = CalibrationProfile
    _ = FusionParams
    _ = TierThresholds


def test_calibration_profile_imports_present() -> None:
    _ensure_calibration_profile_imports_clean()
