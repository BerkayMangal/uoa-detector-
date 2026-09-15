"""Phase 5.2.A-fix7: telemetry write failures are counted, not only logged once (review RT-6).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Live worker": the
``DecisionRecordWriter`` never disturbs ingestion) and §4.2
(``alfa_stage_telemetry`` and ``alfa_print_meta``).

Pins:
  - each failed write increments ``AlfaTelemetryWriter.write_failures`` and the
    warning carries the running count;
  - a later successful write does not reset the count, and nothing raises;
  - a write with no active run or after ``close()`` is not a failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session
from webapp.board.telemetry import AlfaPrintMeta

from tests.unit.test_alfa_telemetry_writer import _record, _rows, _url, _writer

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_LOGGER = "webapp.board.telemetry"


def test_each_failed_write_is_counted_and_logged_with_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(self: Session) -> None:
        msg = "database is locked"
        raise RuntimeError(msg)

    url = _url(tmp_path)
    writer = _writer(url)
    assert writer.write_failures == 0
    monkeypatch.setattr(Session, "commit", _boom)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        writer.write(_record("evt-1"))
        writer.write(_record("evt-2"))
    monkeypatch.undo()
    assert writer.write_failures == 2
    assert "(1 write failures since this writer started)" in caplog.text
    assert "(2 write failures since this writer started)" in caplog.text

    writer.write(_record("evt-3"))
    assert writer.write_failures == 2
    assert [row.event_id for row in _rows(url, AlfaPrintMeta)] == ["evt-3"]
    writer.close()


def test_no_active_run_and_a_closed_writer_are_not_failures(tmp_path: Path) -> None:
    idle = _writer(_url(tmp_path, "idle.db"), run_id=None)
    idle.write(_record())
    assert idle.write_failures == 0
    idle.close()
    idle.write(_record())
    assert idle.write_failures == 0
