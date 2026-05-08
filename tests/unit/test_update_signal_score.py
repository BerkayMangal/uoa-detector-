"""Phase 3.4.8.1c tests for ``BacktestStoreProtocol.update_signal_score``.

Pins (BREAKING CHANGE):
  - In-memory store implements update_signal_score
  - SQLite store implements update_signal_score
  - Returns True if (run_id, event_id) matched + updated
  - Returns False if no matching signal
  - Setting value=None clears the score
  - Allowed score_name whitelist enforced (ValueError on unknown)
  - Updated value visible via iter_records
  - In-memory: update reflected in self._rows AND self._run_signals
  - SQLite: full_record_json updated; dedicated column synced for
    combined_score_pre/post
  - Idempotent: same (run, event, name, value) = same end state
  - Both implementations satisfy BacktestStoreProtocol post-extension
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from uoa_detector.backtest.protocol import BacktestStoreProtocol
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize


def _make_event(event_id: str = "e1") -> EnrichedEvent:
    op = OptionsPrint(
        event_id=event_id,
        timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        ticker="AAPL",
        option_type="call",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=32,
        spot_price=Decimal("150.00"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.25,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("synthetic",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    e = EnrichedEvent(print=op)
    e.opening_closing_score = 0.7
    return e


def _decision_and_size() -> tuple[LabelDecision, PositionSize]:
    from uoa_detector.domain.risk import RiskBucket
    return (
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


def test_in_memory_store_satisfies_protocol_after_extension() -> None:
    store = BacktestStore()
    assert isinstance(store, BacktestStoreProtocol)


def test_sqlite_store_satisfies_protocol_after_extension() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        assert isinstance(store, BacktestStoreProtocol)
        store.close()


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


def test_in_memory_update_signal_score_returns_true_on_match() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    store.add(_make_event(), decision, size)
    ok = store.update_signal_score(
        rid, "e1", "m28_confirmation_score", 1.0,
    )
    assert ok is True
    store.finish_run()
    store.close()


def test_in_memory_update_signal_score_returns_false_on_miss() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    store.add(_make_event(), decision, size)
    ok = store.update_signal_score(
        rid, "nonexistent_event_id", "m28_confirmation_score", 1.0,
    )
    assert ok is False
    store.finish_run()
    store.close()


def test_in_memory_update_signal_score_visible_via_iter_records() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    store.add(_make_event(), decision, size)
    store.update_signal_score(rid, "e1", "m28_confirmation_score", 0.5)
    store.finish_run()
    records = list(store.iter_records(rid))
    assert len(records) == 1
    assert records[0].m28_confirmation_score == 0.5
    store.close()


def test_in_memory_update_signal_score_with_none_clears() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    event = _make_event()
    event.m28_confirmation_score = 1.0
    store.add(event, decision, size)
    store.update_signal_score(rid, "e1", "m28_confirmation_score", None)
    store.finish_run()
    records = list(store.iter_records(rid))
    assert records[0].m28_confirmation_score is None
    store.close()


def test_in_memory_update_signal_score_unknown_field_raises() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    store.add(_make_event(), decision, size)
    with pytest.raises(ValueError, match="not in the allowed set"):
        store.update_signal_score(rid, "e1", "not_a_field", 1.0)
    store.finish_run()
    store.close()


def test_in_memory_update_signal_score_idempotent() -> None:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    store.add(_make_event(), decision, size)
    store.update_signal_score(rid, "e1", "m28_confirmation_score", 1.0)
    store.update_signal_score(rid, "e1", "m28_confirmation_score", 1.0)
    store.finish_run()
    records = list(store.iter_records(rid))
    assert records[0].m28_confirmation_score == 1.0
    store.close()


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


def test_sqlite_update_signal_score_round_trips() -> None:
    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        rid = store.start_run(profile=profile)
        decision, size = _decision_and_size()
        store.add(_make_event(), decision, size)
        ok = store.update_signal_score(
            rid, "e1", "m28_confirmation_score", 1.0,
        )
        assert ok is True
        store.finish_run()
        records = list(store.iter_records(rid))
        assert len(records) == 1
        assert records[0].m28_confirmation_score == 1.0
        store.close()


def test_sqlite_update_signal_score_returns_false_on_miss() -> None:
    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        rid = store.start_run(profile=profile)
        decision, size = _decision_and_size()
        store.add(_make_event(), decision, size)
        ok = store.update_signal_score(
            rid, "nonexistent", "m28_confirmation_score", 1.0,
        )
        assert ok is False
        store.finish_run()
        store.close()


def test_sqlite_update_signal_score_unknown_field_raises() -> None:
    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        rid = store.start_run(profile=profile)
        decision, size = _decision_and_size()
        store.add(_make_event(), decision, size)
        with pytest.raises(ValueError, match="not in the allowed set"):
            store.update_signal_score(rid, "e1", "not_a_field", 1.0)
        store.finish_run()
        store.close()


def test_sqlite_update_signal_score_syncs_dedicated_columns() -> None:
    """combined_score_pre/post live in BOTH JSON and SQL columns; both update."""
    from sqlalchemy import select  # local import for verification

    from uoa_detector.backtest.sqlite_store import SignalRow

    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        rid = store.start_run(profile=profile)
        decision, size = _decision_and_size()
        store.add(_make_event(), decision, size)
        store.update_signal_score(
            rid, "e1", "combined_score_pre_penalty", 0.42,
        )
        store.finish_run()
        # Verify the dedicated SQL column was synced
        with store._session_factory() as session:
            stmt = select(SignalRow).where(SignalRow.event_id == "e1")
            row = session.execute(stmt).scalar_one()
            assert row.combined_score_pre == 0.42
        store.close()


def test_sqlite_update_signal_score_with_none_clears() -> None:
    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SqliteBacktestStore(database_url=f"sqlite:///{db_path}")
        rid = store.start_run(profile=profile)
        decision, size = _decision_and_size()
        event = _make_event()
        event.m28_confirmation_score = 1.0
        store.add(event, decision, size)
        store.update_signal_score(rid, "e1", "m28_confirmation_score", None)
        store.finish_run()
        records = list(store.iter_records(rid))
        assert records[0].m28_confirmation_score is None
        store.close()
