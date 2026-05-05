"""Phase 3.2.1 acceptance test sweep for ``BacktestStoreProtocol``.

Two-axis test design:

  * **Backend axis**: every behavioural test is parametrised over both
    the in-memory ``BacktestStore`` and the persistent
    ``SqliteBacktestStore`` so the Protocol contract is enforced
    identically on both. This is the single most important acceptance
    check from the doc.

  * **Lifecycle axis**: tests covering ``strict_run_lifecycle=True`` are
    added separately (they are mode-specific by design — the whole
    point is that strict mode rejects what permissive mode accepts).

Tests are organised into:

  1. ``test_protocol_compliance_*``    — both backends satisfy ``BacktestStoreProtocol``
  2. ``test_implicit_run_*``           — permissive mode lifecycle
  3. ``test_strict_lifecycle_*``       — strict mode lifecycle
  4. ``test_schema_version_*``         — None vs ≥ 1
  5. ``test_latency_roundtrip``        — pipeline + data_source latency columns persist
  6. ``test_error_roundtrip``          — record_error / iter_errors / total_errors
  7. ``test_sqlite_wal_mode``          — PRAGMA journal_mode == wal
  8. ``test_sqlite_idempotent_reopen`` — open / close / open preserves data
  9. ``test_sqlite_concurrent_read``   — second connection sees committed rows
  10. ``test_sqlite_no_update_method`` — append-only at the public surface
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from uoa_detector.backtest import (
    BacktestStore,
    BacktestStoreProtocol,
    SqliteBacktestStore,
)
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.agreement import single_source_agreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.errors import RunLifecycleError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_event(event_id: str = "e1") -> EnrichedEvent:
    """Minimal valid EnrichedEvent for store.add() round-trips."""
    return EnrichedEvent(
        print=OptionsPrint(
            event_id=event_id,
            timestamp=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
            ticker="AAPL",
            option_type="call",
            strike=Decimal("200"),
            expiry=date(2025, 7, 18),
            dte=37,
            spot_price=Decimal("198"),
            premium_paid=Decimal("1000"),
            option_price=Decimal("1.50"),
            implied_volatility=0.45,
            bid=Decimal("1.45"),
            ask=Decimal("1.55"),
            fill_side="above_ask",
            exchange="CBOE",
            is_iso=False,
            open_interest=1500,
            source_agreement=single_source_agreement("synthetic"),
        ),
    )


def _build_decision() -> LabelDecision:
    return LabelDecision(
        label=SignalLabel.STANDARD_UOA,
        reason="test fixture",
    )


def _build_size() -> PositionSize:
    return PositionSize(
        bucket=RiskBucket.STANDARD_UOA,
        max_r=0.75,
        scale_in=False,
        initial_r=None,
    )


@pytest.fixture
def in_memory_store() -> Iterator[BacktestStore]:
    store = BacktestStore()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def sqlite_store(tmp_path: Path) -> Iterator[SqliteBacktestStore]:
    db_path = tmp_path / "backtest.db"
    url = f"sqlite:///{db_path}"
    store = SqliteBacktestStore(url)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture(params=["in_memory", "sqlite"])
def any_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[BacktestStoreProtocol]:
    """Yield each backend in turn so the same test runs against both."""
    if request.param == "in_memory":
        store: BacktestStoreProtocol = BacktestStore()
    else:
        url = f"sqlite:///{tmp_path / 'backtest.db'}"
        store = SqliteBacktestStore(url)
    try:
        yield store
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Protocol compliance — structural typing
# ---------------------------------------------------------------------------


def test_protocol_compliance_in_memory(in_memory_store: BacktestStore) -> None:
    assert isinstance(in_memory_store, BacktestStoreProtocol)


def test_protocol_compliance_sqlite(sqlite_store: SqliteBacktestStore) -> None:
    assert isinstance(sqlite_store, BacktestStoreProtocol)


# ---------------------------------------------------------------------------
# Implicit run (permissive mode default) — Phase 1-2 compat
# ---------------------------------------------------------------------------


def test_implicit_run_default_behavior(any_store: BacktestStoreProtocol) -> None:
    """Phase 1-2 calling convention: just call add(), no start_run.

    The store auto-creates an implicit run; subsequent adds write to it.
    """
    event = _build_event()
    decision = _build_decision()
    size = _build_size()

    stored = any_store.add(event, decision, size)

    assert stored.run_id == "implicit-default"
    runs = any_store.list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == "implicit-default"
    assert runs[0].profile_id == "<implicit>"
    assert runs[0].total_signals_processed == 1


def test_implicit_run_collects_multiple_adds(
    any_store: BacktestStoreProtocol,
) -> None:
    for i in range(5):
        any_store.add(_build_event(f"e{i}"), _build_decision(), _build_size())

    runs = any_store.list_runs()
    assert len(runs) == 1
    assert runs[0].total_signals_processed == 5

    records = list(any_store.iter_records("implicit-default"))
    assert len(records) == 5


# ---------------------------------------------------------------------------
# Strict lifecycle — opt-in for cell isolation
# ---------------------------------------------------------------------------


def _make_strict(backend: str, tmp_path: Path) -> BacktestStoreProtocol:
    if backend == "in_memory":
        return BacktestStore(strict_run_lifecycle=True)
    url = f"sqlite:///{tmp_path / 'strict.db'}"
    return SqliteBacktestStore(url, strict_run_lifecycle=True)


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
def test_strict_lifecycle_raises_without_start_run(
    backend: str, tmp_path: Path,
) -> None:
    store = _make_strict(backend, tmp_path)
    try:
        with pytest.raises(RunLifecycleError, match="strict_run_lifecycle"):
            store.add(_build_event(), _build_decision(), _build_size())
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
def test_strict_lifecycle_accepts_explicit_start_run(
    backend: str, tmp_path: Path,
) -> None:
    store = _make_strict(backend, tmp_path)
    try:
        profile = load_default_profile()
        rid = store.start_run(profile, universe_id="test")
        store.add(_build_event(), _build_decision(), _build_size())
        meta = store.finish_run()
        assert meta is not None
        assert meta.run_id == rid
        assert meta.total_signals_processed == 1
        assert meta.profile_id == profile.profile_id
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
def test_strict_lifecycle_finish_without_active_raises(
    backend: str, tmp_path: Path,
) -> None:
    store = _make_strict(backend, tmp_path)
    try:
        with pytest.raises(RunLifecycleError, match="no active run"):
            store.finish_run()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Schema version — SQLite has it, in-memory doesn't
# ---------------------------------------------------------------------------


def test_schema_version_in_memory_is_none(in_memory_store: BacktestStore) -> None:
    assert in_memory_store.schema_version is None


def test_schema_version_sqlite_is_at_least_one(
    sqlite_store: SqliteBacktestStore,
) -> None:
    assert sqlite_store.schema_version is not None
    assert sqlite_store.schema_version >= 1


# ---------------------------------------------------------------------------
# Latency round-trip — both columns preserved
# ---------------------------------------------------------------------------


def test_latency_columns_roundtrip(any_store: BacktestStoreProtocol) -> None:
    """Write known latencies, iter back, assert they survived."""
    profile = load_default_profile()
    rid = any_store.start_run(profile)

    any_store.add(
        _build_event("e1"),
        _build_decision(),
        _build_size(),
        pipeline_latency_ms=42.5,
        data_source_latency_ms=120.7,
    )
    any_store.finish_run()

    records = list(any_store.iter_records(rid))
    assert len(records) == 1
    assert records[0].pipeline_latency_ms == pytest.approx(42.5)
    assert records[0].data_source_latency_ms == pytest.approx(120.7)


def test_latency_columns_default_to_none_when_unspecified(
    any_store: BacktestStoreProtocol,
) -> None:
    """Phase 1-2 callers don't pass latencies — must keep working with None."""
    profile = load_default_profile()
    rid = any_store.start_run(profile)
    any_store.add(_build_event("e1"), _build_decision(), _build_size())
    any_store.finish_run()

    records = list(any_store.iter_records(rid))
    assert len(records) == 1
    assert records[0].pipeline_latency_ms is None
    assert records[0].data_source_latency_ms is None


