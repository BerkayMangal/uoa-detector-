"""Phase 3.4.8.2 tests for M28Validator (post-event batch validator).

Pins:
  Pure _score_from_oi_delta:
    - delta > 0 → confirmed_score (1.0)
    - delta == 0 → ambiguous_score (0.5)
    - delta < 0 → closing_score (0.0)
    - operator override of confirmed_score honoured

  M28Validator integration:
    - skips signals with no opening_closing_score (older runs)
    - skips signals below min_m27_score_to_validate
    - skips contracts that expired by event date
    - validates qualifying signal: delta > 0 → confirmed
    - validates qualifying: delta == 0 → ambiguous
    - validates qualifying: delta < 0 → closing
    - T+1 OI returns None → marks pending (no score written)
    - prior-close OI returns None → marks pending
    - provider error → counted in errors, doesn't abort batch
    - timeout → counted in errors
    - ValidationStats counts all branches
    - Score is written via update_signal_score (visible via iter_records)
    - Multiple signals in one run all processed
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.pipeline.validators.m28_next_day_oi import (
    M28Validator,
    ValidationStats,
    _score_from_oi_delta,
)
from uoa_detector.providers.open_interest import OpenInterestSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_id: str = "e1",
    ticker: str = "AAPL",
    expiry: date = date(2024, 2, 16),
    opening_closing_score: float | None = 0.7,
) -> EnrichedEvent:
    op = OptionsPrint(
        event_id=event_id,
        timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
        ticker=ticker,
        option_type="call",
        strike=Decimal("150.00"),
        expiry=expiry,
        dte=(expiry - date(2024, 1, 15)).days,
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
    e.opening_closing_score = opening_closing_score
    return e


def _decision_and_size() -> tuple[LabelDecision, PositionSize]:
    return (
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _snap(oi: int) -> OpenInterestSnapshot:
    return OpenInterestSnapshot(
        ticker="AAPL",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        option_type="call",
        as_of=datetime(2024, 1, 16, 14, 30, tzinfo=UTC),
        open_interest=oi,
    )


class _StubProvider:
    """Configurable OpenInterestProvider for M28 validator tests.

    Returns prior_snapshot from at(); next_day_snapshot from next_day().
    """

    def __init__(
        self,
        *,
        prior_snapshot: OpenInterestSnapshot | None = None,
        next_day_snapshot: OpenInterestSnapshot | None = None,
        delay_seconds: float = 0.0,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._prior = prior_snapshot
        self._next_day = next_day_snapshot
        self._delay = delay_seconds
        self._raise = raise_on_call
        self.at_calls = 0
        self.next_day_calls = 0

    async def at(  # type: ignore[no-untyped-def]
        self, *, ticker, strike, expiry, option_type, when,
    ):
        del ticker, strike, expiry, option_type, when
        self.at_calls += 1
        if self._raise is not None:
            raise self._raise
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._prior

    async def next_day(  # type: ignore[no-untyped-def]
        self, *, ticker, strike, expiry, option_type, trade_date,
    ):
        del ticker, strike, expiry, option_type, trade_date
        self.next_day_calls += 1
        if self._raise is not None:
            raise self._raise
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._next_day


def _make_store_with_signals(*events: EnrichedEvent) -> tuple[BacktestStore, str]:
    profile = load_default_profile()
    store = BacktestStore()
    rid = store.start_run(profile=profile)
    decision, size = _decision_and_size()
    for e in events:
        store.add(e, decision, size)
    store.finish_run()
    return store, rid


# ---------------------------------------------------------------------------
# Pure _score_from_oi_delta function
# ---------------------------------------------------------------------------


def test_score_delta_positive_confirmed() -> None:
    profile = load_default_profile()
    score, branch = _score_from_oi_delta(
        next_day_oi=1500, prior_oi=1000, settings=profile.scoring.modules.m28,
    )
    assert score == 1.0
    assert branch == "confirmed"


def test_score_delta_zero_ambiguous() -> None:
    profile = load_default_profile()
    score, branch = _score_from_oi_delta(
        next_day_oi=1000, prior_oi=1000, settings=profile.scoring.modules.m28,
    )
    assert score == 0.5
    assert branch == "ambiguous"


def test_score_delta_negative_closing() -> None:
    profile = load_default_profile()
    score, branch = _score_from_oi_delta(
        next_day_oi=800, prior_oi=1000, settings=profile.scoring.modules.m28,
    )
    assert score == 0.0
    assert branch == "closing"


def test_score_operator_override_confirmed() -> None:
    """Operator may pin different confirmed_score."""
    from uoa_detector.calibration.profile import M28Settings
    settings = M28Settings(confirmed_score=0.8)
    score, _ = _score_from_oi_delta(
        next_day_oi=1500, prior_oi=1000, settings=settings,
    )
    assert score == 0.8


# ---------------------------------------------------------------------------
# M28Validator integration with BacktestStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validator_skips_signals_without_m27_score() -> None:
    """Signals with opening_closing_score=None are skipped."""
    profile = load_default_profile()
    store, rid = _make_store_with_signals(
        _make_event(event_id="e1", opening_closing_score=None),
    )
    provider = _StubProvider()
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.total_signals == 1
    assert stats.skipped_no_m27_score == 1
    assert stats.validated_count == 0
    assert provider.next_day_calls == 0
    store.close()


@pytest.mark.asyncio
async def test_validator_skips_signals_below_threshold() -> None:
    """Signals with opening_closing_score < 0.7 default are skipped."""
    profile = load_default_profile()
    store, rid = _make_store_with_signals(
        _make_event(event_id="e_low", opening_closing_score=0.5),
        _make_event(event_id="e_high", opening_closing_score=0.7),
    )
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=_snap(1500),
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.total_signals == 2
    assert stats.skipped_below_threshold == 1
    assert stats.confirmed == 1  # only e_high validated
    store.close()


@pytest.mark.asyncio
async def test_validator_skips_expired_contracts() -> None:
    """Signal whose expiry <= event_date can't be validated."""
    profile = load_default_profile()
    same_day_expiry = date(2024, 1, 15)  # = event_date
    store, rid = _make_store_with_signals(
        _make_event(event_id="e_expired", expiry=same_day_expiry),
    )
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=_snap(1500),
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.total_signals == 1
    assert stats.skipped_contract_expired == 1
    assert stats.validated_count == 0
    assert provider.next_day_calls == 0
    store.close()


