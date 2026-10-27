"""The absence of a first completed cycle is not an invisible healthy startup."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from webapp import main
from webapp.live_alpha import job


class StopSupervisorError(BaseException):
    pass


def test_startup_failure_publishes_safe_driver_evidence_before_first_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "LAST_CYCLE", {})
    observed = []
    original = Exception("PRIVATE SQL AND PASSWORD")
    original.sqlstate = "08006"

    async def fail(**_: Any) -> None:
        observed.append(dict(job.LAST_CYCLE))
        assert main.health()["live_alpha"]["status"] == "starting"
        raise OperationalError("PRIVATE SQL", {"password": "PRIVATE"}, original)

    def pause(seconds: float) -> None:
        assert seconds == 30
        state = job.LAST_CYCLE
        assert state["status"] == "failed" and state["failure"]["stage"] == "startup"
        assert state["failure"]["sqlstate"] == "08006"
        assert "PRIVATE" not in json.dumps(main.health())
        raise StopSupervisorError()

    monkeypatch.setattr(main, "live_alpha_loop", fail)
    monkeypatch.setattr(main.time, "sleep", pause)
    with pytest.raises(StopSupervisorError):
        main._live_alpha_thread_main("PRIVATE DATABASE URL")
    assert observed[0]["status"] == "starting" and "failure" not in observed[0]


def test_unexpected_exit_is_not_left_as_a_successful_running_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "LAST_CYCLE", {})

    async def exited(**_: Any) -> None:
        return None

    def pause(seconds: float) -> None:
        assert seconds == 30
        assert job.LAST_CYCLE["status"] == "failed"
        assert job.LAST_CYCLE["failure"]["category"] == "runtime"
        assert job.LAST_CYCLE["failure"]["stage"] == "startup"
        raise StopSupervisorError()

    monkeypatch.setattr(main, "live_alpha_loop", exited)
    monkeypatch.setattr(main.time, "sleep", pause)
    with pytest.raises(StopSupervisorError):
        main._live_alpha_thread_main("sqlite://")


def test_restart_clears_old_failure_and_a_real_cycle_overwrites_starting_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "LAST_CYCLE", {"status": "failed", "failure": "old"})
    attempts = 0
    waits = []

    async def restart(**_: Any) -> None:
        nonlocal attempts
        attempts += 1
        assert job.LAST_CYCLE["status"] == "starting" and "failure" not in job.LAST_CYCLE
        if attempts == 1:
            raise RuntimeError("PRIVATE")
        # The actual loop stays alive after its first completed cycle.
        job.LAST_CYCLE.clear()
        job.LAST_CYCLE.update(status="ok", mode="CLOSED", cards=0)
        raise StopSupervisorError()

    monkeypatch.setattr(main, "live_alpha_loop", restart)
    monkeypatch.setattr(main.time, "sleep", waits.append)
    with pytest.raises(StopSupervisorError):
        main._live_alpha_thread_main("sqlite://")
    assert attempts == 2 and waits == [30]
    assert job.LAST_CYCLE == {"status": "ok", "mode": "CLOSED", "cards": 0}
