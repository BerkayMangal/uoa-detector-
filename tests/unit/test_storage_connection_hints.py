"""Native causes only; SQL/parameters cannot masquerade as connection evidence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from webapp import main
from webapp.live_alpha import job
from webapp.storage_diagnostics import Reason, native_hint

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_startup_visibility import StopSupervisorError

SECRET = "postgres://PRIVATE-user:PRIVATE-password@PRIVATE-host:5432/PRIVATE-db"
AT = datetime(2026, 10, 28, 13, 10, tzinfo=UTC)


def error(message: str, code: str | None = None) -> OperationalError:
    original = Exception(message + " " + SECRET)
    original.sqlstate = code
    return OperationalError(SECRET, {"password": SECRET}, original)


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("FATAL: the database system is in recovery mode", Reason.RECOVERY),
        ("FATAL: the database system is starting up", Reason.STARTING),
        ("the database system is shutting down", Reason.SHUTDOWN),
        ("connection failed: Connection refused", Reason.REFUSED),
        ("could not translate host name PRIVATE", Reason.DNS),
        ("Name or service not known", Reason.DNS),
        ("Temporary failure in name resolution", Reason.DNS),
        ("nodename nor servname provided", Reason.DNS),
        ("connection timeout expired", Reason.TIMEOUT),
        ("Connection timed out", Reason.TIMEOUT),
        ("timeout expired", Reason.TIMEOUT),
        ("password authentication failed for user PRIVATE", Reason.AUTH),
        ("no pg_hba.conf entry", Reason.AUTH),
        ("certificate verify failed", Reason.TLS),
        ("server does not support SSL", Reason.TLS),
        ("SSL certificate verification failed", Reason.TLS),
        ("too many connections", Reason.CONNECTIONS),
        ("remaining connection slots are reserved", Reason.CONNECTIONS),
        ("too many clients already", Reason.CONNECTIONS),
    ],
)
def test_native_reason_is_fixed_and_secret_free(message: str, reason: Reason) -> None:
    hint = native_hint(error(message))
    assert hint and hint.code is reason
    encoded = json.dumps(asdict(hint), ensure_ascii=False)
    assert "PRIVATE" not in encoded and "postgres://" not in encoded


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("57P03", Reason.NOT_ACCEPTING),
        ("57P01", Reason.SHUTDOWN),
        ("28P01", Reason.AUTH),
        ("28000", Reason.AUTH),
        ("53300", Reason.CONNECTIONS),
        ("08006", Reason.LOST),
        ("08003", Reason.LOST),
    ],
)
def test_native_code_without_precise_message_does_not_guess_recovery(
    code: str, reason: Reason
) -> None:
    hint = native_hint(error("server unavailable", code))
    assert hint and hint.code is reason
    assert (
        native_hint(error("database system is in recovery mode", "57P03")).code is Reason.RECOVERY
    )


@pytest.mark.parametrize(
    "exc",
    [
        Exception("database system is in recovery mode"),
        OperationalError(
            "database system is in recovery mode",
            {"message": "connection refused"},
            Exception("unknown"),
        ),
        OperationalError(SECRET, {}, Exception("PRIVATE unknown problem")),
        OperationalError(SECRET, {}, None),
    ],
)
def test_no_specific_hint_from_wrapper_sql_parameters_or_non_db_error(exc: Exception) -> None:
    assert native_hint(exc) is None


def test_unreadable_native_detail_and_sqlstate_are_safe() -> None:
    class UnreadableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError(SECRET)

    assert native_hint(OperationalError(SECRET, {}, UnreadableError())) is None

    class BadCodeError(Exception):
        @property
        def sqlstate(self) -> str:
            raise RuntimeError(SECRET)

    assert native_hint(OperationalError(SECRET, {}, BadCodeError())) is None


def test_startup_health_has_safe_cause_and_next_restart_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job, "LAST_CYCLE", {})
    attempts = 0

    async def fail(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        assert "database_hint" not in job.LAST_CYCLE
        if attempts == 1:
            raise error("database system is in recovery mode")
        raise StopSupervisorError()

    def pause(seconds: float) -> None:
        assert seconds == 30
        assert job.LAST_CYCLE["database_hint"] == "recovery"
        assert "PRIVATE" not in json.dumps(main.health())

    monkeypatch.setattr(main, "live_alpha_loop", fail)
    monkeypatch.setattr(main.time, "sleep", pause)
    with pytest.raises(StopSupervisorError):
        main._live_alpha_thread_main(SECRET)
    assert job.LAST_CYCLE["status"] == "starting" and "database_hint" not in job.LAST_CYCLE


@pytest.fixture
def unavailable(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fail() -> Any:
        raise error("database system is in recovery mode")

    monkeypatch.setattr(main, "_board_reader", fail)
    monkeypatch.setattr(main, "_now", lambda: AT)
    return TestClient(main.app, headers=set_web_auth(monkeypatch), raise_server_exceptions=False)


def test_opening_outage_keeps_clock_503_no_advice_no_polling(unavailable: TestClient) -> None:
    r = unavailable.get("/acilis")
    assert r.status_code == 503
    assert "BEKLE" in r.text and "16:10" in r.text and "16:45" in r.text
    assert "kurtarma sürecinde" in r.text and "sonuçları bilinmiyor" in r.text
    assert "PRIVATE" not in r.text
    assert "<script" not in r.text and "data-opening-action=" not in r.text
    assert 'http-equiv="refresh"' not in r.text and "retry-after" not in r.headers
    assert (
        r.headers["cache-control"] == "private, no-store" and "noindex" in r.headers["x-robots-tag"]
    )
    assert unavailable.get("/acilis", headers={"Authorization": "Basic invalid"}).status_code == 401


@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2026, 11, 2, 14, 10, tzinfo=UTC), "17:45"),
        (datetime(2026, 11, 26, 14, 10, tzinfo=UTC), "tatil"),
    ],
)
def test_unavailable_clock_preserves_dst_and_holiday(
    unavailable: TestClient, monkeypatch: pytest.MonkeyPatch, instant: datetime, expected: str
) -> None:
    monkeypatch.setattr(main, "_now", lambda: instant)
    r = unavailable.get("/acilis")
    assert r.status_code == 503 and expected in r.text


def test_api_and_mutation_keep_original_outcome_contract(unavailable: TestClient) -> None:
    assert unavailable.get("/api/acilis/uyarilar").json() == {
        "error": "database_unavailable",
        "outcome": "unavailable",
    }
    r = unavailable.post(
        "/canli/izle", data={"opportunity_id": "x"}, headers={"origin": "http://testserver"}
    )
    assert r.status_code == 503 and "kaydedilip kaydedilmediğini doğrulayamıyoruz" in r.text
    assert "otomatik olarak tekrar gönderilmedi" in r.text
    assert "retry-after" not in r.headers and "<script" not in r.text and "PRIVATE" not in r.text


def test_opening_render_failure_keeps_503_and_does_not_expose_native_detail(
    unavailable: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(main.templates, "TemplateResponse", fail)
    r = unavailable.get("/acilis")
    assert r.status_code == 503 and "16:45" in r.text and "PRIVATE" not in r.text


def test_other_opening_errors_remain_500(
    unavailable: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> Any:
        raise ValueError(SECRET)

    monkeypatch.setattr(main, "_board_reader", fail)
    r = unavailable.get("/acilis")
    assert r.status_code == 500 and "PRIVATE" not in r.text


def test_cycle_hint_is_added_and_cleared_by_success(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webapp.live_alpha.job as job_module

    from tests.unit.test_live_alpha_job import _seed

    engine = _seed(f"sqlite:///{tmp_path / 'cycle.db'}")
    engine.dispose()
    monkeypatch.setattr(job_module, "LAST_CYCLE", {})
    calls = 0

    async def scan(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error("connection refused")
        return job_module.store.ScanRow("ok", AT, AT, "PREMARKET", "ok", None, {"cards": []})

    async def no_op(*args: Any, **kwargs: Any) -> None:
        return None

    waits = []

    async def pause(seconds: float) -> None:
        waits.append(dict(job_module.LAST_CYCLE))

    monkeypatch.setattr(job_module, "run_cycle", scan)
    for name in (
        "run_shadow",
        "run_desk",
        "run_calibration",
        "run_tournament",
        "run_opening",
        "run_readiness",
    ):
        monkeypatch.setattr(job_module, name, no_op)
    asyncio.run(
        job_module.live_alpha_loop(
            database_url=f"sqlite:///{tmp_path / 'cycle.db'}",
            client_factory=lambda: object(),
            clock=lambda: AT,
            sleep=pause,
            max_cycles=2,
        )
    )
    assert waits[0]["database_hint"] == "refused"
    assert job_module.LAST_CYCLE["status"] == "ok" and "database_hint" not in job_module.LAST_CYCLE


def test_nested_sqlalchemy_wrapper_is_not_native_driver_evidence() -> None:
    wrapper = OperationalError("database system is in recovery mode", {}, Exception("unknown"))
    assert native_hint(OperationalError(SECRET, {}, wrapper)) is None


@pytest.mark.parametrize("specific", [False, True])
def test_actual_psycopg_native_exception_without_a_database_call(specific: bool) -> None:
    from psycopg.errors import CannotConnectNow

    original = CannotConnectNow(
        "FATAL: the database system is in recovery mode" if specific else "server unavailable"
    )
    hint = native_hint(OperationalError(SECRET, {}, original))
    assert hint and hint.code is (Reason.RECOVERY if specific else Reason.NOT_ACCEPTING)