@pytest.mark.asyncio
async def test_validator_confirmed_branch() -> None:
    profile = load_default_profile()
    store, rid = _make_store_with_signals(_make_event(event_id="e1"))
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=_snap(1500),  # +500
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.confirmed == 1
    assert stats.ambiguous == 0
    assert stats.closing == 0
    # Score visible via iter_records
    records = list(store.iter_records(rid))
    assert records[0].m28_confirmation_score == 1.0
    store.close()


@pytest.mark.asyncio
async def test_validator_ambiguous_branch() -> None:
    profile = load_default_profile()
    store, rid = _make_store_with_signals(_make_event(event_id="e1"))
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=_snap(1000),  # delta == 0
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.ambiguous == 1
    records = list(store.iter_records(rid))
    assert records[0].m28_confirmation_score == 0.5
    store.close()


@pytest.mark.asyncio
async def test_validator_closing_branch() -> None:
    profile = load_default_profile()
    store, rid = _make_store_with_signals(_make_event(event_id="e1"))
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=_snap(800),  # -200
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.closing == 1
    records = list(store.iter_records(rid))
    assert records[0].m28_confirmation_score == 0.0
    store.close()


@pytest.mark.asyncio
async def test_validator_t_plus_1_data_unavailable_marks_pending() -> None:
    """Provider returns None for next_day → no score written, pending counted."""
    profile = load_default_profile()
    store, rid = _make_store_with_signals(_make_event(event_id="e1"))
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=None,  # T+1 not yet published
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.pending_no_data == 1
    assert stats.validated_count == 0
    records = list(store.iter_records(rid))
    assert records[0].m28_confirmation_score is None
    store.close()


