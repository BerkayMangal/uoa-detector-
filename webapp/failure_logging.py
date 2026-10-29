"""One safe storage-failure record; preserve non-storage diagnostic tracebacks."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from webapp.live_alpha.cycle_status import safe_failure
from webapp.storage_diagnostics import native_hint

_MESSAGES = {
    "live-worker": "live-worker crashed; restarting in 30s",
    "gamma-refresh": "gamma-refresh crashed; restarting in 30s",
    "board-refresh": "board-refresh crashed; restarting in 30s",
    "board-prerender": "board-prerender crashed; restarting in 30s",
    "live-alpha-startup": "live-alpha crashed; restarting in 30s",
    "live-alpha-cycle": "live alpha cycle failed",
    "live-alpha-heartbeat": "live alpha heartbeat write failed",
    "gamma-loop": "gamma refresh loop error; backing off",
    "worker-cycle": "live worker error; restarting after backoff",
}


def log_failure(logger: logging.Logger, component: str, exc: Exception, stage: str) -> None:
    message = _MESSAGES.get(component, "background operation failed")
    if not isinstance(exc, SQLAlchemyError):
        logger.error(message, exc_info=(type(exc), exc, exc.__traceback__))
        return
    failure = safe_failure(exc, stage)
    hint = native_hint(exc)
    logger.error(
        "%s category=%s exception=%s stage=%s site=%s sqlstate=%s resource=%s database=%s",
        message,
        failure.category,
        failure.exception,
        failure.stage,
        failure.site,
        failure.sqlstate,
        failure.resource,
        hint.code.value if hint else None,
    )
