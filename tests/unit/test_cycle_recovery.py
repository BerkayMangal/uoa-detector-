"""A failed deployment cycle needs a safe cause and a bounded next attempt."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from webapp.live_alpha import cycle_status, job, store

from uoa_detector.live_alpha.calendar import MarketMode
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

NOW = datetime(2026, 10, 28, 22, tzinfo=UTC)
SECRET = "secret-token https://provider.invalid/private?ticker=PRIVATE"


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (ValueError(SECRET), "invalid_data"),
        (RuntimeError(SECRET), "runtime"),
        (TimeoutError(SECRET), "timeout"),
        (KeyError(SECRET), "runtime"),
        (httpx.ConnectError(SECRET), "network"),
        (UnusualWhalesAuthError(SECRET), "auth"),
        (UnusualWhalesNotFoundError(SECRET, status_code=404), "data_unavailable"),
        (UnusualWhalesDailyLimitError(SECRET), "quota"),
        (UnusualWhalesRateLimitError(SECRET), "quota"),
        (UnusualWhalesTransientError(SECRET), "network"),
        (
            OperationalError(SECRET, {}, Exception("no such table: private_name")),
            "storage_missing_table",
        ),
        (OperationalError(SECRET, {}, Exception("database is locked")), "storage_busy"),
        (OperationalError(SECRET, {}, Exception("UNIQUE constraint failed")), "storage_conflict"),
    ],
)
def test_categories_never_expose_secret_messages_or_sql(exc: Exception, category: str) -> None:
    failure = cycle_status.safe_failure(exc, "scan")
    assert failure.category == category and failure.stage == "scan" and failure.site is None
    assert SECRET not in repr(failure) and "private_name" not in repr(failure)


def test_project_site_is_relative_and_unknown_exception_or_stage_is_generic() -> None:
    from webapp.research_readiness import aware

    try:
        aware(datetime(2026, 10, 28))
    except ValueError as exc:
        failure = cycle_status.safe_failure(exc, "calibration")
    assert failure.site.startswith("webapp/research_readiness.py:")
    assert "/workspace" not in failure.site and str(Path.home()) not in failure.site

    class SecretError(Exception):
        pass

    private = cycle_status.safe_failure(SecretError(SECRET), SECRET)
    assert (
        private.exception == "Exception"
        and private.category == "internal"
        and private.stage == "unknown"
    )


@pytest.mark.parametrize("mode", list(MarketMode))
def test_failed_cadence_and_successful_cadence_use_only_existing_profile(mode: MarketMode) -> None:
    s = load_live_alpha_settings()
    assert cycle_status.next_wait(s, "failed", mode) == min(
        s.cycle.live_seconds, s.cycle.closed_seconds
    )
    expected = (
        s.cycle.live_seconds
        if mode in (MarketMode.LIVE, MarketMode.PREMARKET)
        else s.cycle.closed_seconds
    )
    assert cycle_status.next_wait(s, "ok", mode) == expected
    assert cycle_status.next_wait(s, "degraded", mode) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["scan", "shadow", "desk", "calibration", "tournament"])
async def test_real_loop_reports_exact_failed_stage_and_clears_it_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    from webapp import gamma

    from tests.unit.test_live_alpha_job import FakeUW

    calls, waits = [], []
    checks = 0
    recovering = False
    row = store.ScanRow("test", NOW, NOW, "CLOSED", "ok", None, {"cards": []})

    def attempt(stage: str) -> None:
        calls.append(stage)
        if stage == failed_stage and not recovering:
            raise RuntimeError(SECRET)

    async def scan(*_: Any, **__: Any) -> store.ScanRow:
        attempt("scan")
        return row

    def hook(stage: str) -> Any:
        async def call(*_: Any) -> None:
            attempt(stage)

        return call

    async def check(*_: Any) -> None:
        nonlocal checks
        checks += 1

    async def pause(seconds: float) -> None:
        nonlocal recovering
        waits.append(seconds)
        # Capture failed cycle before next iteration overwrites it.
        assert job.LAST_CYCLE["failure"]["stage"] == failed_stage
        assert SECRET not in json.dumps(job.LAST_CYCLE)
        recovering = True

    monkeypatch.setattr(job, "LAST_CYCLE", {})
    monkeypatch.setattr(job, "run_cycle", scan)
    for name, stage in (
        ("run_shadow", "shadow"),
        ("run_desk", "desk"),
        ("run_calibration", "calibration"),
        ("run_tournament", "tournament"),
    ):
        monkeypatch.setattr(job, name, hook(stage))
    monkeypatch.setattr(job, "run_readiness", check)
    monkeypatch.setattr(gamma, "GammaRepo", lambda *_: SimpleNamespace(latest=lambda: None))
    client = FakeUW()
    await job.live_alpha_loop(
        database_url=f"sqlite:///{tmp_path / 'loop.db'}",
        client_factory=lambda: client,
        clock=lambda: NOW,
        sleep=pause,
        max_cycles=2,
    )
    assert waits == [load_live_alpha_settings().cycle.live_seconds]
    assert checks == 2 and not client.calls
    assert job.LAST_CYCLE["status"] == "ok" and "failure" not in job.LAST_CYCLE
