"""Storage restart errors stay single-line, useful and free of native secrets."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError
from webapp import main
from webapp.failure_logging import log_failure
from webapp.live_alpha import job

from tests.unit.test_startup_visibility import StopSupervisorError

SECRET = "postgres://PRIVATE-user:PRIVATE-password@PRIVATE-host/private-db"


def outage() -> OperationalError:
    return OperationalError(
        "PRIVATE SQL",
        {"private": SECRET},
        Exception(
            "SSL error: unexpected eof while reading\nFATAL: the database system is in recovery mode "
            + SECRET
        ),
    )


@pytest.mark.parametrize(
    "component",
    [
        "live-worker",
        "gamma-refresh",
        "board-refresh",
        "board-prerender",
        "live-alpha-startup",
        "live-alpha-cycle",
        "live-alpha-heartbeat",
        "gamma-loop",
        "worker-cycle",
        "PRIVATE unknown\n",
    ],
)
def test_storage_failure_is_one_sanitized_record(
    component: str, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("fixture.failure")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        log_failure(logger, component, outage(), "startup")
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.exc_info is None and record.exc_text is None
    text = record.getMessage()
    assert "\n" not in text and "PRIVATE" not in text and "postgres://" not in text
    assert "database=recovery" in text and "exception=OperationalError" in text


def test_specific_code_still_visible_without_sql_payload(caplog: pytest.LogCaptureFixture) -> None:
    original = Exception("PRIVATE missing table")
    original.sqlstate = "42P01"
    with caplog.at_level(logging.ERROR):
        log_failure(
            logging.getLogger("fixture"),
            "worker-cycle",
            ProgrammingError(SECRET, {}, original),
            "scan",
        )
    assert "sqlstate=42P01" in caplog.text and "storage_missing_table" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_unexpected_programming_error_keeps_traceback(caplog: pytest.LogCaptureFixture) -> None:
    try:
        raise RuntimeError("controlled programming fixture")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR):
            log_failure(logging.getLogger("fixture"), "worker-cycle", exc, "scan")
    assert caplog.records[0].exc_info and "Traceback" in caplog.text


def test_actual_startup_failure_compacts_log_preserves_state_and_backoff(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(job, "LAST_CYCLE", {})

    async def fail(**kwargs: Any) -> None:
        raise outage()

    def stop(seconds: float) -> None:
        assert seconds == 30
        raise StopSupervisorError()

    monkeypatch.setattr(main, "live_alpha_loop", fail)
    monkeypatch.setattr(main.time, "sleep", stop)
    with caplog.at_level(logging.ERROR, logger="webapp.main"), pytest.raises(StopSupervisorError):
        main._live_alpha_thread_main(SECRET)
    relevant = [r for r in caplog.records if "live-alpha crashed" in r.getMessage()]
    assert len(relevant) == 1 and relevant[0].exc_info is None
    assert "PRIVATE" not in relevant[0].getMessage()
    assert job.LAST_CYCLE["status"] == "failed" and job.LAST_CYCLE["database_hint"] == "recovery"
