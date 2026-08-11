"""Phase 3.2.3.5 — CLI integration tests for ``backtest report``.

End-to-end: drive ``run`` to populate a SQLite store, then drive
``backtest report --run-id`` against it and assert the rendered
output. Covers:
  - missing run_id → exit 1 + clear message
  - non-sqlite store rejected
  - happy path: report renders with expected metric labels and the
    correct overall PASS/FAIL based on min_trades
  - unknown --pnl value rejected
  - --pnl simple deferred to Phase 3.3 (clear deferral message)
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from uoa_detector.cli import app

runner = CliRunner()


def _populate_store(db_path: Path) -> None:
    """Run the historical synthetic fixture into a SQLite store."""
    result = runner.invoke(
        app,
        [
            "run",
            "--source", "historical",
            "--data-dir", "tests/fixtures/historical/synthetic",
            "--store", f"sqlite:{db_path}",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_report_requires_sqlite_store_when_run_id_missing(tmp_path: Path) -> None:
    """An unknown run_id triggers exit 1 + a clear 'no signals' message."""
    db_path = tmp_path / "empty.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "no-such-run",
            "--store", f"sqlite:{db_path}",
        ],
    )
    assert result.exit_code == 1
    assert "No signals found" in result.output
    assert "no-such-run" in result.output


def test_report_rejects_unknown_pnl_value(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
            "--pnl", "moonshot",
        ],
    )
    assert result.exit_code != 0
    assert "--pnl" in result.output


def test_report_simple_pnl_requires_replay_data(tmp_path: Path) -> None:
    """Phase 3.5.0.4: --pnl simple now works, but needs --replay-data."""
    db_path = tmp_path / "store.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
            "--pnl", "simple",
        ],
    )
    assert result.exit_code != 0
    assert "--replay-data is required" in result.output


def test_report_simple_pnl_prices_positioned_signals(tmp_path: Path) -> None:
    """--pnl simple runs end-to-end over the AAPL fixture.

    The AAPL fixture's positioned signals all exit off-data (their
    5-day windows land past the fixture's last quote), so SimplePnL
    marks them open — never fabricating a quote. The report still
    renders and exits 0.
    """
    db_path = tmp_path / "store.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
            "--pnl", "simple",
            "--replay-data", "tests/fixtures/historical/synthetic",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Backtest report" in result.output
    # Positioned AAPL signals price as open (exits off-data), so there
    # are 0 closed trades but a non-zero open count.
    assert "Total trades:       0" in result.output
    assert "Open trades:" in result.output


def test_report_simple_pnl_closes_trades_on_4cell_fixture(tmp_path: Path) -> None:
    """--pnl simple closes real trades over the engine-proof fixture.

    The 4-cell fixture is built with exit-day quotes, so its positioned
    entry signals close (2 winners + 2 losers across SPY + PLTR); the
    exit-day rows open. Total closed trades = 4.
    """
    db_path = tmp_path / "store4.db"
    result = runner.invoke(
        app,
        [
            "run",
            "--source", "historical",
            "--data-dir", "tests/fixtures/historical/synthetic_4cell",
            "--store", f"sqlite:{db_path}",
            "--output", "json",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
            "--pnl", "simple",
            "--replay-data", "tests/fixtures/historical/synthetic_4cell",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Total trades:       4" in result.output


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_report_renders_table_with_open_trades(tmp_path: Path) -> None:
    """End-to-end: 10-row fixture → 10 open trades → FAIL on min_trades.

    Covers every section of the rendered report:
      - header line with run_id
      - Total / Open trade counts
      - per-metric pass/fail rows
      - OVERALL line
    """
    db_path = tmp_path / "store.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
        ],
    )
    assert result.exit_code == 0, result.output

    out = result.output

    # Header
    assert "Backtest report" in out
    assert "implicit-default" in out

    # Trade counts — all 10 fixture rows are open trades (NoOp PnL)
    assert "Total trades:       0" in out
    assert "Open trades:        10" in out

    # Per-metric rows present
    assert "sharpe" in out
    assert "expectancy" in out
    assert "walk_forward_consistency" in out
    assert "max_drawdown" in out
    assert "total_trades" in out

    # Insufficient sample where applicable
    assert "INSUFFICIENT_SAMPLE" in out

    # Overall verdict
    assert "OVERALL: FAIL" in out


def test_report_uses_walk_forward_windows_kwarg(tmp_path: Path) -> None:
    """--walk-forward-windows changes the partition count.

    With 10 trades and N=4 windows, the report doesn't crash even
    though all trades are open (consistency stays None / insufficient).
    The test confirms the flag is plumbed end-to-end.
    """
    db_path = tmp_path / "store.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
            "--walk-forward-windows", "4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OVERALL:" in result.output


def test_report_includes_threshold_column(tmp_path: Path) -> None:
    """Each rendered metric row carries its threshold value."""
    db_path = tmp_path / "store.db"
    _populate_store(db_path)

    result = runner.invoke(
        app,
        [
            "backtest", "report",
            "--run-id", "implicit-default",
            "--store", f"sqlite:{db_path}",
        ],
    )
    assert result.exit_code == 0, result.output
    out = result.output

    # Default thresholds appear as numeric strings.
    assert "1.5000" in out  # sharpe_pass
    assert "0.5000" in out  # expectancy_pass
    assert "0.7500" in out  # walk_forward_consistency_pass
    assert "0.2000" in out  # max_drawdown_ceiling
    assert "60.0000" in out  # min_trades