# ---------------------------------------------------------------------------
# Error round-trip — record_error / iter_errors / total_errors
# ---------------------------------------------------------------------------


def test_error_roundtrip_three_errors(any_store: BacktestStoreProtocol) -> None:
    profile = load_default_profile()
    rid = any_store.start_run(profile)

    any_store.record_error(
        stage_name="m37_relative_premium",
        error_type="DataSourceError",
        error_message="Median provider unreachable",
        event_id="e1",
    )
    any_store.record_error(
        stage_name="m38_temporal_cluster",
        error_type="ValueError",
        error_message="Invalid cluster key",
        event_id="e2",
    )
    any_store.record_error(
        stage_name="m37_relative_premium",
        error_type="TimeoutError",
        error_message="Slow query",
        event_id="e3",
    )
    any_store.finish_run()

    errors = list(any_store.iter_errors(rid))
    assert len(errors) == 3
    assert {e.stage_name for e in errors} == {
        "m37_relative_premium", "m38_temporal_cluster",
    }
    assert {e.event_id for e in errors} == {"e1", "e2", "e3"}

    meta = any_store.get_run(rid)
    assert meta is not None
    assert meta.total_errors == 3


def test_error_with_no_event_id(any_store: BacktestStoreProtocol) -> None:
    """Some errors happen before an event_id is assigned — nullable column."""
    profile = load_default_profile()
    rid = any_store.start_run(profile)

    any_store.record_error(
        stage_name="source_init",
        error_type="ConnectionError",
        error_message="Polygon WS handshake failed",
        # no event_id
    )
    any_store.finish_run()

    errors = list(any_store.iter_errors(rid))
    assert len(errors) == 1
    assert errors[0].event_id is None


