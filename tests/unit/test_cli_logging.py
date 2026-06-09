"""Phase 3.5 bug fix — `_configure_logging` honours UOA_LOG_LEVEL.

Pre-fix the level was hardcoded to INFO and `AppSettings.log_level`
(env `UOA_LOG_LEVEL`) was dead config, so a full-universe replay drowned
in per-signal INFO logs. These tests pin that the env now drives the
level and that a bad value falls back to INFO.
"""

from __future__ import annotations

import logging

import pytest
import structlog

from uoa_detector.cli import _configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> object:
    """Restore global logging/structlog state mutated by _configure_logging."""
    prev = logging.getLogger().level
    yield
    structlog.reset_defaults()
    logging.getLogger().setLevel(prev)


def test_configure_logging_defaults_to_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UOA_LOG_LEVEL", raising=False)
    _configure_logging(log_to_stderr=True)
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_honours_uoa_log_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UOA_LOG_LEVEL", "WARNING")
    _configure_logging(log_to_stderr=True)
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_bad_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UOA_LOG_LEVEL", "NOT_A_LEVEL")
    _configure_logging(log_to_stderr=True)
    assert logging.getLogger().level == logging.INFO
