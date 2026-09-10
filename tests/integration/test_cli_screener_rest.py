"""Phase 3.7.4 integration test for `screener --source rest`.

Drives the full screener command with the REST source, but injects a FAKE
Unusual Whales client (no network, no credentials required beyond a dummy
env var). Verifies the one-shot REST fetch flows through the SAME pipeline
and digest as the other sources.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from uoa_detector.cli import app
from uoa_detector.observability.digest import DIGEST_INTENT
from uoa_detector.sources.unusual_whales.rest_flow import RECENT_FLOW_PATH

runner = CliRunner()


class _FakeClient:
    """Injected in place of UnusualWhalesClient — returns a canned payload.

    Phase 3.8: the screener now runs the live enrichment pipeline, so the
    enrichment providers (M21-M27) also call ``request_json`` against this
    same fake client. It records EVERY call as ``(path, params)`` so a test
    can assert the flow fetch specifically rather than "the last call", and
    returns ``{"data": []}`` for any non-flow path so every provider takes
    its graceful no-data branch (no network, no crash).
    """

    flow_payload: ClassVar[dict[str, Any]] = {"data": []}
    calls: ClassVar[list[tuple[str, dict[str, Any] | None]]] = []
    aclose_calls: ClassVar[int] = 0

    def __init__(self, **_kwargs: Any) -> None:
        # Accept api_key=..., settings=... like the real client.
        pass

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        type(self).calls.append((path, params))
        if path == RECENT_FLOW_PATH:
            # The one-shot flow fetch — serve the canned flow rows.
            return type(self).flow_payload
        # Any enrichment endpoint: empty → provider no-data branch.
        return {"data": []}

    async def aclose(self) -> None:
        type(self).aclose_calls += 1


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "rest_evt_1",
        "ticker": "AAPL",
        "executed_at": "2024-01-15T15:30:00Z",
        "option_type": "call",
        "strike": "150.00",
        "expiry": "2024-02-16",
        "premium": "250000.00",
        "price": "1.50",
        "bid": "1.45",
        "ask": "1.55",
        "side_classification": "bullish",
        "open_interest": 1000,
        "implied_volatility": 0.25,
        "exchange": "CBOE",
        "alert_type": "sweep",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.flow_payload = {
        "data": [
            _row(id="e1", ticker="AAPL"),
            _row(id="e2", ticker="MSFT", strike="400.00", option_type="put"),
        ],
    }
    _FakeClient.calls = []
    _FakeClient.aclose_calls = 0
    monkeypatch.setattr("uoa_detector.cli.UnusualWhalesClient", _FakeClient)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "dummy-test-key")


def test_screener_rest_renders_digest(tmp_path: Path) -> None:
    report = tmp_path / "digest.md"
    result = runner.invoke(
        app,
        [
            "screener",
            "--source", "rest",
            "--live-tickers", "AAPL,MSFT",
            "--report-path", str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    # The digest header + meta always render.
    assert DIGEST_INTENT in result.stdout
    assert "candidates:" in result.stdout
    # The markdown twin is written.
    assert report.exists()
    assert report.read_text().startswith("# Screener digest")
    # The one-shot REST flow fetch happened with the tickers (found among
    # all recorded calls, since Phase 3.8 enrichment providers also call the
    # shared client), and the client was cleanly closed by the CLI.
    flow_calls = [
        params
        for path, params in _FakeClient.calls
        if path == RECENT_FLOW_PATH and params is not None
    ]
    assert any(p.get("tickers") == "AAPL,MSFT" for p in flow_calls)
    assert _FakeClient.aclose_calls == 1


def test_screener_rest_emits_diagnostic_lines(tmp_path: Path) -> None:
    """Phase 3.8: one run prints flow + per-module health to stderr."""
    result = runner.invoke(
        app,
        [
            "screener",
            "--source", "rest",
            "--live-tickers", "AAPL,MSFT",
            "--report-path", str(tmp_path / "d.md"),
        ],
    )
    assert result.exit_code == 0, result.output
    err = result.stderr or result.output
    # Flow ingestion line: 2 canned rows fetched + mapped, none dropped.
    assert "flow rows fetched=2, mapped=2, dropped=0" in err
    # Per-module enrichment health line names every wired M-module.
    assert "enrichment:" in err
    for short in ("M21", "M22", "M23", "M24", "M25", "M26", "M27"):
        assert short in err


def test_screener_rest_top_n_and_strict(tmp_path: Path) -> None:
    """--top-n renders rows; --strict changes the candidate count meta."""
    top = runner.invoke(
        app,
        [
            "screener", "--source", "rest",
            "--live-tickers", "AAPL,MSFT",
            "--top-n", "1",
            "--report-path", str(tmp_path / "t.md"),
        ],
    )
    assert top.exit_code == 0, top.output
    # Exactly one candidate row rendered under --top-n 1.
    assert "candidates: 1" in top.stdout

    strict = runner.invoke(
        app,
        [
            "screener", "--source", "rest",
            "--live-tickers", "AAPL,MSFT",
            "--strict",
            "--report-path", str(tmp_path / "s.md"),
        ],
    )
    assert strict.exit_code == 0, strict.output


def test_screener_rest_requires_tickers() -> None:
    result = runner.invoke(app, ["screener", "--source", "rest"])
    assert result.exit_code != 0
    assert "--live-tickers" in (result.stderr or result.output)


def test_screener_rest_missing_key_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Force Credentials to report no UW key regardless of env / local .env.
    class _NoKeyCreds:
        unusual_whales_api_key = None

    monkeypatch.setattr(
        "uoa_detector.config.credentials.Credentials", _NoKeyCreds,
    )
    result = runner.invoke(
        app,
        [
            "screener",
            "--source", "rest",
            "--live-tickers", "AAPL",
            "--report-path", str(tmp_path / "d.md"),
        ],
    )
    assert result.exit_code != 0
    assert "UNUSUAL_WHALES_API_KEY" in (result.stderr or result.output)
