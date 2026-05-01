"""Tests for the CLI flag wiring (Phase 2.8).

Coverage:
  - ``--output pretty/json/parquet`` produce the right artefacts.
  - ``--output parquet`` requires ``--output-file``.
  - ``--output json`` produces NDJSON (one record per print).
  - ``--profile <path>`` loads a non-default profile (verified via the
    ``profile_id`` and ``profile_content_hash`` in the records).
  - ``--multi-source`` exercises 3 sources through fusion and shows
    all four ``confidence_tier`` values across the bucket sequence.

Tests invoke the typer CLI via ``CliRunner`` for fidelity with the user's
shell experience, but ``-m uoa_detector`` would behave identically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.loader import load_profile
from uoa_detector.cli import app

if TYPE_CHECKING:
    pass

runner = CliRunner()


# ---------------------------------------------------------------------------
# --output pretty (default)
# ---------------------------------------------------------------------------


def test_default_pretty_output_writes_to_stdout() -> None:
    """No flags — pretty output to stdout, structlog to stderr."""
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    # Pretty header markers
    assert "===" in result.stdout
    assert "decision:" in result.stdout
    assert "sub-scores:" in result.stdout
    # All 8 default-scenario events render.
    assert result.stdout.count("===") >= 8


def test_pretty_output_to_file(tmp_path: Path) -> None:
    out_path = tmp_path / "decisions.txt"
    result = runner.invoke(
        app,
        ["run", "--output", "pretty", "--output-file", str(out_path)],
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    content = out_path.read_text()
    assert "===" in content
    assert "decision:" in content


# ---------------------------------------------------------------------------
# --output json
# ---------------------------------------------------------------------------


def test_json_output_emits_valid_ndjson_to_stdout() -> None:
    """``--output json`` writes one JSON object per line to stdout."""
    result = runner.invoke(app, ["run", "--output", "json"])
    assert result.exit_code == 0, result.output

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    # Default scenario has 8 prints.
    assert len(lines) == 8

    # Each line must parse as a complete JSON object with the spec-required keys.
    for line in lines:
        obj = json.loads(line)
        for required_key in (
            "decision_emitted_at",
            "profile_id",
            "profile_content_hash",
            "event",
            "stage_executions",
            "score_breakdown",
            "decision",
            "size",
        ):
            assert required_key in obj, f"missing {required_key} in {line[:100]}..."

        # The event payload uses 'print' alias (not 'print_').
        assert "print" in obj["event"]
        assert "ticker" in obj["event"]["print"]


def test_json_output_to_file(tmp_path: Path) -> None:
    """``--output json --output-file PATH`` writes NDJSON to PATH, leaving
    stdout free for structlog logs.
    """
    out_path = tmp_path / "decisions.jsonl"
    result = runner.invoke(
        app,
        ["run", "--output", "json", "--output-file", str(out_path)],
    )
    assert result.exit_code == 0, result.output

    assert out_path.exists()
    lines = [line for line in out_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 8
    for line in lines:
        json.loads(line)  # must parse


# ---------------------------------------------------------------------------
# --output parquet
# ---------------------------------------------------------------------------


def test_parquet_output_requires_output_file() -> None:
    """Parquet is binary; cannot stream to stdout. Must error cleanly."""
    result = runner.invoke(app, ["run", "--output", "parquet"])
    assert result.exit_code != 0
    # Typer prints BadParameter messages to stderr.
    assert "requires --output-file" in (result.stderr or result.output)


def test_parquet_output_writes_readable_file(tmp_path: Path) -> None:
    """``--output parquet --output-file PATH`` writes a valid parquet file."""
    import pyarrow.parquet as pq

    out_path = tmp_path / "decisions.parquet"
    result = runner.invoke(
        app,
        [
            "run",
            "--output", "parquet",
            "--output-file", str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output

    assert out_path.exists()
    table = pq.read_table(out_path)
    # 8 prints in default scenario.
    assert table.num_rows == 8
    # Top-level scalars present.
    columns = set(table.column_names)
    for col in (
        "ticker", "label", "max_r",
        "combined_score_pre_penalty", "combined_score_post_penalty",
        "profile_id", "profile_content_hash",
    ):
        assert col in columns


# ---------------------------------------------------------------------------
# --profile
# ---------------------------------------------------------------------------


def test_profile_flag_loads_override() -> None:
    """``--profile <path>`` actually picks up the override.

    Verified by the ``profile_id`` and ``profile_content_hash`` fields in
    each emitted record. The override hash must differ from v5_default's.
    """
    override_path = Path("profiles/example_ticker_override.yaml")
    assert override_path.exists(), "fixture missing"

    result = runner.invoke(
        app,
        ["run", "--output", "json", "--profile", str(override_path)],
    )
    assert result.exit_code == 0, result.output

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 8

    override_profile = load_profile(override_path)
    default_hash = load_default_profile().content_hash()
    override_hash = override_profile.content_hash()
    assert override_hash != default_hash  # sanity: distinct profiles

    for line in lines:
        obj = json.loads(line)
        assert obj["profile_id"] == "example_ticker_override"
        assert obj["profile_content_hash"] == override_hash


def test_profile_flag_on_missing_file_errors() -> None:
    result = runner.invoke(
        app,
        ["run", "--profile", "/no/such/file.yaml"],
    )
    assert result.exit_code != 0
    assert "not found" in (result.stderr or result.output).lower()


# ---------------------------------------------------------------------------
# --multi-source
# ---------------------------------------------------------------------------


def test_multi_source_run_completes_with_all_four_tiers() -> None:
    """``--multi-source`` runs the 3-source scenario and the records
    show all four ``confidence_tier`` values across the four buckets.
    """
    result = runner.invoke(
        app,
        ["run", "--multi-source", "--output", "json"],
    )
    assert result.exit_code == 0, result.output

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    # 4 fusion buckets → 4 canonical OptionsPrints → 4 records.
    assert len(lines) == 4

    tiers_seen = set()
    for line in lines:
        obj = json.loads(line)
        sa = obj["event"]["print"]["source_agreement"]
        tiers_seen.add(sa["confidence_tier"])

    assert tiers_seen == {"unanimous", "majority", "single", "conflicted"}, (
        f"expected all 4 tiers; got {tiers_seen}"
    )


def test_multi_source_records_carry_correct_sources_seen() -> None:
    """Each tier corresponds to the right cardinality of sources_seen:
    unanimous/majority/conflicted are 3-source; single is 1-source.
    """
    result = runner.invoke(
        app,
        ["run", "--multi-source", "--output", "json"],
    )
    assert result.exit_code == 0

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    by_tier = {}
    for line in lines:
        obj = json.loads(line)
        sa = obj["event"]["print"]["source_agreement"]
        by_tier[sa["confidence_tier"]] = sa

    assert len(by_tier["unanimous"]["sources_seen"]) == 3
    assert len(by_tier["majority"]["sources_seen"]) == 3
    assert len(by_tier["conflicted"]["sources_seen"]) == 3
    assert len(by_tier["single"]["sources_seen"]) == 1


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_unsupported_source_errors() -> None:
    result = runner.invoke(app, ["run", "--source", "polygon"])
    assert result.exit_code != 0
    assert "synthetic" in (result.stderr or result.output)


def test_unsupported_scenario_errors() -> None:
    result = runner.invoke(app, ["run", "--scenario", "fancy"])
    assert result.exit_code != 0
    assert "default" in (result.stderr or result.output)