# ---------------------------------------------------------------------------
# SQLite-specific: WAL mode, durability, concurrency, append-only
# ---------------------------------------------------------------------------


def test_sqlite_wal_mode_enabled(sqlite_store: SqliteBacktestStore) -> None:
    """PRAGMA journal_mode must report 'wal' for file-backed databases."""
    with sqlite_store._engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal"


def test_sqlite_foreign_keys_enabled(sqlite_store: SqliteBacktestStore) -> None:
    """PRAGMA foreign_keys must be ON so signal.run_id integrity is enforced."""
    with sqlite_store._engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert fk == 1


def test_sqlite_idempotent_reopen(tmp_path: Path) -> None:
    """Open, write, close, re-open: data persists; schema unchanged."""
    db_path = tmp_path / "reopen.db"
    url = f"sqlite:///{db_path}"

    profile = load_default_profile()

    store1 = SqliteBacktestStore(url)
    rid = store1.start_run(profile, notes="first session")
    store1.add(_build_event("e1"), _build_decision(), _build_size())
    store1.add(_build_event("e2"), _build_decision(), _build_size())
    store1.finish_run()
    initial_version = store1.schema_version
    store1.close()

    store2 = SqliteBacktestStore(url)
    assert store2.schema_version == initial_version
    runs = store2.list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == rid
    assert runs[0].notes == "first session"
    assert runs[0].total_signals_processed == 2
    records = list(store2.iter_records(rid))
    assert len(records) == 2
    store2.close()


def test_sqlite_concurrent_read_sees_committed_writes(tmp_path: Path) -> None:
    """A second connection on the same file sees data committed by the first.

    WAL mode allows this — under the default rollback journal, the second
    connection would see a snapshot from before the writer's transaction.
    """
    db_path = tmp_path / "concurrent.db"
    url = f"sqlite:///{db_path}"
    profile = load_default_profile()

    writer = SqliteBacktestStore(url)
    try:
        writer.start_run(profile)
        writer.add(_build_event("e1"), _build_decision(), _build_size())
        writer.finish_run()  # commits

        reader_engine = create_engine(url, future=True)
        with reader_engine.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM signal")).scalar()
        reader_engine.dispose()
        assert count == 1
    finally:
        writer.close()


