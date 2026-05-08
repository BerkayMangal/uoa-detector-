"""Phase 3.3.5.4 CLI integration tests for ``--source live``.

Pins:
  - --source live without --live-tickers → BadParameter
  - --source live --feeds thetadata → BadParameter (Phase 4 gate)
  - --source live --feeds unknown → BadParameter
  - --source live --feeds unusual_whales without UW key → BadParameter
"""

from __future__ import annotations

from typer.testing import CliRunner

from uoa_detector.cli import app

runner = CliRunner()


def test_live_source_requires_live_tickers() -> None:
    result = runner.invoke(app, ["run", "--source", "live"])
    assert result.exit_code != 0
    assert "--live-tickers is required" in result.output


def test_live_source_thetadata_gated_until_phase_4(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """--feeds containing 'thetadata' produces a clear Phase-4 error."""
    monkeypatch.setenv("THETADATA_API_KEY", "test_key_value")
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test_uw_key")
    result = runner.invoke(app, [
        "run", "--source", "live",
        "--feeds", "thetadata",
        "--live-tickers", "AAPL",
    ])
    assert result.exit_code != 0
    assert "Phase 3.3.5" in result.output
    assert "not yet wired" in result.output


def test_live_source_unknown_feed_rejected() -> None:
    result = runner.invoke(app, [
        "run", "--source", "live",
        "--feeds", "polygon",
        "--live-tickers", "AAPL",
    ])
    assert result.exit_code != 0
    assert "unknown feed" in result.output


def test_live_source_missing_uw_key_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When --feeds unusual_whales is requested but no key, fail-fast."""
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    result = runner.invoke(app, [
        "run", "--source", "live",
        "--feeds", "unusual_whales",
        "--live-tickers", "AAPL",
    ])
    assert result.exit_code != 0
    assert "UNUSUAL_WHALES_API_KEY is not set" in result.output


def test_live_source_appears_in_help() -> None:
    """The --source help text mentions 'live' as a valid choice."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "live" in result.output
    assert "--feeds" in result.output
    assert "--live-tickers" in result.output
