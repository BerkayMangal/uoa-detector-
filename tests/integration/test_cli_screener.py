"""Integration tests for the Phase 3.6 `screener` CLI command.

Exercises the synthetic path (no credentials, no ThetaData Terminal):
  - digest renders to stdout with the verbatim intent header + candidates
  - the markdown twin is written to --report-path
  - the existing `run` command is unaffected (regression guard)
  - argument validation matches `run`'s contract
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from uoa_detector.cli import app
from uoa_detector.observability.digest import DIGEST_INTENT, DIGEST_LEGEND

runner = CliRunner()


def test_screener_synthetic_renders_digest(tmp_path: Path) -> None:
    report = tmp_path / "digest.md"
    result = runner.invoke(
        app,
        ["screener", "--source", "synthetic", "--report-path", str(report)],
    )
    assert result.exit_code == 0, result.output
    # Verbatim intent header + legend on stdout.
    assert DIGEST_INTENT in result.stdout
    assert DIGEST_LEGEND in result.stdout
    # Column header present.
    assert "TOP REASONS" in result.stdout
    # The default synthetic scenario surfaces actionable candidates.
    assert "candidates:" in result.stdout
    assert "GOOGL" in result.stdout  # HIGH_CONVICTION_SEQUENCE, top-ranked


def test_screener_default_top_n_shows_non_actionable_rows(
    tmp_path: Path,
) -> None:
    """Phase 3.8: default (top-N) view shows every ranked signal with its label.

    A penalized/noise name (XYZ) is now visible AND marked, so the page is
    never hollow. Under --strict it is filtered out again.
    """
    report = tmp_path / "digest.md"
    default_run = runner.invoke(
        app,
        ["screener", "--source", "synthetic", "--report-path", str(report)],
    )
    assert default_run.exit_code == 0, default_run.output
    assert "XYZ" in default_run.stdout  # PENALIZED_BELOW_THRESHOLD, shown+marked

    strict_run = runner.invoke(
        app,
        [
            "screener", "--source", "synthetic",
            "--strict", "--report-path", str(report),
        ],
    )
    assert strict_run.exit_code == 0, strict_run.output
    assert "GOOGL" in strict_run.stdout  # actionable, still present
    assert "XYZ" not in strict_run.stdout  # non-actionable, filtered


def test_screener_writes_markdown_report(tmp_path: Path) -> None:
    report = tmp_path / "nested" / "digest.md"
    result = runner.invoke(
        app,
        ["screener", "--source", "synthetic", "--report-path", str(report)],
    )
    assert result.exit_code == 0, result.output
    assert report.exists()
    md = report.read_text()
    assert md.startswith("# Screener digest")
    assert "| # | Ticker | Side | Label |" in md
    assert DIGEST_INTENT in md


def test_screener_profile_id_in_header(tmp_path: Path) -> None:
    report = tmp_path / "digest.md"
    result = runner.invoke(
        app,
        [
            "screener",
            "--source", "synthetic",
            "--profile", "profiles/v5_gamma_squeeze.yaml",
            "--report-path", str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "profile: v5_gamma_squeeze" in result.stdout


def test_screener_historical_requires_data_dir() -> None:
    result = runner.invoke(app, ["screener", "--source", "historical"])
    assert result.exit_code != 0
    assert "--data-dir" in (result.stderr or result.output)


def test_screener_live_requires_tickers() -> None:
    result = runner.invoke(app, ["screener", "--source", "live"])
    assert result.exit_code != 0
    assert "--live-tickers" in (result.stderr or result.output)


def test_screener_bad_source_errors() -> None:
    result = runner.invoke(app, ["screener", "--source", "polygon"])
    assert result.exit_code != 0
    assert "synthetic" in (result.stderr or result.output)


def test_run_command_still_streams_raw_records() -> None:
    """Regression guard: `run` is unchanged — still emits per-record output."""
    result = runner.invoke(app, ["run", "--output", "json"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 8  # raw per-record stream, not a digest
