"""Phase 3.5.0.2 tests for the historical trade producer + the 4-cell
engine-proof fixture.

The producer replays historical parquet through the full detection
pipeline, filters to positioned signals (``max_r > 0``, pinned decision
#1), and prices each with ``SimplePnLProvider`` + ``ParquetExitQuoteProvider``.

Covers:
  - the committed 4-cell fixture matches its generator (pin)
  - the producer yields ≥ 1 CLOSED trade for every cell of the matrix
  - the winner / loser realized-R values are the pinned deterministic
    numbers (+0.408 / -0.442)
  - ``run_4cell_backtest`` with the historical producer + a data handle
    reports total_trades ≥ 1 per cell and is byte-for-byte deterministic
  - the producer requires a data handle
  - the widened producer signature stays backward compatible with the
    noop / fixture producers (4th arg defaults to None)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from uoa_detector.backtest import (
    CANONICAL_CELLS,
    BacktestDataHandle,
    BacktestStore,
    CellSpec,
    fixture_trade_producer,
    historical_trade_producer,
    noop_trade_producer,
    run_4cell_backtest,
)
from uoa_detector.backtest.parquet_schema import (
    make_synthetic_4cell_fixture,
    row_to_raw_print,
)
from uoa_detector.calibration import load_default_profile

_FIXTURE_DIR = Path("tests/fixtures/historical/synthetic_4cell")
_WIN_R = pytest.approx(0.408)
_LOSE_R = pytest.approx(-0.442)


def _handle() -> BacktestDataHandle:
    return BacktestDataHandle(
        data_dir=_FIXTURE_DIR,
        source_id="synthetic_replay",
    )


# ---------------------------------------------------------------------------
# Fixture pin
# ---------------------------------------------------------------------------


def test_4cell_fixture_files_exist() -> None:
    for ticker in ("SPY", "PLTR"):
        assert (_FIXTURE_DIR / ticker / "2025-06.parquet").exists(), (
            f"4-cell fixture missing for {ticker}. Regenerate with:\n"
            "  uv run python -c 'from pathlib import Path; from "
            "uoa_detector.backtest.parquet_schema import "
            "write_synthetic_4cell_fixture; "
            'write_synthetic_4cell_fixture(Path("tests/fixtures/'
            "historical/synthetic_4cell\"))'"
        )


def test_4cell_fixture_matches_generator() -> None:
    """The committed parquet round-trips to exactly the generator output."""
    expected = make_synthetic_4cell_fixture()
    for ticker, rows in expected.items():
        table = pq.read_table(_FIXTURE_DIR / ticker / "2025-06.parquet")
        reconstructed = [row_to_raw_print(r) for r in table.to_pylist()]
        assert reconstructed == rows


# ---------------------------------------------------------------------------
# Producer: closed trades per cell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell", CANONICAL_CELLS, ids=lambda c: c.name)
def test_producer_yields_closed_trade_per_cell(cell: CellSpec) -> None:
    profile = load_default_profile()
    trades = historical_trade_producer(cell, (), profile, _handle())
    closed = [t for t in trades if t.realized_r is not None]
    assert len(closed) >= 1, f"cell {cell.name} produced no closed trade"


def test_producer_winner_and_loser_values() -> None:
    """Every cell closes exactly one winner (+0.408) and one loser (-0.442)."""
    profile = load_default_profile()
    for cell in CANONICAL_CELLS:
        trades = historical_trade_producer(cell, (), profile, _handle())
        closed = sorted(
            (t.realized_r for t in trades if t.realized_r is not None),
        )
        assert closed == [_LOSE_R, _WIN_R]


def test_tier1_trades_spy_tier2_trades_pltr() -> None:
    """The universe filter routes SPY to Tier-1 cells, PLTR to Tier-2."""
    profile = load_default_profile()
    tier1 = historical_trade_producer(
        CellSpec(universe="tier1", fusion="single"), (), profile, _handle(),
    )
    tier2 = historical_trade_producer(
        CellSpec(universe="tier2", fusion="single"), (), profile, _handle(),
    )
    assert all(t.event_id.startswith("SPY-") for t in tier1)
    assert all(t.event_id.startswith("PLTR-") for t in tier2)


def test_producer_requires_data_handle() -> None:
    profile = load_default_profile()
    cell = CellSpec(universe="tier1", fusion="single")
    with pytest.raises(ValueError, match="requires a BacktestDataHandle"):
        historical_trade_producer(cell, (), profile, None)


# ---------------------------------------------------------------------------
# run_4cell_backtest integration
# ---------------------------------------------------------------------------


def test_run_4cell_with_historical_producer_has_trades() -> None:
    profile = load_default_profile()
    store = BacktestStore(strict_run_lifecycle=True)
    results = run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2025, 6, 1, tzinfo=UTC),
        period_end=datetime(2025, 7, 1, tzinfo=UTC),
        walk_forward_windows=4,
        trade_producer=historical_trade_producer,
        data_handle=_handle(),
    )
    store.close()
    assert len(results) == 4
    for r in results:
        assert r.metrics.total_trades >= 1, (
            f"cell {r.cell.name} has no closed trades"
        )


def test_run_4cell_historical_is_deterministic() -> None:
    profile = load_default_profile()

    def _run() -> tuple[tuple[str, int, float], ...]:
        store = BacktestStore(strict_run_lifecycle=True)
        results = run_4cell_backtest(
            profile=profile,
            store=store,
            period_start=datetime(2025, 6, 1, tzinfo=UTC),
            period_end=datetime(2025, 7, 1, tzinfo=UTC),
            walk_forward_windows=4,
            trade_producer=historical_trade_producer,
            data_handle=_handle(),
        )
        store.close()
        return tuple(
            (r.cell.name, r.metrics.total_trades, r.metrics.expectancy)
            for r in results
        )

    assert _run() == _run()


# ---------------------------------------------------------------------------
# Backward-compat: the widened signature does not break existing producers
# ---------------------------------------------------------------------------


def test_noop_producer_accepts_optional_handle() -> None:
    profile = load_default_profile()
    cell = CellSpec(universe="tier1", fusion="single")
    # Callable both with and without the 4th arg (default None).
    assert noop_trade_producer(cell, (), profile) == []
    assert noop_trade_producer(cell, (), profile, _handle()) == []


def test_fixture_producer_accepts_optional_handle() -> None:
    profile = load_default_profile()
    cell = CellSpec(universe="tier1", fusion="single")
    producer = fixture_trade_producer({})
    assert producer(cell, (), profile) == []
    assert producer(cell, (), profile, _handle()) == []