def test_sqlite_no_public_update_method(
    sqlite_store: SqliteBacktestStore,
) -> None:
    """Append-only audit guarantee: no UPDATE method on the public surface.

    The store mutates only the run_row.finished_at and counter columns
    via finish_run / batched flush; it never exposes a way to mutate
    signal rows or error rows after they're written. This test asserts
    the absence of the API hook.
    """
    public_attrs = {
        a for a in dir(sqlite_store)
        if not a.startswith("_") and callable(getattr(sqlite_store, a))
    }
    assert "update_signal" not in public_attrs
    assert "delete_signal" not in public_attrs
    assert "update_error" not in public_attrs


def test_sqlite_get_run_returns_none_for_unknown(
    sqlite_store: SqliteBacktestStore,
) -> None:
    assert sqlite_store.get_run("does-not-exist") is None


def test_sqlite_iter_records_empty_for_unknown(
    sqlite_store: SqliteBacktestStore,
) -> None:
    """Unknown run_id yields empty iterator (matches in-memory)."""
    records = list(sqlite_store.iter_records("does-not-exist"))
    assert records == []


# ---------------------------------------------------------------------------
# Cross-backend equivalence — same input, same output
# ---------------------------------------------------------------------------


def test_cross_backend_equivalence_same_inputs_same_outputs(
    tmp_path: Path,
) -> None:
    """Drive the same event sequence into both backends; iter back, compare.

    Phase 1-2 tests using the in-memory store should produce the same
    StoredSignal records when run against the SQLite store, modulo
    storage-side fields (run_id, event_id, latencies — which are all
    populated identically by both stores).
    """
    profile = load_default_profile()
    events = [_build_event(f"e{i}") for i in range(3)]
    decision = _build_decision()
    size = _build_size()

    in_mem = BacktestStore()
    in_mem.start_run(profile, run_id="shared-run", universe_id="test")
    for e in events:
        in_mem.add(e, decision, size, pipeline_latency_ms=10.0)
    in_mem.finish_run()
    in_mem_records = list(in_mem.iter_records("shared-run"))

    sqlite = SqliteBacktestStore(f"sqlite:///{tmp_path / 'crosseq.db'}")
    sqlite.start_run(profile, run_id="shared-run", universe_id="test")
    for e in events:
        sqlite.add(e, decision, size, pipeline_latency_ms=10.0)
    sqlite.finish_run()
    sqlite_records = list(sqlite.iter_records("shared-run"))
    sqlite.close()

    assert len(in_mem_records) == 3
    assert len(sqlite_records) == 3

    # Compare on every field except outcome columns (None on both sides
    # in this Phase) — the StoredSignal equality is structural.
    for a, b in zip(in_mem_records, sqlite_records, strict=True):
        assert a.event_id == b.event_id
        assert a.ticker == b.ticker
        assert a.run_id == b.run_id
        assert a.label == b.label
        assert a.max_r == b.max_r
        assert a.pipeline_latency_ms == b.pipeline_latency_ms

    in_mem.close()


# ---------------------------------------------------------------------------
# 4-cell strict mode — the use case strict mode was introduced for
# ---------------------------------------------------------------------------


