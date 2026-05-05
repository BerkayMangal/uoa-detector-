"""Phase 3.2.4.2 tests for the 4-cell combinatorial runner.

Pins:
  - canonical 4-cell matrix (tier1/tier2 × single/fusion)
  - cell name composition matches acceptance ('tier1_single', etc.)
  - universe loader reads tier1_anchor.csv + tier2_starter.csv
    through the same code path
  - run_4cell_backtest produces 4 distinct run_ids in the store
  - each cell's RunMetadata carries universe_id = cell.name
  - fixture_trade_producer routes per-cell trade lists correctly
  - noop_trade_producer always returns []
  - cell runner is deterministic (two invocations with the same
    inputs produce identical CellRunResult tuples)
  - integration: both universes load through load_universe_tickers
    cleanly (acceptance doc 3.2.4 explicit requirement)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uoa_detector.backtest import (
    CANONICAL_CELLS,
    CELL_NAMES,
    BacktestStore,
    CellSpec,
    RealizedTrade,
    cell_name,
    fixture_trade_producer,
    load_universe_tickers,
    noop_trade_producer,
    run_4cell_backtest,
    universe_path,
)
from uoa_detector.calibration import load_default_profile

# ---------------------------------------------------------------------------
# Cell name + matrix shape pins
# ---------------------------------------------------------------------------


def test_cell_names_match_acceptance() -> None:
    """The 4 canonical cells use snake_case names per acceptance doc."""
    assert CELL_NAMES == (
        "tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion",
    )


def test_canonical_cells_match_acceptance() -> None:
    """The 4 cells in canonical order: (tier1, single), (tier1, fusion),
    (tier2, single), (tier2, fusion)."""
    expected = (
        CellSpec(universe="tier1", fusion="single"),
        CellSpec(universe="tier1", fusion="fusion"),
        CellSpec(universe="tier2", fusion="single"),
        CellSpec(universe="tier2", fusion="fusion"),
    )
    assert expected == CANONICAL_CELLS


def test_cell_name_composes_from_coordinates() -> None:
    assert cell_name("tier1", "single") == "tier1_single"
    assert cell_name("tier1", "fusion") == "tier1_fusion"
    assert cell_name("tier2", "single") == "tier2_single"
    assert cell_name("tier2", "fusion") == "tier2_fusion"


def test_cell_spec_name_property() -> None:
    spec = CellSpec(universe="tier2", fusion="fusion")
    assert spec.name == "tier2_fusion"


# ---------------------------------------------------------------------------
# Universe loader (acceptance: same loader for both tiers)
# ---------------------------------------------------------------------------


def test_universe_path_resolves_correctly() -> None:
    assert universe_path("tier1").name == "tier1_anchor.csv"
    assert universe_path("tier2").name == "tier2_starter.csv"


def test_load_universe_tier1_anchor_returns_20_tickers() -> None:
    """Tier-1 is the 20-ticker anchor universe approved in Phase 3 prep."""
    tickers = load_universe_tickers("tier1")
    assert len(tickers) == 20
    # Spot-check a few of the approved tickers.
    assert "SPY" in tickers
    assert "NVDA" in tickers
    assert "GLD" in tickers


def test_load_universe_tier2_starter_returns_more_tickers() -> None:
    """Tier-2 is the broader edge-watch universe."""
    tickers = load_universe_tickers("tier2")
    assert len(tickers) > 20  # Tier-2 is broader than Tier-1
    # First letter sanity: all uppercase, alphanumeric.
    for t in tickers:
        assert t.isupper()


def test_both_universes_load_through_same_loader() -> None:
    """Acceptance 3.2.4 explicit requirement: 'Tier-1 and Tier-2
    universes load cleanly through the same loader code path.'"""
    t1 = load_universe_tickers("tier1")
    t2 = load_universe_tickers("tier2")
    assert isinstance(t1, tuple)
    assert isinstance(t2, tuple)
    # Tier-1 and Tier-2 may overlap (mega-caps appear in both); just
    # confirm both loaded successfully.
    assert all(isinstance(t, str) for t in t1)
    assert all(isinstance(t, str) for t in t2)


def test_load_universe_uppercases_tickers() -> None:
    """Tickers come back uppercase regardless of how they're written
    in the source file."""
    tickers = load_universe_tickers("tier1")
    assert all(t == t.upper() for t in tickers)


# ---------------------------------------------------------------------------
# Cell runner end-to-end
# ---------------------------------------------------------------------------


def _make_trades(cell_name_suffix: str, count: int) -> list[RealizedTrade]:
    """Build a batch of realized trades tagged by cell name."""
    base = datetime(2024, 6, 1, tzinfo=UTC)
    return [
        RealizedTrade(
            event_id=f"{cell_name_suffix}-{i}",
            realized_r=1.0,
            entry_ts=base + timedelta(days=i),
            exit_ts=base + timedelta(days=i + 5),
            exit_reason="fixed_window_elapsed",
        )
        for i in range(count)
    ]


def test_run_4cell_backtest_produces_4_distinct_run_ids() -> None:
    """Four cells → four run_ids in the store, each with distinct
    universe_id matching cell.name."""
    profile = load_default_profile()
    store = BacktestStore()

    fixtures = {
        "tier1_single": _make_trades("t1s", 5),
        "tier1_fusion": _make_trades("t1f", 5),
        "tier2_single": _make_trades("t2s", 5),
        "tier2_fusion": _make_trades("t2f", 5),
    }
    producer = fixture_trade_producer(fixtures)

    results = run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2024, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
        walk_forward_windows=8,
        trade_producer=producer,
    )

    assert len(results) == 4
    run_ids = [r.run_id for r in results]
    assert len(set(run_ids)) == 4  # all distinct
    # Each result's cell matches the canonical order.
    for result, expected_cell in zip(results, CANONICAL_CELLS, strict=True):
        assert result.cell == expected_cell
        assert result.run_id == f"4cell-{expected_cell.name}"


