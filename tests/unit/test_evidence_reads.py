"""Observation-local SQL reuse preserves original rows, clocks and write visibility."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import String, bindparam, event, func, literal_column, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from webapp.board.db import make_engine, session_factory
from webapp.evidence_reads import _freeze, read_scope, scoped_read
from webapp.shadow_capture.storage import ShadowEvent, ShadowReceipt

NOW = datetime(2026, 11, 6, 15, tzinfo=UTC)


class IsolatedTestBase(DeclarativeBase):
    pass


class MutableEvidenceTest(IsolatedTestBase):
    __tablename__ = 'alfa_test_mutable_evidence'
    key: Mapped[str] = mapped_column(String, primary_key=True)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    engine = make_engine('sqlite:///' + str(tmp_path / 'reads.sqlite'))
    for model in (ShadowEvent, ShadowReceipt, MutableEvidenceTest):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([ShadowEvent(key=key, kind=key, at=NOW, payload_json=key,
                                     checksum=key) for key in ('one', 'two')])
        session.add(MutableEvidenceTest(key='mutable'))
        session.commit()
    yield engine
    engine.dispose()


def _get(engine: Engine, key: str) -> ShadowEvent | None:
    with session_factory(engine)() as session:
        return session.get(ShadowEvent, key)


def _all(engine: Engine, statement: Any = None) -> list[Any]:
    with session_factory(engine)() as session:
        return list(session.scalars(statement if statement is not None else select(ShadowEvent)))


def _queries(engine: Engine) -> list[str]:
    sql: list[str] = []
    event.listen(engine, 'before_cursor_execute', lambda _c, _cu, statement, *_a: sql.append(statement))
    return sql


def test_no_scope_unchanged_and_scope_reuses_exact_read(engine: Engine) -> None:
    queries = _queries(engine)
    assert _get(engine, 'one') is not None
    assert _get(engine, 'one') is not None
    assert len(queries) == 2
    with read_scope(engine) as scope:
        a, b = _get(engine, 'one'), _get(engine, 'one')
        assert a is not None and b is not None and a.key == b.key == 'one'
        assert a is not b
        assert scope.hits == 1 and scope.misses == 1
        assert len(scope._cache) == 1
    assert scope.disabled and not scope._cache and scope.retained_rows == 0
    assert len(queries) == 3
    assert _get(engine, 'one') is not None
    assert len(queries) == 4


def test_primary_key_and_bound_query_values_are_exact(engine: Engine) -> None:
    with read_scope(engine) as scope:
        assert _get(engine, 'missing') is None
        one, two = _get(engine, 'one'), _get(engine, 'two')
        assert one is not None and one.key == 'one'
        assert two is not None and two.key == 'two'
        assert [r.key for r in _all(engine, select(ShadowEvent).where(ShadowEvent.kind == 'one'))] == ['one']
        assert [r.key for r in _all(engine, select(ShadowEvent).where(ShadowEvent.kind == 'two'))] == ['two']
        assert [r.key for r in _all(engine, select(ShadowEvent).where(ShadowEvent.key.in_(['two'])))] == ['two']
        assert {r.key for r in _all(engine, select(ShadowEvent).where(ShadowEvent.key.in_(['one', 'two'])))} == {'one', 'two'}
        assert scope.hits == 0


def test_iso_clock_keys_preserve_sqlite_offset_semantics(engine: Engine) -> None:
    same_instant = NOW.astimezone(timezone(timedelta(hours=1)))
    assert same_instant == NOW
    with read_scope(engine) as scope:
        assert len(_all(engine, select(ShadowEvent).where(ShadowEvent.at == NOW))) == 2
        assert _all(engine, select(ShadowEvent).where(ShadowEvent.at == same_instant)) == []
        assert scope.hits == 0


def test_cached_snapshot_cannot_be_mutated_by_detached_caller(engine: Engine) -> None:
    with read_scope(engine):
        first = _get(engine, 'one')
        assert first is not None
        first.payload_json = 'tamper'
        second = _get(engine, 'one')
        assert second is not None and second.payload_json == 'one'


@pytest.mark.parametrize('budget', [{'max_keys': 0}, {'max_rows': 0}, {'max_bytes': 0},
                                  {'max_rows': 1}, {'max_bytes': 1}])
def test_budgets_preserve_complete_results_and_do_not_retain_overflow(engine: Engine, budget: dict[str, int]) -> None:
    with read_scope(engine, **budget) as scope:
        assert {r.key for r in _all(engine)} == {'one', 'two'}
        assert {r.key for r in _all(engine)} == {'one', 'two'}
        assert not scope._cache and scope.hits == 0


def test_key_and_cumulative_row_budget_fall_back_without_eviction(engine: Engine) -> None:
    with read_scope(engine, max_keys=1, max_rows=1) as scope:
        assert _get(engine, 'one') is not None
        assert _get(engine, 'two') is not None
        assert _get(engine, 'two') is not None
        assert _get(engine, 'one') is not None
        assert len(scope._cache) == 1 and scope.retained_rows == 1 and scope.hits == 1


def test_scalar_columns_and_join_rows_keep_metadata(engine: Engine) -> None:
    with read_scope(engine) as scope:
        statement = select(ShadowEvent.key, ShadowEvent.payload_json).order_by(ShadowEvent.key)
        with Session(engine) as session:
            first = session.execute(statement).all()
        with Session(engine) as session:
            second = session.execute(statement).all()
        assert first == second == [('one', 'one'), ('two', 'two')]
        assert scope.hits == 1


def test_missing_receipt_appears_in_next_observation(engine: Engine) -> None:
    def receipt() -> ShadowReceipt | None:
        with Session(engine) as session:
            return session.get(ShadowReceipt, 'receipt')
    with read_scope(engine):
        assert receipt() is None and receipt() is None
    with Session(engine) as session:
        session.add(ShadowReceipt(key='receipt', parent_key='one', stage='recorded',
                                  event_checksum='one', at=NOW, checksum='receipt'))
        session.commit()
    with read_scope(engine):
        assert receipt() is not None


@pytest.mark.parametrize('write', ['core', 'raw', 'orm', 'commit'])
def test_writes_and_explicit_commits_disable_reuse(engine: Engine, write: str) -> None:
    with read_scope(engine) as scope:
        assert _get(engine, 'one') is not None
        if write == 'core':
            with engine.begin() as connection:
                connection.execute(ShadowEvent.__table__.update().where(ShadowEvent.key == 'one').values(payload_json='changed'))
        elif write == 'raw':
            with engine.begin() as connection:
                connection.exec_driver_sql("UPDATE alfa_shadow_event SET payload_json='changed' WHERE key='one'")
        elif write == 'orm':
            with Session(engine) as session:
                row = session.get(ShadowEvent, 'one')
                assert row is not None
                row.payload_json = 'changed'
                session.commit()
        else:
            with Session(engine) as session:
                session.commit()
        assert scope.disabled and not scope._cache
        row = _get(engine, 'one')
        assert row is not None and row.payload_json == ('one' if write == 'commit' else 'changed')


def test_dirty_session_invalidates_before_cached_hit(engine: Engine) -> None:
    with read_scope(engine) as scope, Session(engine, autoflush=False) as session:
        _get(engine, 'one')
        row = session.get(ShadowEvent, 'two')
        assert row is not None
        row.payload_json = 'changed'
        session.execute(select(ShadowEvent)).all()
        assert scope.disabled


def test_nested_write_invalidates_restored_outer_even_different_engine(engine: Engine, tmp_path: Path) -> None:
    other = make_engine('sqlite:///' + str(tmp_path / 'other.sqlite'))
    try:
        with read_scope(engine) as outer:
            _get(engine, 'one')
            with read_scope(other), engine.begin() as connection:
                connection.execute(ShadowEvent.__table__.update().values(payload_json='changed'))
            assert outer.disabled and not outer._cache
            row = _get(engine, 'one')
            assert row is not None and row.payload_json == 'changed'
    finally:
        other.dispose()


def test_independent_nested_observations_and_exception_cleanup(engine: Engine) -> None:
    with read_scope(engine) as outer:
        _get(engine, 'one')
        with pytest.raises(RuntimeError), read_scope(engine) as inner:
            _get(engine, 'one')
            assert inner.hits == 0
            raise RuntimeError('stop')
        assert inner.disabled and not inner._cache
        _get(engine, 'one')
        assert outer.hits == 1


def test_mutable_unlisted_models_options_locks_and_callable_params_bypass(engine: Engine) -> None:
    with read_scope(engine) as scope:
        for _ in range(2):
            _all(engine, select(MutableEvidenceTest))
            _all(engine, select(ShadowEvent).with_for_update())
            _all(engine, select(ShadowEvent).execution_options(yield_per=1))
            _all(engine, select(ShadowEvent).where(ShadowEvent.key == bindparam('key', callable_=lambda: 'one')))
        assert scope.hits == 0 and not scope._cache


def test_engine_isolation(engine: Engine, tmp_path: Path) -> None:
    other = make_engine('sqlite:///' + str(tmp_path / 'other.sqlite'))
    ShadowEvent.__table__.create(other)
    try:
        with read_scope(engine) as scope:
            assert _get(engine, 'one') is not None
            assert _get(other, 'one') is None
            assert _get(other, 'one') is None
            assert scope.hits == 0 and len(scope._cache) == 1
    finally:
        other.dispose()


def test_thread_context_isolation(engine: Engine) -> None:
    with read_scope(engine) as outer, ThreadPoolExecutor(max_workers=1) as executor:
        _get(engine, 'one')
        def worker() -> tuple[int, int]:
            with read_scope(engine) as inner:
                _get(engine, 'one')
                _get(engine, 'one')
                return inner.misses, inner.hits
        assert executor.submit(worker).result() == (1, 1)
        assert outer.hits == 0


@pytest.mark.asyncio
async def test_inherited_child_task_does_not_reuse_parent_observation(engine: Engine) -> None:
    with read_scope(engine) as parent:
        _get(engine, 'one')
        async def child() -> None:
            _get(engine, 'one')
            _get(engine, 'one')
        await asyncio.create_task(child())
        assert parent.hits == 0 and parent.misses == 1
        _get(engine, 'one')
        assert parent.hits == 1


def test_decorator_supports_keyword_engine_and_new_clock(engine: Engine) -> None:
    @scoped_read
    def build(engine: Engine, now: datetime) -> tuple[str, ...]:
        return tuple(row.key for row in _all(engine, select(ShadowEvent).where(ShadowEvent.at <= now)))
    assert build(engine=engine, now=NOW - timedelta(seconds=1)) == ()
    assert set(build(engine, NOW)) == {'one', 'two'}


@pytest.mark.parametrize('value', [float('nan'), float('inf'), Decimal('NaN'), object(), {1, 2}])
def test_ambiguous_parameters_are_not_keyed(value: object) -> None:
    with pytest.raises(ValueError):
        _freeze(value)


def test_typed_parameter_identity_and_negative_budget(engine: Engine) -> None:
    assert _freeze(True) != _freeze(1)
    assert _freeze(NOW) != _freeze(NOW.date())
    assert _freeze([1, 2]) != _freeze([2, 1])
    assert _freeze({'a': 1, 'b': 2}) != _freeze({'b': 2, 'a': 1})
    with pytest.raises(ValueError), read_scope(engine, max_keys=-1):
        pass


def test_four_candidate_full_report_and_source_identity_parity(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import asdict
    from types import SimpleNamespace

    from webapp.board.quota_ledger import ensure_quota_tables
    from webapp.live_alpha import store as live
    from webapp.news_research import storage as physical
    from webapp.shadow_capture import collector, config, storage, view

    from tests.unit.test_fusion_live_view import admit
    from tests.unit.test_news_adapter import NOW as SOURCE_NOW
    from tests.unit.test_shadow_collector import Client, Clock

    monkeypatch.setenv('SHADOW_CAPTURE_ENABLED', 'true')
    # Remove only this independent SQL fixture's generic rows before admitting
    # actual checksummed synthetic source captures through the original APIs.
    with engine.begin() as connection:
        connection.execute(ShadowEvent.__table__.delete())
    live.ensure_live_tables(engine)
    physical.ensure(engine)
    ensure_quota_tables(engine)
    storage.activate(engine, lambda: SOURCE_NOW - timedelta(hours=1))
    admit(SimpleNamespace(engine=engine))
    clock = Clock(SOURCE_NOW + timedelta(seconds=64))
    client = Client(clock)
    client.prices = {'NVDA': 100., 'AMD': 100., 'MSFT': 100., 'AAPL': 100., 'SPY': 500.}
    try:
        for seconds in (64, 966, 3666):
            clock.at = SOURCE_NOW + timedelta(seconds=seconds)
            asyncio.run(collector.tick(engine, client, clock(), clock=clock))
        build = view.build
        identity = config.identity()
        sql = _queries(engine)
        original = build(engine, clock())
        before = len(sql)
        sql.clear()
        with read_scope(engine) as scope:
            measured = build(engine, clock())
            assert scope.hits > 300
        assert asdict(original) == asdict(measured)
        assert measured.candidate_count == 4 and all(c.observed == 4 for c in measured.coverage)
        assert len(sql) < before / 2 and len(client.calls) == 24
        assert config.identity() == identity
        assert all(statement.lstrip().upper().startswith('SELECT') for statement in sql)
    finally:
        collector._HALTS.clear()
        collector._COOLDOWNS.clear()


def test_functions_and_hidden_sql_state_are_never_cached(engine: Engine) -> None:
    with read_scope(engine) as scope:
        for _ in range(2):
            _all(engine, select(ShadowEvent).where(func.length(ShadowEvent.key) == 3))
            _all(engine, select(ShadowEvent).where(text("key = 'one'")))
            _all(engine, select(ShadowEvent).where(literal_column("key = 'one'")))
        assert scope.hits == 0 and not scope._cache