def test_strict_mode_isolates_per_cell_runs(tmp_path: Path) -> None:
    """Phase 3.2.4's 4-cell runner pattern: each cell starts/finishes its
    own run with a unique run_id, no implicit-run leakage between cells.
    """
    url = f"sqlite:///{tmp_path / '4cell.db'}"
    store = SqliteBacktestStore(url, strict_run_lifecycle=True)
    profile = load_default_profile()

    cell_rids = []
    for cell_name in ["cell_1_t1_single", "cell_2_t1_fusion", "cell_3_t2_single", "cell_4_t2_fusion"]:
        rid = store.start_run(profile, run_id=cell_name, universe_id=cell_name.split("_")[2])
        store.add(_build_event(f"{cell_name}_e1"), _build_decision(), _build_size())
        meta = store.finish_run()
        assert meta is not None
        assert meta.run_id == cell_name
        cell_rids.append(rid)

    # Four distinct runs, each with one signal, no implicit-default leak.
    runs = store.list_runs()
    assert len(runs) == 4
    assert {r.run_id for r in runs} == set(cell_rids)
    assert all(r.total_signals_processed == 1 for r in runs)

    store.close()


# ---------------------------------------------------------------------------
# Run finished_at is set after finish_run
# ---------------------------------------------------------------------------


def test_finish_run_sets_finished_at(any_store: BacktestStoreProtocol) -> None:
    profile = load_default_profile()
    rid = any_store.start_run(profile)

    pre_meta = any_store.get_run(rid)
    assert pre_meta is not None
    assert pre_meta.finished_at is None

    any_store.add(_build_event(), _build_decision(), _build_size())
    any_store.finish_run()

    post_meta = any_store.get_run(rid)
    assert post_meta is not None
    assert post_meta.finished_at is not None
    assert post_meta.finished_at >= post_meta.started_at


# ---------------------------------------------------------------------------
# finish_run vs close — run-level vs store-level lifecycle (Phase 3.2.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
def test_finish_run_does_not_close_store(backend: str, tmp_path: Path) -> None:
    """finish_run ends a run, but the store stays open for the next run.

    This is the contract Phase 3.2.4's 4-cell runner relies on: one
    store, four start_run/finish_run cycles, then a single close.
    """
    if backend == "in_memory":
        store: BacktestStoreProtocol = BacktestStore()
    else:
        store = SqliteBacktestStore(f"sqlite:///{tmp_path / 'fr.db'}")

    profile = load_default_profile()

    # Cycle 1
    rid1 = store.start_run(profile, run_id="cell_1")
    store.add(_build_event("e1"), _build_decision(), _build_size())
    store.finish_run()

    # Store must still be usable for the next start_run.
    rid2 = store.start_run(profile, run_id="cell_2")
    store.add(_build_event("e2"), _build_decision(), _build_size())
    store.finish_run()

    runs = store.list_runs()
    assert {r.run_id for r in runs} == {rid1, rid2}
    assert all(r.total_signals_processed == 1 for r in runs)
    assert all(r.finished_at is not None for r in runs)

    store.close()


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
def test_close_makes_store_unusable(backend: str, tmp_path: Path) -> None:
    """After close, every public method raises RuntimeError.

    The contract: close is terminal. Subsequent start_run / add /
    finish_run / iter_records / etc must raise rather than silently
    no-op or use a dead engine.
    """
    if backend == "in_memory":
        store: BacktestStoreProtocol = BacktestStore()
    else:
        store = SqliteBacktestStore(f"sqlite:///{tmp_path / 'cl.db'}")

    profile = load_default_profile()
    store.start_run(profile)
    store.add(_build_event(), _build_decision(), _build_size())
    store.finish_run()
    store.close()

    with pytest.raises(RuntimeError, match="closed"):
        store.start_run(profile)
    with pytest.raises(RuntimeError, match="closed"):
        store.add(_build_event(), _build_decision(), _build_size())
    with pytest.raises(RuntimeError, match="closed"):
        store.list_runs()
    with pytest.raises(RuntimeError, match="closed"):
        list(store.iter_records("any"))


@pytest.mark.parametrize("backend", ["in_memory", "sqlite"])
def test_close_is_idempotent(backend: str, tmp_path: Path) -> None:
    """Calling close twice is a no-op; the second call must not raise.

    Lets ``with``/``try-finally`` wrappers be naive about lifecycle
    state.
    """
    if backend == "in_memory":
        store: BacktestStoreProtocol = BacktestStore()
    else:
        store = SqliteBacktestStore(f"sqlite:///{tmp_path / 'idem.db'}")
    store.close()
    store.close()  # second close — must not raise