@pytest.mark.asyncio
async def test_validator_prior_oi_unavailable_marks_pending() -> None:
    """Prior-close OI returns None → also pending."""
    profile = load_default_profile()
    store, rid = _make_store_with_signals(_make_event(event_id="e1"))
    provider = _StubProvider(
        prior_snapshot=None,
        next_day_snapshot=_snap(1500),
    )
    validator = M28Validator(
        provider=provider, store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.pending_no_data == 1
    assert stats.validated_count == 0
    store.close()


@pytest.mark.asyncio
async def test_validator_provider_error_counted_not_aborting() -> None:
    """A single provider exception counted as error; batch continues."""
    profile = load_default_profile()
    store, rid = _make_store_with_signals(
        _make_event(event_id="e1"),
        _make_event(event_id="e2"),
    )

    call_count = {"n": 0}

    class _FlakyProvider:
        async def at(  # type: ignore[no-untyped-def]
            self, *, ticker, strike, expiry, option_type, when,
        ):
            del ticker, strike, expiry, option_type, when
            call_count["n"] += 1
            if call_count["n"] == 1:
                msg = "synthetic flake"
                raise RuntimeError(msg)
            return _snap(1000)

        async def next_day(  # type: ignore[no-untyped-def]
            self, *, ticker, strike, expiry, option_type, trade_date,
        ):
            del ticker, strike, expiry, option_type, trade_date
            return _snap(1500)

    validator = M28Validator(
        provider=_FlakyProvider(),  # type: ignore[arg-type]
        store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.total_signals == 2
    assert stats.errors == 1
    assert stats.confirmed == 1
    assert len(stats.error_messages) == 1
    assert "RuntimeError" in stats.error_messages[0]
    store.close()


@pytest.mark.asyncio
async def test_validator_timeout_counted_as_error() -> None:
    """Timeout (asyncio.TimeoutError) counted under errors."""
    profile = load_default_profile()
    new_m28 = profile.scoring.modules.m28.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m28": new_m28})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    store, rid = _make_store_with_signals(_make_event(event_id="e1"))
    provider = _StubProvider(
        prior_snapshot=_snap(1000),
        next_day_snapshot=_snap(1500),
        delay_seconds=0.5,
    )
    validator = M28Validator(
        provider=provider, store=store, profile=new_profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.errors == 1
    assert stats.confirmed == 0
    store.close()


@pytest.mark.asyncio
async def test_validator_multiple_signals_aggregated_stats() -> None:
    """Three signals across all three branches, aggregate stats correct."""
    profile = load_default_profile()
    store, rid = _make_store_with_signals(
        _make_event(event_id="e_conf"),
        _make_event(event_id="e_amb"),
        _make_event(event_id="e_close"),
    )

    # Need different OI per signal — wrap a per-call dispatcher.
    # Validator order is next_day() then at() per signal, so we
    # advance the index after at() (the second call).
    responses = {
        # event_id : (prior_oi, next_day_oi)
        "e_conf": (1000, 1500),
        "e_amb": (2000, 2000),
        "e_close": (3000, 2500),
    }
    sig_ids = ["e_conf", "e_amb", "e_close"]
    counter = {"n": 0}

    class _PerCallProvider:
        async def at(  # type: ignore[no-untyped-def]
            self, *, ticker, strike, expiry, option_type, when,
        ):
            del ticker, strike, expiry, option_type, when
            sig_id = sig_ids[counter["n"]]
            counter["n"] += 1  # advance after second call per signal
            return _snap(responses[sig_id][0])

        async def next_day(  # type: ignore[no-untyped-def]
            self, *, ticker, strike, expiry, option_type, trade_date,
        ):
            del ticker, strike, expiry, option_type, trade_date
            sig_id = sig_ids[counter["n"]]
            return _snap(responses[sig_id][1])

    validator = M28Validator(
        provider=_PerCallProvider(),  # type: ignore[arg-type]
        store=store, profile=profile,
    )
    stats = await validator.validate_run(rid)
    assert stats.total_signals == 3
    assert stats.confirmed == 1
    assert stats.ambiguous == 1
    assert stats.closing == 1
    assert stats.validated_count == 3
    store.close()


def test_validation_stats_validated_count_property() -> None:
    """validated_count = confirmed + ambiguous + closing."""
    s = ValidationStats(
        run_id="r1",
        confirmed=3, ambiguous=2, closing=1,
        skipped_no_m27_score=5, errors=2,
    )
    assert s.validated_count == 6


# Suppress unused imports when running in isolation
_ = timedelta
