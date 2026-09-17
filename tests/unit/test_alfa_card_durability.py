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
)

# The pinned exception (see the module docstring). Nothing else may appear.
_KNOWN_EXCEPTIONS: Final = {
    ("webapp/gamma.py", "reset("),        # def GammaRepo.reset
    ("webapp/gamma.py", ".drop("),        # the drop + create_all inside it
    ("webapp/gamma_live.py", "reset("),   # the one call, at gamma-refresh startup
}

# Append-only tables: rows are evidence and are never deleted in bulk.
_APPEND_ONLY: Final = (AlfaDecisionCard, AlfaStageTelemetry, AlfaPrintMeta)

# Rebuildable caches: their rows are re-fetched from the vendor, so a row
# delete there costs nothing. Adding a name here is a deliberate act.
_REBUILDABLE_CACHES: Final = {"AlfaCatalyst", "AlfaAtm", "AlfaAtmExpiry", "AlfaEtfHolding"}

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


def test_the_card_module_itself_has_no_destructive_call() -> None:
    source = (_WEBAPP / "board" / "cards.py").read_text(encoding="utf-8")
    hits = [
        line.strip()
        for line in source.splitlines()
        if _MODULE_DESTRUCTIVE.search(line) and not line.strip().startswith("#")
    ]
    assert hits == [], hits


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
