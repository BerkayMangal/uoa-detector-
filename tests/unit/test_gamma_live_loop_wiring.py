"""Phase 4.41 regression test: the gamma refresh loop must build its catalyst provider.

Phase 4.40 constructed ``UnusualWhalesCatalystCalendarProvider(client, settings)``
positionally, but the provider's constructor is keyword-only. The resulting
TypeError was swallowed by the loop's broad ``except Exception`` and retried
every backoff, so in production the gamma / vol board never refreshed. This test
drives one loop iteration with the network, database and clock stubbed and
asserts the real provider object reaches ``refresh_all``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from webapp import gamma_live

from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)


class _Stop(BaseException):
    """Escapes the loop's ``except Exception`` so the test can end the loop."""


class _FakeRepo:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def reset(self) -> None:
        return None


@pytest.mark.asyncio
async def test_refresh_loop_builds_catalyst_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_refresh_all(
        client: object, tickers: list[str], repo: object, catalyst_provider: object,
    ) -> int:
        captured["provider"] = catalyst_provider
        raise _Stop

    async def _stop_sleep(_seconds: float) -> None:
        raise _Stop

    monkeypatch.setattr(gamma_live, "GammaRepo", _FakeRepo)
    monkeypatch.setattr(gamma_live, "is_market_open", lambda _now: True)
    monkeypatch.setattr(
        gamma_live,
        "Credentials",
        lambda: SimpleNamespace(
            unusual_whales_api_key=SecretStr("test-key"),
            require_unusual_whales_api_key=lambda: SecretStr("test-key"),
        ),
    )
    monkeypatch.setattr(gamma_live, "refresh_all", _fake_refresh_all)
    monkeypatch.setattr(gamma_live.asyncio, "sleep", _stop_sleep)

    with pytest.raises(_Stop):
        await gamma_live.gamma_refresh_loop(tickers=["SPY"], database_url="sqlite://")

    assert isinstance(captured.get("provider"), UnusualWhalesCatalystCalendarProvider), (
        "gamma_refresh_loop never reached refresh_all: provider construction failed"
    )