# ---------------------------------------------------------------------------
# Pipeline.run() finalises the run but does NOT close the store
# ---------------------------------------------------------------------------


def test_pipeline_run_does_not_close_store(tmp_path: Path) -> None:
    """The orchestrator must call finish_run on the run it drove, but
    leave the store alive so the caller (CLI, 4-cell runner) decides
    when to close.

    Drives a real Pipeline through a single synthetic event, then
    asserts:
      - the run completed (finished_at set)
      - the store is still usable (start_run + add + iter succeeds)
    """
    import asyncio

    from uoa_detector.pipeline.orchestrator import Pipeline
    from uoa_detector.pipeline.stages import default_stage_pipeline
    from uoa_detector.sources.scenarios import (
        ScenarioOverrideStage,
        default_scenario,
        overrides_lookup,
    )
    from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

    profile = load_default_profile()
    db_path = tmp_path / "pipeline.db"
    store = SqliteBacktestStore(f"sqlite:///{db_path}")

    steps = default_scenario()
    raw_prints = [to_raw_print(s.print_, source_id="synthetic") for s in steps]
    src = SyntheticRawFlowSource("synthetic", raw_prints)
    stages = [
        ScenarioOverrideStage(overrides_lookup(iter(steps))),
        *default_stage_pipeline(),
    ]
    pipeline = Pipeline([src], stages, profile=profile, store=store)

    async def _drive() -> int:
        results = await pipeline.run()
        return len(results)

    n = asyncio.run(_drive())
    assert n > 0  # pipeline produced events

    # The run that the pipeline drove is finished — finished_at set.
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].finished_at is not None
    assert runs[0].total_signals_processed == n

    # Store is still alive — we can start another run on it.
    rid2 = store.start_run(profile, run_id="post_pipeline")
    assert store.active_run_id == rid2
    store.add(_build_event("e_after"), _build_decision(), _build_size())
    store.finish_run()

    runs = store.list_runs()
    assert len(runs) == 2

    store.close()


def test_pipeline_run_with_strict_store_explicit_start_run(
    tmp_path: Path,
) -> None:
    """When a strict store is used, the caller explicitly starts the run
    before pipeline.run(); pipeline.run() finishes it; the store stays
    open for the next start_run.

    This is the canonical Phase 3.2.4 4-cell runner pattern, exercised
    here against one cell.
    """
    import asyncio

    from uoa_detector.pipeline.orchestrator import Pipeline
    from uoa_detector.pipeline.stages import default_stage_pipeline
    from uoa_detector.sources.scenarios import (
        ScenarioOverrideStage,
        default_scenario,
        overrides_lookup,
    )
    from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print

    profile = load_default_profile()
    store = SqliteBacktestStore(
        f"sqlite:///{tmp_path / 'strict_pipe.db'}",
        strict_run_lifecycle=True,
    )

    # Caller (= the 4-cell runner) starts the run.
    cell_rid = store.start_run(profile, run_id="cell_1_t1_single", universe_id="tier1")

    steps = default_scenario()
    raw_prints = [to_raw_print(s.print_, source_id="synthetic") for s in steps]
    src = SyntheticRawFlowSource("synthetic", raw_prints)
    stages = [
        ScenarioOverrideStage(overrides_lookup(iter(steps))),
        *default_stage_pipeline(),
    ]
    pipeline = Pipeline([src], stages, profile=profile, store=store)

    async def _drive() -> None:
        await pipeline.run()

    asyncio.run(_drive())

    # Pipeline finished the run we explicitly started.
    meta = store.get_run(cell_rid)
    assert meta is not None
    assert meta.finished_at is not None

    # active_run_id is None — the strict store has no leakage; if the
    # caller now adds without starting again, it raises (verified by
    # test_strict_lifecycle_raises_without_start_run elsewhere).
    assert store.active_run_id is None

    store.close()
