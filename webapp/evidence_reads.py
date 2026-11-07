"""Bounded immutable evidence reads for one explicit, synchronous observation.

SQLAlchemy's documented frozen-result merge boundary retains normal verifiers.
``Any`` is confined to its dynamic execution/result API. No query or table is
changed, no result survives its scope, and writers disable reuse immediately.
"""
from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from itertools import chain
from typing import Any, ParamSpec, TypeVar, cast

from sqlalchemy import Table, event
from sqlalchemy.engine import Connection, Engine, FrozenResult, IteratorResult, Row
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.orm import ORMExecuteState, Session, loading
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ColumnClause, TextClause
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import TableClause

_TABLES = frozenset({
    'alfa_shadow_event', 'alfa_shadow_receipt', 'alfa_live_scan',
    'alfa_news_research_packet', 'alfa_news_research_receipt',
    'alfa_semantic_event', 'alfa_semantic_receipt',
    'alfa_fundamental_event', 'alfa_fundamental_receipt',
    'alfa_fusion_event', 'alfa_fusion_receipt',
})
_MAX_KEYS = 512
_MAX_ROWS = 8192
_MAX_BYTES = 64 * 1024 * 1024
P = ParamSpec('P')
R = TypeVar('R')


def _task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass
class ReadScope:
    """Ephemeral operational state, never a persisted financial/domain record.

    Mutable only for bounded cache accounting; all retained rows clear on exit.
    """
    engine: Engine
    max_keys: int = _MAX_KEYS
    max_rows: int = _MAX_ROWS
    max_bytes: int = _MAX_BYTES
    hits: int = 0
    misses: int = 0
    retained_rows: int = 0
    retained_bytes: int = 0
    disabled: bool = False
    _owner_thread: int = field(default_factory=threading.get_ident)
    _owner_task: object | None = field(default_factory=_task)
    _cache: dict[tuple[object, ...], FrozenResult[Any]] = field(default_factory=dict)
    _parent: ReadScope | None = None

    def disable(self) -> None:
        self.disabled = True
        self.clear()

    def clear(self) -> None:
        self._cache.clear()
        self.retained_rows = 0
        self.retained_bytes = 0


_CURRENT: ContextVar[ReadScope | None] = ContextVar('evidence_read_scope', default=None)


def _current(engine: object) -> ReadScope | None:
    scope = _CURRENT.get()
    if (scope is None or scope.disabled or scope.engine is not engine
            or scope._owner_thread != threading.get_ident() or scope._owner_task is not _task()):
        return None
    return scope


@contextmanager
def read_scope(engine: Engine, *, max_keys: int = _MAX_KEYS, max_rows: int = _MAX_ROWS,
               max_bytes: int = _MAX_BYTES) -> Iterator[ReadScope]:
    """Start a fresh fixed-clock observation; never span I/O, writes or polls.

    Nested scopes are independent so a nested build with a different decision
    time cannot inherit observations. Child tasks/threads also cannot reuse it.
    """
    if min(max_keys, max_rows, max_bytes) < 0:
        raise ValueError('negative_evidence_cache_budget')
    scope = ReadScope(engine, max_keys, max_rows, max_bytes, _parent=_CURRENT.get())
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        scope.disable()
        _CURRENT.reset(token)


def scoped_read(function: Callable[P, R]) -> Callable[P, R]:
    """Decorate a pure synchronous read model whose first argument is Engine."""
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        engine = args[0] if args else kwargs.get('engine')
        if not isinstance(engine, Engine):
            raise TypeError('evidence_read_engine_required')
        with read_scope(engine):
            return function(*args, **kwargs)
    return wrapped


def _freeze(value: object) -> object:
    """Exact, typed parameter keys; unsupported values are never cached."""
    if value is None or type(value) in (bool, int, str, bytes):
        return type(value), value
    if type(value) is float and math.isfinite(value):
        return float, value.hex()
    if isinstance(value, Decimal) and value.is_finite():
        return Decimal, value.as_tuple()
    if isinstance(value, datetime):
        return datetime, value.isoformat(), value.fold
    if type(value) is date:
        return date, value.isoformat()
    if isinstance(value, (tuple, list)):
        return type(value), tuple(_freeze(item) for item in value)
    if isinstance(value, Mapping):
        return type(value), tuple((_freeze(key), _freeze(item)) for key, item in value.items())
    raise ValueError('uncacheable_evidence_parameter')


def _size(value: object) -> int:
    if value is None:
        return 8
    if isinstance(value, str):
        return 64 + len(value.encode('utf-8'))
    if isinstance(value, bytes):
        return 64 + len(value)
    if isinstance(value, (tuple, list, Row)):
        return 64 + sum(_size(item) for item in value)
    state = getattr(value, '_sa_instance_state', None)
    if state is not None:
        return 256 + sum(_size(item) for key, item in state.dict.items()
                         if key != '_sa_instance_state')
    return 64


