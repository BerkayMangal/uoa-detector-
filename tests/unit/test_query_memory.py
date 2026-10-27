"""Resource codes are not proof of persistent disk exhaustion; bound query DSM."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from webapp import database_settings, gamma
from webapp.board import db
from webapp.live_alpha.cycle_status import safe_failure


@pytest.mark.parametrize(
    ("message", "code", "resource"),
    [
        (
            'could not resize shared memory segment "/PRIVATE" to 8388608 bytes: No space left on device',
            "53100",
            "shared_memory",
        ),
        ("dynamic shared memory PRIVATE", "53100", "shared_memory"),
        (
            'could not create temporary file "PRIVATE": No space left on device',
            "53100",
            "temporary_storage",
        ),
        ("pgsql_tmp/PRIVATE: No space left on device", "53100", "temporary_storage"),
        ('could not extend file "PRIVATE": No space left on device', "53100", "disk"),
        ("could not write block PRIVATE", "53100", "disk"),
        ("unknown PRIVATE capacity error", "53100", None),
        ("PRIVATE", "53300", "connections"),
        ("PRIVATE", "53200", "memory"),
        ("shared memory segment PRIVATE", "57014", None),
    ],
)
def test_resource_reason_never_exposes_server_segment_or_file_names(
    message: str, code: str, resource: str | None
) -> None:
    orig = Exception(message)
    orig.sqlstate = code
    hint = safe_failure(OperationalError("PRIVATE SQL", {"raw": "PRIVATE"}, orig), "startup")
    assert hint.resource == resource
    assert "PRIVATE" not in json.dumps(asdict(hint))


def test_sql_and_parameters_cannot_forge_the_server_resource_reason() -> None:
    orig = Exception("unknown server capacity")
    orig.sqlstate = "53100"
    hint = safe_failure(OperationalError("shared memory segment PRIVATE", {}, orig), "startup")
    assert hint.resource is None


@pytest.mark.parametrize(
    ("url", "env_options", "prefix"),
    [
        ("postgres://example.invalid/demo", "", ""),
        (
            "postgresql://example.invalid/demo",
            "-c statement_timeout=30000",
            "-c statement_timeout=30000 ",
        ),
        (
            "postgresql+psycopg://example.invalid/demo?options=-c%20search_path%3Dpublic",
            "-c discarded=unused",
            "-c search_path=public ",
        ),
        (
            "postgresql://example.invalid/demo?options=-c%20max_parallel_workers_per_gather%3D2",
            "",
            "-c max_parallel_workers_per_gather=2 ",
        ),
    ],
)
def test_postgres_session_limit_preserves_existing_options(
    monkeypatch: pytest.MonkeyPatch, url: str, env_options: str, prefix: str
) -> None:
    calls = []
    sentinel = create_engine("sqlite://")

    def build(actual: str, **kwargs: Any) -> Any:
        calls.append((actual, kwargs))
        return sentinel

    monkeypatch.setenv("PGOPTIONS", env_options)
    monkeypatch.setattr(db, "create_engine", build)
    assert db.make_engine(url) is sentinel
    actual, kwargs = calls[0]
    assert actual.startswith("postgresql+psycopg://")
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"]["options"] == prefix + "-c max_parallel_workers_per_gather=0"


def test_sqlite_never_receives_postgres_options_and_ambiguous_pg_options_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    sentinel = create_engine("sqlite://")
    monkeypatch.setattr(db, "create_engine", lambda url, **kwargs: calls.append(kwargs) or sentinel)
    db.make_engine("sqlite://")
    assert "connect_args" not in calls[0] and calls[0]["pool_pre_ping"]
    with pytest.raises(ValueError, match="ambiguous"):
        db.make_engine("postgresql://example.invalid/demo?options=a&options=b")
    assert len(calls) == 1


def test_gamma_uses_common_factory_and_preserves_preexisting_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = f"sqlite:///{tmp_path / 'gamma.db'}"
    engine = create_engine(path)
    seen = []
    monkeypatch.setattr(gamma, "make_engine", lambda url: seen.append(url) or engine)
    first = gamma.GammaRepo(path)
    first.upsert(
        "NVDA",
        "2026-10-27",
        {"spot": 100, "net_gex": 20, "flip": 99, "call_wall": 105, "put_wall": 95},
    )
    second = gamma.GammaRepo(path)
    assert len(seen) == 2 and second.latest()["NVDA"].net_gex == 20
    engine.dispose()


def test_operational_profile_is_serial_mode_not_a_strategy_tuning_surface() -> None:
    assert database_settings.settings().max_parallel_workers_per_gather == 0
    with pytest.raises(ValidationError):
        database_settings.DatabaseSettings(max_parallel_workers_per_gather=1)
