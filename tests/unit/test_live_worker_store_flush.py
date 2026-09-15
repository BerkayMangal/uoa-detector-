"""Phase 4.42 regression test: the live worker's store commits every signal.

SqliteBacktestStore batches 100 rows by default, which suits backtests. The live
worker only produces a few signals per minute, so with batching today's run was
invisible to the dashboard (the page read STALE) until the 100th signal, and
every restart or redeploy silently dropped up to 99 buffered signals. The worker
now opens its store with a flush threshold of 1: a separate reader (the
dashboard's SignalRepo) must see a signal immediately after ``add``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from webapp import worker
from webapp.repo import SignalRepo

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

_RUN_ID = "live-2026-09-14"


def _event() -> EnrichedEvent:
    op = OptionsPrint(
        event_id="live-e1",
        timestamp=datetime(2026, 9, 14, 16, 31, tzinfo=UTC),
        ticker="SPY",
        option_type="put",
        strike=Decimal("758"),
        expiry=date(2026, 9, 18),
        dte=4,
        spot_price=Decimal("764"),
        premium_paid=Decimal("250000"),
        option_price=Decimal("3.50"),
        implied_volatility=0.18,
        bid=Decimal("3.45"),
        ask=Decimal("3.55"),
        fill_side="at_ask",
        exchange="UNKNOWN",
        is_iso=True,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("unusual_whales",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


def _decision_and_size() -> tuple[LabelDecision, PositionSize]:
    return (
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _runs_seen_by_dashboard(url: str) -> dict[str, int]:
    return {r.run_id: r.count for r in SignalRepo(url).runs()}


def _add_one_signal(store: SqliteBacktestStore) -> None:
    store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN_ID)
    decision, size = _decision_and_size()
    store.add(_event(), decision, size)


def test_live_store_signal_is_visible_to_dashboard_immediately(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'live.db'}"
    store = worker._open_live_store(url)
    try:
        _add_one_signal(store)
        assert _runs_seen_by_dashboard(url) == {_RUN_ID: 1}
    finally:
        store.close()


def test_default_batched_store_hides_a_single_signal(tmp_path: Path) -> None:
    """Pins why the live worker must not use the backtest batching default."""
    url = f"sqlite:///{tmp_path / 'batched.db'}"
    store = SqliteBacktestStore(url)
    try:
        _add_one_signal(store)
        assert _runs_seen_by_dashboard(url) == {}
    finally:
        store.close()
