"""Phase 5.2.C1c: nothing in the webapp can destroy board data.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 ("append-only;
never dropped, reset or rewritten") and §6.

A decision card is the only record here that cannot be rebuilt: a pass the
owner recorded today is gone forever if a startup, a worker, a refresher or a
repository ever drops, resets or truncates its table. So this file does not
test one function — it scans the whole ``webapp`` package for the shapes that
would do it, and fails if a new one appears.

The one known exception is pinned by name: ``GammaRepo.reset()``, called at
gamma-refresh startup, drops and recreates ``gamma_regime`` — a non-``alfa_``
table that is a full live rebuild every cycle. If that set ever grows, this
test fails and a human reads the new line before it ships.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from fastapi.testclient import TestClient
from webapp.board.cards import AlfaDecisionCard, CardRepo, ensure_card_tables
from webapp.board.db import TABLE_PREFIX, AlfaBase, create_tables, make_engine
from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry
from webapp.gamma import GammaRow

from tests.unit._webapp_auth import set_web_auth

if TYPE_CHECKING:
    from types import ModuleType

_WEBAPP: Final = Path(__file__).resolve().parents[2] / "webapp"

# Every shape that can destroy a table or its contents wholesale.
_DESTRUCTIVE: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("drop_all(", re.compile(r"\bdrop_all\s*\(")),
    (".drop(", re.compile(r"\.drop\s*\(")),
    ("DROP TABLE", re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\b")),
    ("truncate(", re.compile(r"\btruncate\s*\(")),
    ("reset(", re.compile(r"\breset\s*\(")),
    # Raw SQL and the driver-level escape hatch. None of these appear in the
    # package today, which is precisely why they were absent: a scan assembled
    # from what the code currently contains cannot catch what someone adds
    # tomorrow, and this scan exists for tomorrow. ``DELETE FROM`` slipped past
    # BOTH scans — the shape list below and ``_DELETE_CALL``, which only matches
    # SQLAlchemy's ``delete(Model)`` form and never raw SQL (audit 2026-09-19).
    ("DELETE FROM", re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE)),
    ("ALTER TABLE", re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)),
    ("exec_driver_sql(", re.compile(r"\bexec_driver_sql\s*\(")),
)

# The pinned exception (see the module docstring). Nothing else may appear.
_KNOWN_EXCEPTIONS: Final = {
    ("webapp/gamma.py", "reset("),        # def GammaRepo.reset
    ("webapp/gamma.py", ".drop("),        # the drop + create_all inside it
    ("webapp/gamma_live.py", "reset("),   # the one call, at gamma-refresh startup
}

# Phase 5.2.PERF8 / decision P37: EVERY alfa_ table is classified here, not
# just the three the card work touched. The contract (§4.2) states the rule in
# prose — "append-only tables hold forward evidence and are never dropped or
# rewritten" — but prose binds nothing, and a twenty-first table could be added
# tomorrow with no classification and no test to notice.
#
# Append-only: rows are forward evidence. If they are lost they cannot be
# recreated, because the moment that produced them is gone. A pass the owner
# recorded, a telemetry branch that was taken, the OI that confirmed an
# opening — nobody can re-derive these from the vendor tomorrow.
_APPEND_ONLY_TABLES: Final = {
    "alfa_decision_card",    # the owner's recorded decision
    "alfa_fill",             # what he actually paid
    "alfa_outcome",          # how it resolved
    "alfa_stage_telemetry",  # which branch each module took, per event
    "alfa_print_meta",       # the print as it arrived
    "alfa_regime",           # regime history; tripwire persistence is counted over it
    "alfa_oi_confirm",       # OI status advances bekliyor -> final exactly once
    "alfa_delayed",          # disclosed filings, keyed by dedupe_key
    "alfa_daily_close",      # the close that scores an outcome
    "alfa_daily_bar",        # the session OHLC behind ATR; a lost bar breaks the window
}

# Rebuildable: refreshed market data or fetch bookkeeping. A row lost here is
# re-fetched from the vendor on the next cycle and costs nothing. Adding a name
# here is a deliberate act and says: this table holds no evidence.
_REBUILDABLE_TABLES: Final = {
    "alfa_quote",           # refresher upsert
    "alfa_contract_depth",  # refresher upsert, top-K contracts
    "alfa_atm",             # refresher upsert
    "alfa_atm_expiry",      # listed expiries, daily breakdown call
    "alfa_net_prem",        # cumulative day totals, refetchable
    "alfa_ticker_info",     # issue_type, sector
    "alfa_catalyst",        # rebuilt per day
    "alfa_catalyst_fetch",  # fetch coverage bookkeeping
    "alfa_delayed_fetch",   # fetch coverage bookkeeping
    "alfa_etf_holding",     # rebuilt per snapshot
    "alfa_job_run",         # records what ran, not what was observed
}

# The subset carried as model classes, for the identity checks below.
_APPEND_ONLY: Final = (AlfaDecisionCard, AlfaStageTelemetry, AlfaPrintMeta)

# Rebuildable caches: their rows are re-fetched from the vendor, so a row
# delete there costs nothing. Adding a name here is a deliberate act.
_REBUILDABLE_CACHES: Final = {"AlfaCatalyst", "AlfaAtm", "AlfaAtmExpiry", "AlfaEtfHolding"}


def _alfa_models() -> dict[str, str]:
    """{model name -> table name} for every mapped alfa_ table.

    Imports every module of ``webapp.board`` first: a model class that no test
    happens to import is exactly the one that would slip through unclassified.
    """
    import importlib
    import pkgutil

    import webapp.board as board

    for module in pkgutil.iter_modules(board.__path__):
        importlib.import_module(f"webapp.board.{module.name}")
    return {
        mapper.class_.__name__: str(mapper.class_.__tablename__)
        for mapper in AlfaBase.registry.mappers
    }


def test_every_alfa_table_is_classified_append_only_or_rebuildable() -> None:
    """The binding form of contract §4.2. A new alfa_ table fails this test
    until a human decides, in writing, whether losing its rows costs evidence."""
    tables = set(_alfa_models().values())
    classified = _APPEND_ONLY_TABLES | _REBUILDABLE_TABLES

    assert tables - classified == set(), (
        "a new alfa_ table is unclassified. Decide whether its rows are forward "
        "evidence (append-only: losing them loses something nobody can recreate) "
        "or refreshed vendor data (rebuildable), then add it to the matching set "
        f"in this file: {sorted(tables - classified)}"
    )
    assert classified - tables == set(), (
        f"classified table no longer exists: {sorted(classified - tables)}"
    )
    assert not (_APPEND_ONLY_TABLES & _REBUILDABLE_TABLES), "a table cannot be both"
    # Every classified name really carries the prefix the contract requires.
    for name in classified:
        assert name.startswith(TABLE_PREFIX), name


def test_the_classification_covers_the_append_only_models_by_identity() -> None:
    """The three model classes this file holds directly must agree with the
    table-name classification, so the two lists cannot drift apart."""
    for model in _APPEND_ONLY:
        assert str(model.__tablename__) in _APPEND_ONLY_TABLES, model.__name__
    names = _alfa_models()
    for model_name in _REBUILDABLE_CACHES:
        assert names[model_name] in _REBUILDABLE_TABLES, model_name


def test_no_bulk_delete_touches_any_append_only_table() -> None:
    """The wider form of the delete scan: keyed on all nine append-only tables,
    not just the three whose model classes this file imports."""
    names = _alfa_models()
    append_only_models = {
        model for model, table in names.items() if table in _APPEND_ONLY_TABLES
    }
    deleted = {
        match.group(1)
        for _name, source in _sources()
        for match in _DELETE_CALL.finditer(source)
    }
    offenders = deleted & append_only_models
    assert not offenders, (
        "bulk delete against an append-only table — these rows are forward "
        f"evidence and cannot be re-fetched: {sorted(offenders)}"
    )

_DELETE_CALL: Final = re.compile(r"\bdelete\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)")
_MODULE_DESTRUCTIVE: Final = re.compile(r"\b(?:delete|update|drop|truncate|reset)\s*\(")


def _sources() -> list[tuple[str, str]]:
    """(relative path, source) for every module of the webapp package."""
    return [
        (path.relative_to(_WEBAPP.parent).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(_WEBAPP.rglob("*.py"))
    ]


def test_the_webapp_has_no_destructive_path_beyond_the_pinned_exception() -> None:
    found: set[tuple[str, str]] = set()
    lines: list[str] = []
    for name, source in _sources():
        for number, line in enumerate(source.splitlines(), start=1):
            for label, pattern in _DESTRUCTIVE:
                if pattern.search(line):
                    found.add((name, label))
                    lines.append(f"{name}:{number}: {line.strip()}")

    assert found == _KNOWN_EXCEPTIONS, (
        "a drop / reset / truncate path appeared or moved; board tables are append-only "
        "and a pass recorded today cannot be rebuilt tomorrow. Hits:\n" + "\n".join(lines)
    )
    # Exactly three lines, so a second drop cannot hide behind the pinned one.
    assert len(lines) == 3, lines


def test_the_pinned_exception_is_not_an_alfa_table() -> None:
    """The one allowed drop rebuilds ``gamma_regime``, which holds no board record."""
    assert GammaRow.__tablename__ == "gamma_regime"
    assert not GammaRow.__tablename__.startswith(TABLE_PREFIX)
    assert GammaRow.__tablename__ not in {
        str(model.__tablename__) for model in _APPEND_ONLY
    }


def test_no_bulk_delete_touches_an_append_only_table() -> None:
    append_only = {model.__name__ for model in _APPEND_ONLY}
    deleted = {
        match.group(1)
        for _name, source in _sources()
        for match in _DELETE_CALL.finditer(source)
    }
    assert not (deleted & append_only), f"bulk delete against an append-only table: {deleted}"
    assert deleted <= _REBUILDABLE_CACHES, (
        f"delete() against a table that is not a rebuildable cache: {deleted - _REBUILDABLE_CACHES}"
    )


_WOULD_BE_CAUGHT: Final = (
    "AlfaBase.metadata.drop_all(engine)",
    'cast("Table", AlfaDecisionCard.__table__).drop(engine, checkfirst=True)',
    'session.execute(text("DROP TABLE alfa_decision_card"))',
    'session.execute(text("TRUNCATE alfa_decision_card"))',
    "table.truncate()",
    "repo.reset()",
    "    def reset(self) -> None:",
    # The four the scan could not see before the 2026-09-19 audit. Each one wipes
    # or reshapes an append-only table and each one would have passed silently.
    'session.execute(text("DELETE FROM alfa_decision_card"))',
    'session.execute(text("delete from alfa_fill where 1=1"))',
    'session.execute(text("ALTER TABLE alfa_decision_card DROP COLUMN decision"))',
    'connection.exec_driver_sql("DELETE FROM alfa_outcome")',
)


def test_the_scan_catches_the_lines_it_exists_to_catch() -> None:
    """A guard that cannot fail is not a guard: every shape above must be flagged."""
    for line in _WOULD_BE_CAUGHT:
        assert any(pattern.search(line) for _label, pattern in _DESTRUCTIVE), line
    caught = _DELETE_CALL.search("session.execute(delete(AlfaDecisionCard).where(...))")
    assert caught is not None
    assert caught.group(1) == "AlfaDecisionCard"
    # And the everyday lines of this package are not flagged, so the scan stays readable.
    for innocent in (
        "ensure_card_tables(engine)",
        "AlfaBase.metadata.create_all(engine)",
        "notes.append(_say('note.truncated'))",
        "truncated: bool = False",
    ):
        assert not any(pattern.search(innocent) for _label, pattern in _DESTRUCTIVE), innocent


# The one write to an existing card the contract allows: CardRepo.link_trade sets
# trade_id once, on a log card that has none (§3). It became VISIBLE to this scan on
# 2026-09-19, when the audit replaced a read-then-write — `row.trade_id = trade_id`
# after an is_linkable check — that two concurrent journal POSTs could both pass,
# the second silently re-pointing a card already linked. The write did not appear
# that day; only the scan's ability to see it did, and a guarded one-statement
# UPDATE is the shape oi_confirm and outcomes already use for the same reason.
_PINNED_CARD_WRITE: Final = "sa.update(AlfaDecisionCard)"


def test_the_card_module_makes_exactly_one_guarded_write() -> None:
    """Not "no writes": one, pinned by its text, so a second cannot hide behind it."""
    source = (_WEBAPP / "board" / "cards.py").read_text(encoding="utf-8")
    hits = [
        line.strip()
        for line in source.splitlines()
        if _MODULE_DESTRUCTIVE.search(line) and not line.strip().startswith("#")
    ]
    assert hits == [_PINNED_CARD_WRITE], hits


def test_the_card_table_is_registered_for_creation_only() -> None:
    assert AlfaDecisionCard.__tablename__ == "alfa_decision_card"
    assert AlfaDecisionCard.__tablename__.startswith(TABLE_PREFIX)
    assert AlfaDecisionCard.__tablename__ in AlfaBase.metadata.tables


def _write(repo: CardRepo, decision: str = "pas") -> str:
    return repo.write_card(
        decision=decision,  # type: ignore[arg-type]
        ticker="SPY",
        direction="yukarı",
        run_id="live-2026-09-16",
        dominant_option_symbol="SPY260918C00760000",
        card={"row_view": {"ticker": "SPY"}},
        board_profile_hash="board-hash",
        calibration_profile_hash="calib-hash",
    )


def test_ensure_card_tables_is_idempotent_and_never_rewrites_a_row(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'durable.db'}"
    engine = make_engine(url)
    try:
        repo = CardRepo(engine)
        card_id = _write(repo)
        before = repo.get_card(card_id)
        assert before is not None

        for _ in range(3):
            ensure_card_tables(engine)
            create_tables(engine)  # the metadata-wide creator is additive too

        assert repo.get_card(card_id) == before
        assert len(repo.list_cards()) == 1
        # A restart (a fresh repo over the same database) still sees it.
        assert CardRepo(engine).get_card(card_id) == before
    finally:
        engine.dispose()


def test_app_startup_leaves_a_written_card_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's fear, tested directly: booting the app must not cost a card."""
    url = f"sqlite:///{tmp_path / 'startup.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker, no gamma refresh
    set_web_auth(monkeypatch)

    engine = make_engine(url)
    try:
        repo = CardRepo(engine)
        card_id = _write(repo, decision="log")
        before = repo.get_card(card_id)
        assert before is not None
        assert before.created_at <= datetime.now(UTC)

        import webapp.main as m

        _reset(m)
        with TestClient(m.app) as client:  # runs the lifespan: profiles, tasks, shutdown
            assert client.get("/health").status_code == 200
        _reset(m)

        after = CardRepo(engine).get_card(card_id)
        assert after == before
    finally:
        engine.dispose()


def _reset(m: ModuleType) -> None:
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None
    m._CARDS = None
