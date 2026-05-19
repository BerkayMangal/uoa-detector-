"""Phase 3.5.5 tests for ``replay_trade_producer``.

Covers:
  - the producer streams a ticker-month through the pipeline and
    returns a list of RealizedTrade without crashing
  - single and fusion cells both run end-to-end
  - ticker filtering: a cell whose universe has no downloaded data
    produces zero trades
  - _replay_stages: single → core subset, fusion → full pipeline
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from uoa_detector.backtest import replay_trade_producer
from uoa_detector.backtest.cell_runner import (
    CellSpec,
    _replay_stages,
    fusion_stages_with_uw,
)
from uoa_detector.backtest.parquet_schema import write_parquet
from uoa_detector.backtest.pnl_provider import RealizedTrade
from uoa_detector.backtest.walk_forward import equal_time_slices
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.domain.raw_print import RawPrint


def _print(*, ts: datetime, strike: Decimal, ticker: str) -> RawPrint:
    expiry = date(2025, 8, 15)
    return RawPrint(
        source_id="thetadata",
        source_event_id=f"e-{ticker}-{ts.isoformat()}-{strike}",
        timestamp=ts,
        ticker=ticker,
        option_type="call",
        strike=strike,
        expiry=expiry,
        dte=(expiry - ts.date()).days,
        spot_price=Decimal("0"),
        premium_paid=Decimal("5000"),
        option_price=Decimal("2.50"),
        bid=Decimal("2.40"),
        ask=Decimal("2.60"),
        fill_side="above_ask",
        exchange="CBOE",
        implied_volatility=0.45,
        open_interest=1000,
        is_iso=False,
        source_tags=(),
    )


def _write_month(data_dir: Path, ticker: str) -> None:
    prints = [
        _print(
            ts=datetime(2025, 7, 7 + i, 14, 30 + i, tzinfo=UTC),
            strike=Decimal("100") + Decimal(i),
            ticker=ticker,
        )
        for i in range(12)
    ]
    write_parquet(
        sorted(prints, key=lambda p: p.timestamp),
        data_dir / ticker / "2025-07.parquet",
    )


def _windows() -> tuple:
    return equal_time_slices(
        datetime(2025, 7, 1, tzinfo=UTC),
        datetime(2025, 7, 31, tzinfo=UTC),
        1,
    )


def test_replay_stages_single_is_core_subset() -> None:
    single = _replay_stages("single")
    fusion = _replay_stages("fusion")
    assert len(single) < len(fusion)
    # The single subset carries no M21-M27 enrichment stage.
    single_names = {type(s).__name__ for s in single}
    assert "DealerGammaStage" not in single_names
    assert "DealerGammaStage" in {type(s).__name__ for s in fusion}


def test_fusion_stages_with_uw_builds_full_enrichment() -> None:
    """Phase 3.5.5 B3: fusion_stages_with_uw wires real UW providers
    into the M21-M27 enrichment stages."""

    class _StubClient:
        """Stand-in — UW providers don't call the client at construction."""

    stages = fusion_stages_with_uw(_StubClient(), UnusualWhalesSettings())
    names = [type(s).__name__ for s in stages]
    assert len(stages) == 13
    for enrichment in (
        "DealerGammaStage", "EventCalendarStage", "PriceConfirmationStage",
        "IVExhaustionStage", "SectorPeerStage", "DarkPoolStage",
        "OpeningClosingStage",
    ):
        assert enrichment in names


def test_replay_stages_fusion_with_client_uses_uw() -> None:
    """With a client + settings, the fusion set is the full 13-stage
    UW-wired pipeline; without, it falls back to the NoOp default."""

    class _StubClient:
        pass

    wired = _replay_stages("fusion", _StubClient(), UnusualWhalesSettings())
    noop = _replay_stages("fusion", None, None)
    assert len(wired) == 13
    assert len(noop) == 13  # same stages, NoOp providers


def test_producer_returns_realized_trades(tmp_path: Path) -> None:
    _write_month(tmp_path, "AMD")
    producer = replay_trade_producer(
        tmp_path,
        tier1_tickers=("AMD",),
        tier2_tickers=("PLTR",),
    )
    profile = load_default_profile()
    windows = _windows()
    trades = producer(
        CellSpec(universe="tier1", fusion="single"), windows, profile,
    )
    assert isinstance(trades, list)
    assert all(isinstance(t, RealizedTrade) for t in trades)


def test_producer_single_and_fusion_both_run(tmp_path: Path) -> None:
    _write_month(tmp_path, "AMD")
    producer = replay_trade_producer(
        tmp_path,
        tier1_tickers=("AMD",),
        tier2_tickers=("PLTR",),
    )
    profile = load_default_profile()
    windows = _windows()
    single = producer(
        CellSpec(universe="tier1", fusion="single"), windows, profile,
    )
    fusion = producer(
        CellSpec(universe="tier1", fusion="fusion"), windows, profile,
    )
    assert isinstance(single, list)
    assert isinstance(fusion, list)


def test_cell_with_no_data_produces_zero_trades(tmp_path: Path) -> None:
    # Only tier-1 data on disk; the tier-2 cell has nothing to replay.
    _write_month(tmp_path, "AMD")
    producer = replay_trade_producer(
        tmp_path,
        tier1_tickers=("AMD",),
        tier2_tickers=("PLTR",),
    )
    profile = load_default_profile()
    trades = producer(
        CellSpec(universe="tier2", fusion="single"), _windows(), profile,
    )
    assert trades == []
