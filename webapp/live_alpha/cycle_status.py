"""Bounded retry timing and safe failure hints, never exception messages/payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from uoa_detector.live_alpha.calendar import MarketMode
from uoa_detector.live_alpha.settings import LiveAlphaSettings
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_ROOT = Path(__file__).resolve().parents[2]
_STAGES = {"startup", "scan", "shadow", "desk", "calibration", "tournament"}
_SQLSTATES = {
    **dict.fromkeys(
        ("08000", "08001", "08003", "08004", "08006", "08007", "08P01", "57P01", "57P02", "57P03"),
        "storage_connection",
    ),
    **dict.fromkeys(("53100", "53200", "53300", "53400", "54000", "54001"), "storage_capacity"),
    "57014": "storage_timeout",
    "55P03": "storage_busy",
    "40P01": "storage_busy",
    "40001": "storage_conflict",
    "23505": "storage_conflict",
    "25006": "storage_readonly",
    "42P01": "storage_missing_table",
    "28P01": "storage_auth",
    "42501": "storage_permission",
    "58030": "storage_io",
}
_CLASSES = {
    "Exception",
    "RuntimeError",
    "ValueError",
    "TypeError",
    "AttributeError",
    "KeyError",
    "TimeoutError",
    "OperationalError",
    "IntegrityError",
    "ProgrammingError",
    "InvalidRequestError",
    "StatementError",
    "DatabaseError",
    "InterfaceError",
    "UnusualWhalesAuthError",
    "UnusualWhalesNotFoundError",
    "UnusualWhalesDailyLimitError",
    "UnusualWhalesRateLimitError",
    "UnusualWhalesTransientError",
    "CircuitBreakerOpenError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "ReadError",
    "HTTPStatusError",
}


@dataclass(frozen=True)
class Failure:
    stage: str
    category: str
    exception: str
    site: str | None
    sqlstate: str | None = None
    connection_invalidated: bool = False


def database_status(exc: Exception) -> tuple[str | None, bool]:
    if not isinstance(exc, DBAPIError):
        return None, False
    try:
        raw = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    except Exception:
        raw = None
    return (
        raw if isinstance(raw, str) and raw in _SQLSTATES else None,
        exc.connection_invalidated is True,
    )


def _category(exc: Exception) -> str:
    if isinstance(exc, SQLAlchemyError):
        code, invalidated = database_status(exc)
        if code is not None:
            return _SQLSTATES[code]
        if invalidated:
            return "storage_connection"
        text = str(exc).lower()  # Inspect locally; no part of this text is ever returned.
        if "database or disk is full" in text:
            return "storage_capacity"
        if "readonly database" in text or "read-only database" in text:
            return "storage_readonly"
        if "disk i/o error" in text:
            return "storage_io"
        if "no such table" in text or "undefinedtable" in text:
            return "storage_missing_table"
        if "database is locked" in text or "database table is locked" in text:
            return "storage_busy"
        if "unique constraint" in text or "duplicate key" in text:
            return "storage_conflict"
        return "storage"
    if isinstance(exc, UnusualWhalesNotFoundError):
        return "data_unavailable"
    if isinstance(exc, UnusualWhalesAuthError):
        return "auth"
    if isinstance(exc, UnusualWhalesDailyLimitError | UnusualWhalesRateLimitError):
        return "quota"
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError | UnusualWhalesTransientError | CircuitBreakerOpenError):
        return "network"
    if isinstance(exc, ValueError):
        return "invalid_data"
    if isinstance(exc, RuntimeError | TypeError | AttributeError | KeyError):
        return "runtime"
    return "internal"


def safe_failure(exc: Exception, stage: str) -> Failure:
    site = None
    try:
        tb = exc.__traceback__
        while tb is not None:
            path = Path(tb.tb_frame.f_code.co_filename).resolve()
            if (
                (
                    path.is_relative_to(_ROOT / "webapp")
                    or path.is_relative_to(_ROOT / "src/uoa_detector")
                )
                and path.suffix == ".py"
                and path.is_file()
            ):
                site = f"{path.relative_to(_ROOT).as_posix()}:{tb.tb_lineno}"
            tb = tb.tb_next
        category = _category(exc)
    except Exception:
        category = "internal"
    name = type(exc).__name__
    code, invalidated = database_status(exc)
    return Failure(
        stage if stage in _STAGES else "unknown",
        category,
        name if name in _CLASSES else "Exception",
        site,
        code,
        invalidated,
    )


def next_wait(settings: LiveAlphaSettings, status: str, mode: MarketMode) -> int:
    if mode in (MarketMode.LIVE, MarketMode.PREMARKET):
        return settings.cycle.live_seconds
    if status == "failed":
        return min(settings.cycle.live_seconds, settings.cycle.closed_seconds)
    return settings.cycle.closed_seconds