def test_run_4cell_backtest_persists_universe_id_per_cell() -> None:
    """Each run's RunMetadata has universe_id set to cell.name."""
    profile = load_default_profile()
    store = BacktestStore()
    producer = fixture_trade_producer({})

    run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2024, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
        walk_forward_windows=8,
        trade_producer=producer,
    )

    runs = store.list_runs()
    universe_ids = sorted(r.universe_id for r in runs if r.universe_id)
    assert universe_ids == [
        "tier1_fusion", "tier1_single", "tier2_fusion", "tier2_single",
    ]


def test_run_4cell_backtest_metrics_per_cell() -> None:
    """Each cell's metrics reflect its trade fixture (different per cell)."""
    profile = load_default_profile()
    store = BacktestStore()

    fixtures = {
        "tier1_single": _make_trades("t1s", 5),
        "tier1_fusion": _make_trades("t1f", 30),  # larger sample
        "tier2_single": _make_trades("t2s", 5),
        "tier2_fusion": _make_trades("t2f", 30),
    }
    producer = fixture_trade_producer(fixtures)

    results = run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2024, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
        walk_forward_windows=8,
        trade_producer=producer,
    )

    by_cell = {r.cell.name: r for r in results}
    # Tier-1 single + Tier-2 single: only 5 trades → < 8 windows → consistency None
    assert by_cell["tier1_single"].metrics.total_trades == 5
    assert by_cell["tier2_single"].metrics.total_trades == 5
    # Tier-1 fusion + Tier-2 fusion: 30 trades → consistency computable
    assert by_cell["tier1_fusion"].metrics.total_trades == 30
    assert by_cell["tier2_fusion"].metrics.total_trades == 30


def test_run_4cell_backtest_is_deterministic() -> None:
    """Same inputs → identical results (Phase 3.2.4 acceptance)."""
    profile = load_default_profile()
    fixtures = {
        c.name: _make_trades(c.name, 30) for c in CANONICAL_CELLS
    }

    def _go() -> tuple:  # type: ignore[type-arg]
        store = BacktestStore()
        producer = fixture_trade_producer(fixtures)
        return run_4cell_backtest(
            profile=profile,
            store=store,
            period_start=datetime(2024, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 1, tzinfo=UTC),
            walk_forward_windows=8,
            trade_producer=producer,
        )

    a = _go()
    b = _go()
    assert len(a) == len(b)
    for ra, rb in zip(a, b, strict=True):
        assert ra.cell == rb.cell
        assert ra.run_id == rb.run_id
        assert ra.metrics == rb.metrics


def test_run_4cell_backtest_with_subset_of_cells() -> None:
    """The cells= kwarg lets you run a subset (e.g., one cell for testing)."""
    profile = load_default_profile()
    store = BacktestStore()
    fixtures = {"tier2_fusion": _make_trades("t2f", 10)}
    producer = fixture_trade_producer(fixtures)

    only_one = (CellSpec(universe="tier2", fusion="fusion"),)
    results = run_4cell_backtest(
        profile=profile,
        store=store,
        period_start=datetime(2024, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
        walk_forward_windows=8,
        trade_producer=producer,
        cells=only_one,
    )
    assert len(results) == 1
    assert results[0].cell.name == "tier2_fusion"


# ---------------------------------------------------------------------------
# Trade producers
# ---------------------------------------------------------------------------


def test_fixture_trade_producer_routes_by_cell_name() -> None:
    """Each cell receives the fixtures registered against its name."""
    fixtures = {
        "tier1_single": _make_trades("t1s", 3),
        "tier2_fusion": _make_trades("t2f", 7),
    }
    producer = fixture_trade_producer(fixtures)
    profile = load_default_profile()

    cell_t1s = CellSpec(universe="tier1", fusion="single")
    cell_t2f = CellSpec(universe="tier2", fusion="fusion")
    cell_t2s = CellSpec(universe="tier2", fusion="single")

    assert len(producer(cell_t1s, (), profile)) == 3
    assert len(producer(cell_t2f, (), profile)) == 7
    # Unregistered cell → empty list.
    assert producer(cell_t2s, (), profile) == []


def test_noop_trade_producer_always_empty() -> None:
    """noop_trade_producer returns [] for any cell."""
    profile = load_default_profile()
    for cell in CANONICAL_CELLS:
        assert noop_trade_producer(cell, (), profile) == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_cell_spec_extra_fields_rejected() -> None:
    with pytest.raises(Exception, match="extra"):
        CellSpec.model_validate({
            "universe": "tier1",
            "fusion": "single",
            "rogue_field": "X",
        })


def test_cell_spec_unknown_universe_rejected() -> None:
    with pytest.raises(Exception, match="universe"):
        CellSpec(universe="tier3", fusion="single")  # type: ignore[arg-type]


def test_cell_spec_unknown_fusion_rejected() -> None:
    with pytest.raises(Exception, match="fusion"):
        CellSpec(universe="tier1", fusion="quad")  # type: ignore[arg-type]