def _key(state: ORMExecuteState) -> tuple[object, ...] | None:
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return None
    statement = cast(Any, state.statement)
    if statement._for_update_arg is not None:
        return None
    nodes = list(visitors.iterate(statement))
    # Hidden text/subqueries and SQL functions can observe mutable state or
    # server clocks even when their visible entity is append-only.
    if any(isinstance(node, (TextClause, FunctionElement))
           or (isinstance(node, ColumnClause) and node.is_literal)
           or (isinstance(node, TableClause) and not isinstance(node, Table))
           or (isinstance(node, Table) and node.schema is not None)
           for node in nodes):
        return None
    tables = {node.name for node in nodes if isinstance(node, Table)}
    if not tables or not tables <= _TABLES:
        return None
    generated = statement._generate_cache_key()
    if generated is None or any(parameter.callable is not None for parameter in generated.bindparams):
        return None
    # ORM internal options describe the caller's Session and identity-map load,
    # not database values. Bypass any public execution modifiers instead of
    # conflating semantically distinct reads (streaming, identity tokens, etc).
    allowed = {'_sa_orm_load_options', '_result_disable_adapt_to_context'}
    if not set(state.execution_options) <= allowed:
        return None
    options = state.execution_options.get('_sa_orm_load_options')
    if options is not None and any(getattr(options, name, None) for name in
            ('_identity_token', '_yield_per', '_refresh_state', '_populate_existing')):
        return None
    try:
        return (generated.key, tuple(_freeze(p.value) for p in generated.bindparams),
                _freeze(state.parameters),
                _freeze(state.execution_options.get('_result_disable_adapt_to_context')))
    except (ValueError, TypeError):
        return None


@event.listens_for(Session, 'do_orm_execute', retval=True)
def _read(state: ORMExecuteState) -> Any:
    scope = _current(state.session.get_bind(**state.bind_arguments))
    if scope is None:
        return None
    if state.session.new or state.session.dirty or state.session.deleted or not state.is_select:
        _invalidate(scope.engine)
        return None
    key = _key(state)
    if key is None:
        return None
    frozen = scope._cache.get(key)
    if frozen is not None:
        scope.hits += 1
        return cast(Any, loading.merge_frozen_result)(state.session, state.statement, frozen, load=False)()
    scope.misses += 1
    result = state.invoke_statement()
    if len(scope._cache) >= scope.max_keys:
        return result
    # Consume only a bounded prefix before deciding whether freezing is safe.
    # Oversized results keep their original full stream and are never retained.
    raw = cast(Any, result)._raw_row_iterator()
    prefix: list[Any] = []
    size = 0
    for row in raw:
        prefix.append(row)
        size += _size(row)
        if (scope.retained_rows + len(prefix) > scope.max_rows
                or scope.retained_bytes + size > scope.max_bytes):
            resumed: IteratorResult[Any] = IteratorResult(result._metadata, chain(prefix, raw), raw=result,
                                     _source_supports_scalars=result._source_supports_scalars)
            resumed._attributes = result._attributes
            return resumed
    bounded: IteratorResult[Any] = IteratorResult(result._metadata, iter(prefix), raw=result,
                             _source_supports_scalars=result._source_supports_scalars)
    bounded._attributes = result._attributes
    frozen = bounded.freeze()
    rows = len(prefix)
    if (len(scope._cache) < scope.max_keys and scope.retained_rows + rows <= scope.max_rows
            and scope.retained_bytes + size <= scope.max_bytes):
        # Keep a detached copy: no ORM object handed to a caller can mutate the
        # cached evidence when its original Session has already closed.
        with Session() as snapshot:
            scope._cache[key] = cast(Any, loading.merge_frozen_result)(
                snapshot, state.statement, frozen, load=False)
        scope.retained_rows += rows
        scope.retained_bytes += size
    return frozen()


@event.listens_for(Session, 'before_flush')
def _flush(session: Session, _context: object, _instances: object) -> None:
    _session_boundary(session)


def _invalidate(engine: Engine | Connection) -> None:
    engine = engine.engine if isinstance(engine, Connection) else engine
    scope = _CURRENT.get()
    while scope is not None:
        if (scope.engine is engine and scope._owner_thread == threading.get_ident()
                and scope._owner_task is _task()):
            scope.disable()
        scope = scope._parent


@event.listens_for(Engine, 'before_cursor_execute')
def _write(connection: Any, _cursor: Any, statement: str, _parameters: Any,
           _context: Any, _many: bool) -> None:
    sql = statement.lstrip().upper()
    read = sql.startswith(('SELECT', 'SHOW')) or (
        sql.startswith(('PRAGMA MAIN.TABLE_', 'PRAGMA TEMP.TABLE_', 'PRAGMA MAIN.INDEX_',
                        'PRAGMA TEMP.INDEX_', 'PRAGMA MAIN.FOREIGN_KEY_LIST',
                        'PRAGMA TEMP.FOREIGN_KEY_LIST')) and '=' not in sql)
    if not read:
        _invalidate(connection.engine)


@event.listens_for(Engine, 'commit')
def _commit(connection: Any) -> None:
    _invalidate(connection.engine)


@event.listens_for(Session, 'before_commit')
def _session_commit(session: Session) -> None:
    _session_boundary(session)


def _session_boundary(session: Session) -> None:
    scope = _CURRENT.get()
    if scope is None:
        return
    try:
        _invalidate(session.get_bind())
    except UnboundExecutionError:
        # Multibind or unbound Session: a boundary is conservative, never a
        # reason to alter ordinary Session behavior outside our observation.
        while scope is not None:
            if scope._owner_thread == threading.get_ident() and scope._owner_task is _task():
                scope.disable()
            scope = scope._parent
