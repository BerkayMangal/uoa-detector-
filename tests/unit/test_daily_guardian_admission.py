"""Production mutex semantics without a database, provider, or shared ORM model."""

from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.engine import Engine
from webapp.daily_guardian import service


class Result:
    def __init__(self, value: object):
        self.value = value

    def scalar_one(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class Connection:
    def __init__(self, acquire: object = True, release: object = True, options_error=None):
        self.acquire = acquire
        self.release = release
        self.options_error = options_error
        self.options = []
        self.calls = []
        self.events = []
        self.closed = False
        self.invalidated = False

    def execution_options(self, **values):
        self.options.append(values)
        if self.options_error is not None:
            raise self.options_error
        return self

    def execute(self, statement, parameters):
        sql = str(statement)
        self.calls.append((sql, parameters))
        self.events.append("acquire" if "pg_try_advisory_lock" in sql else "release")
        return Result(self.acquire if "pg_try_advisory_lock" in sql else self.release)

    def invalidate(self):
        self.events.append("invalidate")
        self.invalidated = True

    def close(self):
        self.events.append("close")
        self.closed = True


def engine(connection):
    return cast(
        Engine,
        SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=lambda: connection,
        ),
    )


def test_production_lock_uses_autocommit_and_keeps_session_until_operation_finishes():
    connection = Connection()
    with service._admission(engine(connection)) as admitted:
        assert admitted is True
        assert connection.options == [{"isolation_level": "AUTOCOMMIT"}]
        assert connection.events == ["acquire"]
        assert not connection.closed
    assert connection.events == ["acquire", "release", "close"]
    assert not connection.invalidated


def test_distinct_engine_pools_use_the_same_signed_postgres_key():
    first, second = Connection(), Connection()
    with (
        service._admission(engine(first)) as first_admitted,
        service._admission(engine(second)) as second_admitted,
    ):
        assert first_admitted and second_admitted
    keys = [
        parameters["key"] for connection in (first, second) for _sql, parameters in connection.calls
    ]
    assert len(set(keys)) == 1
    assert all(type(key) is int and -(2**63) <= key < 2**63 for key in keys)


@pytest.mark.parametrize("value", [False, None, 0, 1])
def test_unknown_or_refused_lock_never_admits_or_unlocks(value):
    connection = Connection(acquire=value)
    with service._admission(engine(connection)) as admitted:
        assert admitted is False
    assert connection.events == ["acquire", "close"]


def test_acquisition_outcome_exception_invalidates_physical_connection():
    connection = Connection(acquire=RuntimeError("untrusted native connection detail"))
    with service._admission(engine(connection)) as admitted:
        assert admitted is False
    assert connection.events == ["acquire", "invalidate", "close"]


def test_isolation_setup_exception_does_not_leak_the_borrowed_pool_connection():
    connection = Connection(options_error=RuntimeError("isolation setup failed"))
    with service._admission(engine(connection)) as admitted:
        assert admitted is False
    assert connection.invalidated and connection.closed
    assert connection.calls == []


@pytest.mark.parametrize("release", [False, None, RuntimeError("unknown unlock outcome")])
def test_unknown_unlock_cannot_return_a_possibly_locked_connection_to_pool(release):
    connection = Connection(release=release)
    with service._admission(engine(connection)) as admitted:
        assert admitted is True
    assert connection.events == ["acquire", "release", "invalidate", "close"]


def test_operation_error_propagates_only_after_releasing_lock_and_connection():
    connection = Connection()
    with (
        pytest.raises(ValueError, match="operation failed"),
        service._admission(engine(connection)) as admitted,
    ):
        assert admitted is True
        raise ValueError("operation failed")
    assert connection.events == ["acquire", "release", "close"]
    assert not connection.invalidated


def test_connect_failure_does_not_admit_work():
    def unavailable():
        raise RuntimeError("database unavailable")

    broken = cast(
        Engine,
        SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=unavailable,
        ),
    )
    with service._admission(broken) as admitted:
        assert admitted is False
