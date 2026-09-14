"""Unit tests for ``fusion.SourceFusion`` — event-time watermark windowing.

Three required test scenarios from the Phase 2.3.3 spec:
  1. The 100/500/2000ms differentiation test (window-correctness under
     the watermark closure rule).
  2. ``test_stalled_source_does_not_block_fusion``: silent source must
     not deadlock the pipeline; stall timeout force-advances watermark.
  3. ``test_late_arrival_is_dropped_and_logged``: events older than
     ``global_watermark`` are dropped with a structured warning.

Plus the usual single-source / multi-source / canonical-aggregation tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration.profile import FusionParams, TierThresholds
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.errors import DataSourceError
from uoa_detector.fusion import SourceFusion
from uoa_detector.sources.synthetic import (
    StallingRawFlowSource,
    SyntheticRawFlowSource,
)

_BASE_TS = datetime(2025, 6, 11, 15, 30, tzinfo=UTC)


def _raw(
    *,
    source_id: str,
    ts_offset_ms: int = 0,
    premium: str = "100",
    is_iso: bool = False,
    exchange: str = "CBOE",
    ticker: str = "AAPL",
    strike: str = "200",
    iv: float = 0.45,
    oi: int = 1500,
    event_id_suffix: str = "0",
) -> RawPrint:
    """Build a RawPrint with sensible defaults; tests override what they need."""
    ts = _BASE_TS + timedelta(milliseconds=ts_offset_ms)
    return RawPrint(
        source_id=source_id,
        source_event_id=f"{source_id}-{event_id_suffix}",
        timestamp=ts,
        ticker=ticker,
        option_type="call",
        strike=Decimal(strike),
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
        implied_volatility=iv,
        open_interest=oi,
    )


def _params(
    *,
    window_ms: int = 500,
    stalled_source_timeout_ms: int = 2000,
    allowed_lateness_ms: int = 200,
) -> FusionParams:
    return FusionParams(
        window_ms=window_ms,
        timestamp_skew_tolerance_ms=100,
        stalled_source_timeout_ms=stalled_source_timeout_ms,
        allowed_lateness_ms=allowed_lateness_ms,
        tier_thresholds=TierThresholds(
            unanimous_min_sources=2,
            majority_fraction=0.5,
            premium_disagreement_tolerance_pct=0.05,
        ),
    )


# ---------------------------------------------------------------------------
# Constructor / state
# ---------------------------------------------------------------------------

def test_empty_sources_rejected() -> None:
    with pytest.raises(DataSourceError, match="at least one source"):
        SourceFusion([], _params())


def test_is_single_source_true_for_one_source() -> None:
    src = SyntheticRawFlowSource("polygon", [])
    fusion = SourceFusion([src], _params())
    assert fusion.is_single_source is True


def test_is_single_source_false_when_force_multi() -> None:
    src = SyntheticRawFlowSource("polygon", [])
    fusion = SourceFusion([src], _params(), force_multi_source=True)
    assert fusion.is_single_source is False


def test_is_single_source_false_for_two_sources() -> None:
    a = SyntheticRawFlowSource("a", [])
    b = SyntheticRawFlowSource("b", [])
    fusion = SourceFusion([a, b], _params())
    assert fusion.is_single_source is False


# ---------------------------------------------------------------------------
# Single-source fast path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_source_emits_immediately_with_single_tier() -> None:
    """Single source → tier='single', no windowing wait."""
    src = SyntheticRawFlowSource(
        "synthetic",
        [
            _raw(source_id="synthetic", ts_offset_ms=0),
            _raw(source_id="synthetic", ts_offset_ms=10000, event_id_suffix="1"),
        ],
    )
    fusion = SourceFusion([src], _params(window_ms=500))

    out = [p async for p in fusion.stream()]

    assert len(out) == 2
    for canonical in out:
        assert canonical.source_agreement.confidence_tier == "single"
        assert canonical.source_agreement.sources_seen == ("synthetic",)
        assert canonical.source_agreement.timestamp_skew_ms == 0
        assert canonical.source_agreement.classification_disagreement is False


@pytest.mark.asyncio
async def test_single_source_preserves_event_id_verbatim() -> None:
    """n=1 buckets preserve source_event_id (no 'fused:' prefix).

    Critical for downstream systems that use upstream event_ids as keys
    (scenario overrides, decision-record correlation).
    """
    src = SyntheticRawFlowSource(
        "synthetic",
        [_raw(source_id="synthetic", event_id_suffix="abc123")],
    )
    fusion = SourceFusion([src], _params())

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].event_id == "synthetic-abc123"
    assert not out[0].event_id.startswith("fused:")


# ---------------------------------------------------------------------------
# 100/500/2000ms differentiation — mandatory
# ---------------------------------------------------------------------------
# Two sources, 4 events, same bucket key. The number of canonical
# OptionsPrints emitted varies by window_ms:
#   100ms  → 4 (every event isolates)
#   500ms  → 2 (paired fusion)
#   2000ms → 1 (all fuse)
#
# Under the watermark closure rule, the assertion proves bucket closures
# happen at the right time as the global watermark advances.

_SCENARIO_SCHEDULE = (("a", 0), ("b", 300), ("a", 1500), ("b", 1800))


def _make_scenario_sources() -> tuple[SyntheticRawFlowSource, SyntheticRawFlowSource]:
    a_events = [
        _raw(source_id="a", ts_offset_ms=offset, event_id_suffix=str(i))
        for i, (src, offset) in enumerate(_SCENARIO_SCHEDULE)
        if src == "a"
    ]
    b_events = [
        _raw(source_id="b", ts_offset_ms=offset, event_id_suffix=str(i))
        for i, (src, offset) in enumerate(_SCENARIO_SCHEDULE)
        if src == "b"
    ]
    return (
        SyntheticRawFlowSource("a", a_events),
        SyntheticRawFlowSource("b", b_events),
    )


@pytest.mark.asyncio
async def test_window_100ms_isolates_every_print() -> None:
    """window=100ms is shorter than every inter-print gap → 4 separate buckets."""
    a, b = _make_scenario_sources()
    fusion = SourceFusion([a, b], _params(window_ms=100))

    out = [p async for p in fusion.stream()]

    assert len(out) == 4
    for canonical in out:
        assert canonical.source_agreement.confidence_tier == "single"
        assert len(canonical.source_agreement.sources_seen) == 1


@pytest.mark.asyncio
async def test_window_500ms_fuses_pairs() -> None:
    """window=500ms catches consecutive prints → 2 outputs, each 2-source."""
    a, b = _make_scenario_sources()
    fusion = SourceFusion([a, b], _params(window_ms=500))

    out = [p async for p in fusion.stream()]

    assert len(out) == 2
    for canonical in out:
        assert canonical.source_agreement.confidence_tier == "unanimous"
        assert set(canonical.source_agreement.sources_seen) == {"a", "b"}


@pytest.mark.asyncio
async def test_window_2000ms_fuses_all_four_into_one_bucket() -> None:
    """window=2000ms spans the entire 1.8s scenario → 1 output, 4 prints."""
    a, b = _make_scenario_sources()
    fusion = SourceFusion([a, b], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]

    assert len(out) == 1
    canonical = out[0]
    assert canonical.source_agreement.confidence_tier == "unanimous"
    assert len(canonical.source_agreement.sources_seen) == 4


@pytest.mark.asyncio
async def test_window_differentiation_dict_equality() -> None:
    """Same scenario, three windows, three different output counts.

    A regression that breaks any window fails this test cleanly.
    """
    counts: dict[int, int] = {}
    for window in (100, 500, 2000):
        a, b = _make_scenario_sources()
        fusion = SourceFusion([a, b], _params(window_ms=window))
        out = [p async for p in fusion.stream()]
        counts[window] = len(out)

    assert counts == {100: 4, 500: 2, 2000: 1}


# ---------------------------------------------------------------------------
# Stalled-source recovery — required by Phase 2.3.3 spec
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stalled_source_does_not_block_fusion() -> None:
    """3 sources A/B/C; C never emits. Without stall recovery, the global
    watermark sticks at C's None watermark forever and buckets never close.
    With stall recovery, C's watermark force-advances after
    ``stalled_source_timeout_ms`` walltime and the AB bucket closes.

    Test consumes canonicals as they're emitted (via a separate task) rather
    than awaiting stream end — fusion.stream() can't return until C sends
    DONE, so we close C explicitly after observing the AB canonical.
    """
    a = SyntheticRawFlowSource(
        "a",
        [
            _raw(source_id="a", ts_offset_ms=0, event_id_suffix="0"),
            _raw(source_id="a", ts_offset_ms=50, event_id_suffix="1"),
        ],
    )
    b = SyntheticRawFlowSource(
        "b",
        [
            _raw(source_id="b", ts_offset_ms=25, event_id_suffix="0"),
            _raw(source_id="b", ts_offset_ms=75, event_id_suffix="1"),
        ],
    )
    c = StallingRawFlowSource("c", [])  # never emits, just stalls

    fusion = SourceFusion(
        [a, b, c],
        _params(
            window_ms=500,
            stalled_source_timeout_ms=100,
            allowed_lateness_ms=20,
        ),
    )

    results: list = []

    async def _consume() -> None:
        async for canonical in fusion.stream():
            results.append(canonical)

    consume_task = asyncio.create_task(_consume())

    try:
        # Wait for the AB bucket to be emitted. The stall on C must
        # force-advance C's watermark within ~100ms walltime; budget 1.5s
        # to comfortably absorb scheduler jitter.
        async def _wait_for_ab() -> object:
            while True:
                ab = next(
                    (
                        r
                        for r in results
                        if "a" in r.source_agreement.sources_seen
                        and "b" in r.source_agreement.sources_seen
                    ),
                    None,
                )
                if ab is not None:
                    return ab
                await asyncio.sleep(0.01)

        try:
            ab_print = await asyncio.wait_for(_wait_for_ab(), timeout=1.5)
        except TimeoutError:
            pytest.fail(
                "fusion deadlocked — stalled-source recovery did not advance "
                "the global watermark within 1.5s walltime"
            )

        # The canonical print emitted under stall recovery should contain A
        # and B's events but NOT C (C never emitted).
        sources_seen = set(ab_print.source_agreement.sources_seen)  # type: ignore[attr-defined]
        assert "a" in sources_seen
        assert "b" in sources_seen
        assert "c" not in sources_seen

    finally:
        # Release the stalling source so the stream can complete cleanly.
        await c.close()
        try:
            await asyncio.wait_for(consume_task, timeout=1.0)
        except TimeoutError:
            consume_task.cancel()


# ---------------------------------------------------------------------------
# Late-arrival drop — required by Phase 2.3.3 spec
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_late_arrival_is_dropped_and_logged() -> None:
    """An event whose timestamp is below ``global_watermark`` is dropped
    with a structured ``late_event=True`` warning.

    Uses ``force_multi_source=True`` with a single source emitting events
    out of order — this makes the test fully deterministic by avoiding
    cross-source scheduling. The single source's watermark advances
    monotonically with each forward-in-time event, so when an
    out-of-order event arrives behind the watermark, it's late.

    Sequence:
      - A0 at t=0. A.wm = 0. global_wm = 0. (A0.ts < 0? no. accepted.)
      - A1 at t=2000. A.wm = 2000. global_wm = 2000. A0's bucket closes.
      - A_late at t=500. global_wm = 2000. 500 < 2000 → LATE, dropped.

    Uses ``structlog.testing.capture_logs`` rather than pytest's ``caplog``
    because structlog writes through its own PrintLogger by default, which
    bypasses Python's ``logging`` module that ``caplog`` hooks.
    """
    from structlog.testing import capture_logs

    src = SyntheticRawFlowSource(
        "a",
        [
            _raw(source_id="a", ts_offset_ms=0, event_id_suffix="0"),
            _raw(source_id="a", ts_offset_ms=2000, event_id_suffix="1"),
            _raw(source_id="a", ts_offset_ms=500, event_id_suffix="late"),
        ],
    )
    fusion = SourceFusion(
        [src],
        _params(window_ms=100),
        force_multi_source=True,
    )

    with capture_logs() as cap:
        out = [p async for p in fusion.stream()]

    # The late event must NOT appear as a canonical event_id.
    late_event_id = "a-late"
    for canonical in out:
        assert canonical.event_id != late_event_id, (
            f"late event {late_event_id} was not dropped — appeared as "
            f"canonical event_id; canonical={canonical}"
        )

    # The two non-late events produced canonicals.
    assert len(out) == 2
    assert {c.event_id for c in out} == {"a-0", "a-1"}

    # A structured 'late_event' log was emitted with the required fields.
    late_logs = [
        rec
        for rec in cap
        if rec.get("event") == "late_event"
        and rec.get("log_level") == "warning"
    ]
    assert late_logs, (
        f"expected a 'late_event' warning; got {len(cap)} log records: "
        f"events={[r.get('event') for r in cap]}"
    )

    record = late_logs[0]
    assert record["late_event"] is True
    assert record["source_id"] == "a"
    assert "global_watermark" in record
    assert "event_time" in record
    assert record["lateness_ms"] == 1500  # 2000 - 500


# ---------------------------------------------------------------------------
# Bucket-key correctness (different keys never fuse)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_strikes_do_not_fuse_within_window() -> None:
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", strike="200")])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", strike="210")])
    fusion = SourceFusion([a, b], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 2
    assert {c.strike for c in out} == {Decimal("200"), Decimal("210")}


@pytest.mark.asyncio
async def test_different_tickers_do_not_fuse_within_window() -> None:
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", ticker="AAPL")])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", ticker="MSFT")])
    fusion = SourceFusion([a, b], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 2
    assert {c.ticker for c in out} == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# End-of-stream drain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_of_stream_drains_open_buckets_unconditionally() -> None:
    """A bucket whose deadline hasn't been crossed by global_watermark still
    emits when all sources are done.
    """
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", ts_offset_ms=0)])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", ts_offset_ms=10)])
    # window 5000ms — bucket would NOT close on its own.
    fusion = SourceFusion([a, b], _params(window_ms=5000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].source_agreement.confidence_tier == "unanimous"


# ---------------------------------------------------------------------------
# Canonical-print field aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_canonical_premium_uses_median_across_sources() -> None:
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", premium="100")])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", premium="110")])
    c = SyntheticRawFlowSource("c", [_raw(source_id="c", premium="120")])
    fusion = SourceFusion([a, b, c], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].premium_paid == Decimal("110")


@pytest.mark.asyncio
async def test_canonical_timestamp_uses_earliest() -> None:
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", ts_offset_ms=300)])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", ts_offset_ms=0)])
    c = SyntheticRawFlowSource("c", [_raw(source_id="c", ts_offset_ms=100)])
    fusion = SourceFusion([a, b, c], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].timestamp == _BASE_TS  # earliest = b at offset 0


@pytest.mark.asyncio
async def test_canonical_oi_uses_max_across_sources() -> None:
    """OI is monotonic per session — max reading is most recent."""
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", oi=1500)])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", oi=2200)])
    fusion = SourceFusion([a, b], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].open_interest == 2200


@pytest.mark.asyncio
async def test_canonical_event_id_for_multi_source_uses_fused_prefix() -> None:
    a, b = _make_scenario_sources()
    fusion = SourceFusion([a, b], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].event_id.startswith("fused:")


@pytest.mark.asyncio
async def test_canonical_exchange_is_empty_for_multi_source_bucket() -> None:
    a = SyntheticRawFlowSource("a", [_raw(source_id="a", exchange="CBOE")])
    b = SyntheticRawFlowSource("b", [_raw(source_id="b", exchange="ISE")])
    fusion = SourceFusion([a, b], _params(window_ms=2000))

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    assert out[0].exchange == ""
    assert set(out[0].source_agreement.exchanges_seen) == {"CBOE", "ISE"}


# ---------------------------------------------------------------------------
# Missing IV / OI handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_source_supplies_iv_yields_none() -> None:
    """Phase 3.5.5.3: a bucket with no IV on any source no longer
    raises — the canonical print carries ``implied_volatility=None``.
    A pure ThetaData replay has no historical print-level IV; print
    IV has no downstream scoring consumer, so None is the honest
    value rather than a rejection or a fabricated default."""
    raw_no_iv = RawPrint(
        source_id="a",
        source_event_id="a-0",
        timestamp=_BASE_TS,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal("100"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        implied_volatility=None,
        open_interest=1500,
    )
    src = SyntheticRawFlowSource("a", [raw_no_iv])
    fusion = SourceFusion([src], _params(), force_multi_source=True)

    fused = [p async for p in fusion.stream()]
    assert len(fused) == 1
    assert fused[0].implied_volatility is None


@pytest.mark.asyncio
async def test_no_source_supplies_oi_yields_none() -> None:
    """Phase 3.5.5.3: a bucket with no OI on any source no longer
    raises — the canonical print carries ``open_interest=None``. The
    thin-OI penalty skips on None (unknown is not 'thin'); M28's OI
    delta re-fetches via its provider rather than the print."""
    raw_no_oi = RawPrint(
        source_id="a",
        source_event_id="a-0",
        timestamp=_BASE_TS,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("200"),
        expiry=date(2025, 7, 18),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal("100"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="above_ask",
        exchange="CBOE",
        is_iso=False,
        implied_volatility=0.45,
        open_interest=None,
    )
    src = SyntheticRawFlowSource("a", [raw_no_oi])
    fusion = SourceFusion([src], _params(), force_multi_source=True)

    fused = [p async for p in fusion.stream()]
    assert len(fused) == 1
    assert fused[0].open_interest is None


# ---------------------------------------------------------------------------
# force_multi_source: exercises the watermark path with one source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_multi_source_routes_through_watermark_path() -> None:
    """With force_multi_source=True, single-source events go through windowing
    instead of the fast path. Tier reflects the bucket population.
    """
    src = SyntheticRawFlowSource(
        "a",
        [
            _raw(source_id="a", ts_offset_ms=0),
            _raw(source_id="a", ts_offset_ms=200, event_id_suffix="1"),
        ],
    )
    fusion = SourceFusion([src], _params(window_ms=1000), force_multi_source=True)

    out = [p async for p in fusion.stream()]
    # Both prints fuse into one canonical (same source twice; tier reflects
    # n=2 with all-agreement → unanimous per classify_agreement semantics).
    assert len(out) == 1
    assert out[0].source_agreement.confidence_tier == "unanimous"
    assert out[0].source_agreement.sources_seen == ("a", "a")
