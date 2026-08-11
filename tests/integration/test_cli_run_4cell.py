"""Phase 3.2.4.4 — CLI integration tests for ``backtest run-4cell``.

Smoke + acceptance pins for the
``python -m uoa_detector backtest run-4cell`` subcommand:
  - validation: required flags, --to > --from, unknown trades mode
  - happy path: SQLite store has 4 distinct runs, report file
    written with all 4 cells + falsification section
  - **determinism (acceptance doc requirement)**: two runs with
    identical inputs produce byte-identical reports
  - --seed flag accepted (Phase 3.4+ wiring; currently no-op)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from uoa_detector.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_run_4cell_requires_store(tmp_path: Path) -> None:
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2024-01-01", "--to", "2026-01-01",
        ],
    )
    assert result.exit_code != 0
    assert "--store" in result.output


def test_run_4cell_requires_report_path(tmp_path: Path) -> None:
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/x.db",
            "--from", "2024-01-01", "--to", "2026-01-01",
        ],
    )
    assert result.exit_code != 0
    assert "--report-path" in result.output


def test_run_4cell_rejects_inverted_period(tmp_path: Path) -> None:
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/x.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2026-01-01",
            "--to", "2024-01-01",
        ],
    )
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "must be after" in flat


def test_run_4cell_rejects_invalid_iso_date(tmp_path: Path) -> None:
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/x.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "yesterday",
            "--to", "today",
        ],
    )
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "ISO" in flat


def test_run_4cell_rejects_unknown_trades_mode(tmp_path: Path) -> None:
    """Phase 3.5.0.3: 'historical' is now a valid producer, so the old

    'must be noop / Phase 3.3 deferral' message is gone. An UNKNOWN mode
    is still rejected, now naming the two valid modes.
    """
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/x.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2024-01-01", "--to", "2026-01-01",
            "--trades", "moonshot",
        ],
    )
    assert result.exit_code != 0
    # Typer box-wraps long error messages — strip the box-drawing
    # characters and whitespace before asserting on the message.
    cleaned = result.output.translate(
        str.maketrans({"│": " ", "╭": " ", "╮": " ", "╰": " ", "╯": " ", "─": " "}),
    )
    flat = " ".join(cleaned.split())
    assert "noop" in flat
    assert "historical" in flat


def test_run_4cell_historical_requires_replay_data(tmp_path: Path) -> None:
    """--trades historical without --replay-data is rejected up front."""
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/x.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2025-06-01", "--to", "2025-07-01",
            "--trades", "historical",
        ],
    )
    assert result.exit_code != 0
    cleaned = result.output.translate(
        str.maketrans({"│": " ", "╭": " ", "╮": " ", "╰": " ", "╯": " ", "─": " "}),
    )
    flat = " ".join(cleaned.split())
    assert "--replay-data is required" in flat


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_4cell_happy_path_writes_report_and_4_runs(tmp_path: Path) -> None:
    db = tmp_path / "4cell.db"
    report = tmp_path / "report.md"
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{db}",
            "--report-path", str(report),
            "--from", "2024-01-01", "--to", "2026-01-01",
        ],
    )
    assert result.exit_code == 0, result.output

    # Report file exists with expected sections.
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "# 4-cell comparison report" in content
    for cell in ("tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion"):
        assert f"`{cell}`" in content
    assert "What would falsify Formülasyon A" in content

    # SQLite store has 4 distinct runs.
    conn = sqlite3.connect(db)
    try:
        run_ids = sorted(
            r[0] for r in conn.execute("SELECT run_id FROM backtest_run")
        )
        assert run_ids == [
            "4cell-tier1_fusion", "4cell-tier1_single",
            "4cell-tier2_fusion", "4cell-tier2_single",
        ]
        # Each run has universe_id matching its cell name.
        rows = list(conn.execute(
            "SELECT run_id, universe_id FROM backtest_run",
        ))
        for run_id, universe_id in rows:
            expected = run_id.removeprefix("4cell-")
            assert universe_id == expected
    finally:
        conn.close()


def test_run_4cell_console_summary_includes_all_cells(tmp_path: Path) -> None:
    """Console output (stdout) shows a one-line summary per cell."""
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/r.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2024-01-01", "--to", "2026-01-01",
        ],
    )
    assert result.exit_code == 0
    for cell in ("tier1_single", "tier1_fusion", "tier2_single", "tier2_fusion"):
        assert cell in result.output


def test_run_4cell_creates_parent_dirs_for_report_path(tmp_path: Path) -> None:
    """--report-path's parent dir is created if missing."""
    nested = tmp_path / "deep" / "nested" / "report.md"
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/x.db",
            "--report-path", str(nested),
            "--from", "2024-01-01", "--to", "2026-01-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert nested.exists()


def test_run_4cell_walk_forward_windows_flag_accepted(tmp_path: Path) -> None:
    """--walk-forward-windows changes the partition count without crashing."""
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/r.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2024-01-01", "--to", "2026-01-01",
            "--walk-forward-windows", "4",
        ],
    )
    assert result.exit_code == 0, result.output


def test_run_4cell_seed_flag_accepted(tmp_path: Path) -> None:
    """--seed wired but no-op in 3.2.4. Test confirms the flag exists."""
    result = runner.invoke(
        app, [
            "backtest", "run-4cell",
            "--store", f"sqlite:{tmp_path}/r.db",
            "--report-path", str(tmp_path / "r.md"),
            "--from", "2024-01-01", "--to", "2026-01-01",
            "--seed", "42",
        ],
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Determinism — acceptance doc explicit requirement
# ---------------------------------------------------------------------------


def test_run_4cell_is_deterministic_byte_identical_reports(
    tmp_path: Path,
) -> None:
    """Acceptance doc: 'run the 4-cell suite twice with the same inputs,
    assert the SQLite content_hash of decision records is identical
    across runs.'

    Stronger pin here: the **markdown report** is byte-identical
    across runs. If anything in the rendering or metric pipeline
    introduces non-determinism (timestamps, dict ordering, etc.),
    this fails.
    """
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    report_a = tmp_path / "a.md"
    report_b = tmp_path / "b.md"

    args_a = [
        "backtest", "run-4cell",
        "--store", f"sqlite:{db_a}",
        "--report-path", str(report_a),
        "--from", "2024-01-01", "--to", "2026-01-01",
        "--seed", "42",
    ]
    args_b = [
        "backtest", "run-4cell",
        "--store", f"sqlite:{db_b}",
        "--report-path", str(report_b),
        "--from", "2024-01-01", "--to", "2026-01-01",
        "--seed", "42",
    ]

    result_a = runner.invoke(app, args_a)
    result_b = runner.invoke(app, args_b)
    assert result_a.exit_code == 0
    assert result_b.exit_code == 0

    bytes_a = report_a.read_bytes()
    bytes_b = report_b.read_bytes()
    assert bytes_a == bytes_b, "4-cell report is not byte-deterministic"
