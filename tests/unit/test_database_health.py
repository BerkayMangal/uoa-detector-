"""Known-dead pooled connections are not a valid input to a new write."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from webapp.board.db import make_engine
from webapp.live_alpha.cycle_status import safe_failure


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("08006", "storage_connection"),
        ("57P01", "storage_connection"),
        ("53100", "storage_capacity"),
        ("53200", "storage_capacity"),
        ("53300", "storage_capacity"),
        ("57014", "storage_timeout"),
        ("55P03", "storage_busy"),
        ("40P01", "storage_busy"),
        ("40001", "storage_conflict"),
        ("23505", "storage_conflict"),
        ("42P01", "storage_missing_table"),
        ("25006", "storage_readonly"),
        ("28P01", "storage_auth"),
        ("42501", "storage_permission"),
        ("58030", "storage_io"),
    ],
)
def test_safe_driver_state_classifies_without_exposing_sql_or_exception_text(
    code: str, category: str
) -> None:
    original = Exception("PRIVATE PASSWORD AND SQL")
    original.sqlstate = code
    exc = OperationalError("PRIVATE SQL", {"password": "PRIVATE"}, original)
    hint = safe_failure(exc, "scan")
    assert hint.category == category and hint.sqlstate == code
    assert "PRIVATE" not in json.dumps(asdict(hint))


@pytest.mark.parametrize("code", [None, "PRIVATE-SECRET", "ZZZZZ", "08006 PRIVATE", 8006, {}])
def test_unknown_or_malformed_state_is_omitted(code: object) -> None:
    original = SimpleNamespace(sqlstate=code, pgcode=None)
    hint = safe_failure(OperationalError("PRIVATE SQL", {}, original), "scan")
    assert hint.sqlstate is None and not hint.connection_invalidated
    assert "PRIVATE" not in json.dumps(asdict(hint))


def test_pgcode_fallback_and_connection_invalidated_are_driver_neutral() -> None:
    original = SimpleNamespace(pgcode="08003", sqlstate=None)
    hint = safe_failure(
        OperationalError("PRIVATE", {}, original, connection_invalidated=True), "scan"
    )
    assert hint.sqlstate == "08003" and hint.connection_invalidated
    disconnected = safe_failure(
        OperationalError("PRIVATE", {}, Exception("PRIVATE"), connection_invalidated=True), "scan"
    )
    assert disconnected.sqlstate is None and disconnected.category == "storage_connection"
    non_driver = safe_failure(RuntimeError("PRIVATE"), "scan")
    assert non_driver.sqlstate is None and not non_driver.connection_invalidated


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("database or disk is full", "storage_capacity"),
        ("attempt to write a readonly database", "storage_readonly"),
        ("disk I/O error", "storage_io"),
    ],
)
def test_sqlite_storage_category_survives_missing_sqlstate(message: str, category: str) -> None:
    hint = safe_failure(OperationalError("PRIVATE SQL", {}, Exception(message)), "scan")
    assert hint.category == category and hint.sqlstate is None


def test_broken_driver_metadata_cannot_replace_the_original_failure() -> None:
    class BadDriverError(Exception):
        @property
        def sqlstate(self) -> str:
            raise ValueError("PRIVATE DRIVER DETAIL")

    hint = safe_failure(OperationalError("PRIVATE SQL", {}, BadDriverError()), "scan")
    assert hint.sqlstate is None and "PRIVATE" not in json.dumps(asdict(hint))


def test_killed_idle_pool_connection_is_replaced_before_next_transaction(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    connected: list[object] = []
    event.listen(engine, "connect", lambda connection, _: connected.append(connection))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)"))
        conn.execute(text("INSERT INTO evidence VALUES (1, 'keep')"))
        # Keep the actual DBAPI connection, then return it to the idle pool.
        stale = conn.connection.dbapi_connection
    assert len(connected) == 1
    stale.close()  # Server/network closed it while SQLAlchemy believed it was idle/healthy.
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO evidence VALUES (2, 'new')"))
    assert len(connected) == 2
    with engine.connect() as conn:
        assert list(conn.execute(text("SELECT value FROM evidence ORDER BY id")).scalars()) == [
            "keep",
            "new",
        ]
    engine.dispose()
