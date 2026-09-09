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

runner = CliRunner()


class _FakeClient:
    """Injected in place of UnusualWhalesClient — returns a canned payload."""

    payload: ClassVar[dict[str, Any]] = {"data": []}
    last_params: ClassVar[dict[str, Any] | None] = None
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
        type(self).last_params = params
        return type(self).payload

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
    _FakeClient.payload = {
        "data": [
            _row(id="e1", ticker="AAPL"),
            _row(id="e2", ticker="MSFT", strike="400.00", option_type="put"),
        ],
    }
    _FakeClient.last_params = None
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
    # The one-shot REST fetch happened with the tickers, and the client was
    # cleanly closed by the CLI.
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["tickers"] == "AAPL,MSFT"
    assert _FakeClient.aclose_calls == 1


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
