"""Multi-source synthetic scenario for CLI ``--multi-source`` mode.

Builds three ``SyntheticRawFlowSource`` instances (``syn_a``, ``syn_b``,
``syn_c``) emitting ``RawPrint`` events designed to exercise all four
``SourceAgreement.confidence_tier`` values when fused through
``SourceFusion`` with the v5 default 500ms window:

  - **Unanimous bucket**: 3 sources, all reporting essentially the same
    print (premium within 5% tolerance, same is_iso). Demonstrates the
    happy path where every source corroborates.

  - **Majority bucket**: 3 sources, 1 outlier on premium (way outside the
    5% tolerance). The two agreeing sources put fraction at 2/3 ≥ 0.5 → majority.

  - **Single bucket**: only 1 source emits a print at this fusion key.
    Demonstrates the single-source path within a multi-source run.

  - **Conflicted bucket**: 3 sources, all disagreeing on premium AND on
    is_iso. Agreement fraction below ``majority_fraction`` → conflicted.

All four buckets target ``ticker=AAPL`` for simplicity but use distinct
``(strike, expiry, option_type)`` so fusion treats them independently.
Timestamps within each bucket are within the 500ms window; timestamps
across buckets are spaced minutes apart so each closes deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from uoa_detector.domain.events import FillSide
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.sources.synthetic import SyntheticRawFlowSource

# Common timestamp anchor — 16:00 UTC (~12:00 ET) puts every event in the
# prime trading window so M39 doesn't apply outside-session weighting.
_BASE_TS = datetime(2025, 6, 11, 16, 0, tzinfo=UTC)


def _raw(
    *,
    source_id: str,
    event_id_suffix: str,
    ticker: str,
    strike: str,
    ts_offset_ms: int = 0,
    premium: str = "5000",
    is_iso: bool = False,
    exchange: str = "CBOE",
    fill_side: FillSide = "above_ask",
) -> RawPrint:
    """Construct a RawPrint for the multi-source scenario."""
    return RawPrint(
        source_id=source_id,
        source_event_id=f"{source_id}-{event_id_suffix}",
        timestamp=_BASE_TS + timedelta(milliseconds=ts_offset_ms),
        ticker=ticker,
        option_type="call",
        strike=Decimal(strike),
        expiry=datetime(2025, 7, 18, tzinfo=UTC).date(),
        dte=37,
        spot_price=Decimal("198"),
        premium_paid=Decimal(premium),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side=fill_side,
        exchange=exchange,
        is_iso=is_iso,
        implied_volatility=0.45,
        open_interest=1500,
    )


def multi_source_scenario() -> tuple[
    SyntheticRawFlowSource,
    SyntheticRawFlowSource,
    SyntheticRawFlowSource,
]:
    """Build the three flow sources for the multi-source CLI scenario.

    Returns ``(syn_a, syn_b, syn_c)``. Caller passes the tuple to
    ``Pipeline([syn_a, syn_b, syn_c], ...)``.
    """
    # Each bucket targets a distinct strike so fusion treats them as
    # independent fusion keys. Timestamps spaced minutes apart between
    # buckets so each closes via watermark advance before the next opens.

    # --- Bucket 1 (strike 200, t=0): UNANIMOUS ------------------------
    # All three sources report premium 5000 (within 5% tolerance) and
    # is_iso=False. classify_agreement → 'unanimous'.
    a_events = [
        _raw(source_id="syn_a", event_id_suffix="b1", ticker="AAPL",
             strike="200", ts_offset_ms=0, premium="5000"),
    ]
    b_events = [
        _raw(source_id="syn_b", event_id_suffix="b1", ticker="AAPL",
             strike="200", ts_offset_ms=20, premium="5050",
             exchange="ISE"),
    ]
    c_events = [
        _raw(source_id="syn_c", event_id_suffix="b1", ticker="AAPL",
             strike="200", ts_offset_ms=40, premium="5100",
             exchange="PHLX"),
    ]

    # --- Bucket 2 (strike 210, t=60s): MAJORITY -----------------------
    # syn_a and syn_b at premium 7000 (consensus); syn_c at 14000 — way
    # outside the 5% tolerance. Median = 7000; tolerance = ±350. syn_c
    # at 14000 disagrees on premium → 2/3 fraction = 0.667 ≥ 0.5 majority.
    bucket2_offset = 60_000  # 60 seconds — well past 500ms window
    a_events.append(_raw(
        source_id="syn_a", event_id_suffix="b2", ticker="AAPL",
        strike="210", ts_offset_ms=bucket2_offset, premium="7000"))
    b_events.append(_raw(
        source_id="syn_b", event_id_suffix="b2", ticker="AAPL",
        strike="210", ts_offset_ms=bucket2_offset + 30, premium="7000",
        exchange="ISE"))
    c_events.append(_raw(
        source_id="syn_c", event_id_suffix="b2", ticker="AAPL",
        strike="210", ts_offset_ms=bucket2_offset + 60, premium="14000",
        exchange="PHLX"))

    # --- Bucket 3 (strike 220, t=120s): SINGLE ------------------------
    # Only syn_a emits at this key. classify_agreement → 'single'.
    bucket3_offset = 120_000
    a_events.append(_raw(
        source_id="syn_a", event_id_suffix="b3", ticker="AAPL",
        strike="220", ts_offset_ms=bucket3_offset, premium="3000"))
    # syn_b and syn_c silent at this key.

    # --- Bucket 4 (strike 230, t=180s): CONFLICTED --------------------
    # Three sources, all disagreeing on premium (1000 / 5000 / 10000)
    # and on is_iso. Median = 5000, tolerance ±250.
    #   syn_a: 1000 (way below median) + is_iso=False
    #   syn_b: 5000 (matches median premium) + is_iso=True (modal tie → False)
    #   syn_c: 10000 (way above median) + is_iso=True
    # Modal is_iso ties False vs True → tie-breaks to False per
    # classify_agreement's deterministic rule.
    # Per-source agree (premium AND is_iso):
    #   syn_a: premium NO, is_iso YES (False) → no
    #   syn_b: premium YES, is_iso NO        → no
    #   syn_c: premium NO, is_iso NO          → no
    # 0/3 agree → < majority_fraction → conflicted.
    bucket4_offset = 180_000
    a_events.append(_raw(
        source_id="syn_a", event_id_suffix="b4", ticker="AAPL",
        strike="230", ts_offset_ms=bucket4_offset, premium="1000",
        is_iso=False))
    b_events.append(_raw(
        source_id="syn_b", event_id_suffix="b4", ticker="AAPL",
        strike="230", ts_offset_ms=bucket4_offset + 30, premium="5000",
        is_iso=True, exchange="ISE"))
    c_events.append(_raw(
        source_id="syn_c", event_id_suffix="b4", ticker="AAPL",
        strike="230", ts_offset_ms=bucket4_offset + 60, premium="10000",
        is_iso=True, exchange="PHLX"))

    return (
        SyntheticRawFlowSource("syn_a", a_events),
        SyntheticRawFlowSource("syn_b", b_events),
        SyntheticRawFlowSource("syn_c", c_events),
    )
