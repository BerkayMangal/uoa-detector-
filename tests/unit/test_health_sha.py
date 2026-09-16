"""Phase 5.2.RAIL2: /health reports the deployed commit.

An autonomous deploy check must prove which build answered, so the endpoint
returns the Railway commit id (public, never a secret) alongside liveness.
"""

from __future__ import annotations

import pytest
from webapp.main import health


def test_health_reports_ok_and_the_deployed_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234567890")
    assert health() == {"ok": True, "sha": "abcdef1"}


def test_health_without_the_variable_says_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    assert health() == {"ok": True, "sha": "unknown"}
