"""Phase 3.2.3.2 tests for the ``PnLProvider`` Protocol + trivial impls.

Covers:
  - both Mock and NoOp satisfy the runtime-checkable Protocol
  - NoOp returns realized_r=None and exit_reason='holding_window_open'
  - Mock returns the registered fixture for known event_ids
  - Mock falls back to open-position default for unknown event_ids
  - RealizedTrade frozen-model contract (extra='forbid', frozen=True)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from uoa_detector.backtest import (
    BacktestStore,
    MockPnLProvider,
    NoOpPnLProvider,
    PnLProvider,
    RealizedTrade,
)
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_signal(event_id: str = "e1") -> tuple[BacktestStore, str]:
    store = BacktestStore()
    event = EnrichedEvent(
        print=OptionsPrint(
            event_id=event_id,
            timestamp=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
            ticker="AAPL",
            option_type="call",
            strike=Decimal("200"),
            expiry=date(2025, 7, 18),
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal("1000"),
            option_price=Decimal("1.50"),
            implied_volatility=0.45,
            bid=Decimal("1.45"),
            ask=Decimal("1.55"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=False,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )
    decision = LabelDecision(label=SignalLabel.STANDARD_UOA, reason="t")
    size = PositionSize(
        bucket=RiskBucket.STANDARD_UOA,
        max_r=0.75,
        scale_in=False,
    )
    store.add(event, decision, size)
    return store, event_id


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_noop_satisfies_protocol() -> None:
    assert isinstance(NoOpPnLProvider(), PnLProvider)


def test_mock_satisfies_protocol() -> None:
    assert isinstance(MockPnLProvider({}), PnLProvider)


# ---------------------------------------------------------------------------
# NoOp: returns open-position for everything
# ---------------------------------------------------------------------------


def test_noop_returns_open_position_for_any_signal() -> None:
    store, event_id = _build_signal("event-noop")
    signals = list(store.iter_records("implicit-default"))
    assert len(signals) == 1
    sig = signals[0]

    provider = NoOpPnLProvider()
    out = provider.provide(sig)

    assert out.event_id == event_id
    assert out.realized_r is None
    assert out.exit_ts is None
    assert out.exit_reason == "holding_window_open"
    assert out.entry_ts == sig.timestamp


def test_noop_is_deterministic() -> None:
    """Same input → same output."""
    store, _ = _build_signal("event-det")
    sig = next(iter(store.iter_records("implicit-default")))
    provider = NoOpPnLProvider()
    a = provider.provide(sig)
    b = provider.provide(sig)
    assert a == b


# ---------------------------------------------------------------------------
# Mock: returns registered fixtures, falls back for unknowns
# ---------------------------------------------------------------------------


def test_mock_returns_registered_fixture() -> None:
    store, _ = _build_signal("event-known")
    sig = next(iter(store.iter_records("implicit-default")))
    fixture = RealizedTrade(
        event_id="event-known",
        realized_r=2.5,
        entry_ts=sig.timestamp,
        exit_ts=sig.timestamp,
        exit_reason="fixed_window_elapsed",
    )
    provider = MockPnLProvider({"event-known": fixture})
    out = provider.provide(sig)
    assert out == fixture


def test_mock_falls_back_to_open_for_unknown_event_id() -> None:
    store, _ = _build_signal("event-unregistered")
    sig = next(iter(store.iter_records("implicit-default")))
    provider = MockPnLProvider({"some-other-id": RealizedTrade(
        event_id="some-other-id",
        realized_r=1.0,
        entry_ts=sig.timestamp,
        exit_ts=sig.timestamp,
        exit_reason="fixed_window_elapsed",
    )})
    out = provider.provide(sig)
    assert out.realized_r is None
    assert out.exit_reason == "holding_window_open"


def test_mock_does_not_share_state_across_instances() -> None:
    """Two MockPnLProvider instances built from the same fixture dict
    do not interfere if the dict is later mutated."""
    fixtures: dict[str, RealizedTrade] = {}
    p1 = MockPnLProvider(fixtures)
    fixtures["e1"] = RealizedTrade(
        event_id="e1",
        realized_r=1.0,
        entry_ts=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
        exit_ts=datetime(2025, 6, 16, 15, 30, tzinfo=UTC),
        exit_reason="fixed_window_elapsed",
    )
    p2 = MockPnLProvider(fixtures)

    store, _ = _build_signal("e1")
    sig = next(iter(store.iter_records("implicit-default")))
    # p1 was built before "e1" was added → unknown; p2 has it.
    assert p1.provide(sig).realized_r is None
    assert p2.provide(sig).realized_r == 1.0


# ---------------------------------------------------------------------------
# RealizedTrade — model contract
# ---------------------------------------------------------------------------


def test_realized_trade_frozen() -> None:
    """RealizedTrade is immutable after construction."""
    t = RealizedTrade(
        event_id="x",
        realized_r=1.0,
        entry_ts=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
        exit_ts=datetime(2025, 6, 16, 15, 30, tzinfo=UTC),
        exit_reason="fixed_window_elapsed",
    )
    with pytest.raises((TypeError, ValueError)):
        t.realized_r = 999.0  # type: ignore[misc]


def test_realized_trade_extra_fields_rejected() -> None:
    """Unknown fields raise; catches typos in test/library code."""
    with pytest.raises(Exception, match="extra"):
        RealizedTrade.model_validate({
            "event_id": "x",
            "realized_r": 1.0,
            "entry_ts": datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
            "exit_ts": datetime(2025, 6, 16, 15, 30, tzinfo=UTC),
            "exit_reason": "fixed_window_elapsed",
            "future_field": "this is not part of the schema",
        })


def test_realized_trade_open_position_shape() -> None:
    """Open position: realized_r=None, exit_ts=None,
    exit_reason='holding_window_open'."""
    t = RealizedTrade(
        event_id="x",
        realized_r=None,
        entry_ts=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
        exit_ts=None,
        exit_reason="holding_window_open",
    )
    assert t.realized_r is None
    assert t.exit_ts is None


def test_realized_trade_unknown_exit_reason_rejected() -> None:
    """exit_reason is a Literal — typos get caught at construction."""
    with pytest.raises(Exception, match="exit_reason"):
        RealizedTrade(
            event_id="x",
            realized_r=1.0,
            entry_ts=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
            exit_ts=datetime(2025, 6, 16, 15, 30, tzinfo=UTC),
            exit_reason="moonshot",  # type: ignore[arg-type]
        )
