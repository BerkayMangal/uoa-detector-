"""Phase 5.2.PERF7: every engine the web app uses checks a connection before using it.

The board reads about twenty sources per render against a database in another
Railway project. A connection the proxy has dropped costs a full reconnect on
its next use — 0.37 s, measured live. pool_pre_ping replaces only a dead
connection; a timed recycle was tried and made it worse, because these pages are
read minutes apart and every render then exceeded the window.
"""

from __future__ import annotations

from pathlib import Path

from webapp import gamma, journal, repo
from webapp.board.db import make_engine


def test_every_web_engine_pre_pings(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'pool.db'}"
    engines = [
        make_engine(url),
        gamma.GammaRepo(url)._engine,
        journal.JournalRepo(url)._engine,
        repo.SignalRepo(url)._engine,
    ]
    for engine in engines:
        assert engine.pool._pre_ping is True, engine
        # No timed recycle: these pages are read minutes apart, so a window would
        # force a reconnect on every render instead of preventing one.
        assert engine.pool._recycle == -1, engine


def test_make_engine_pre_pings_on_the_fallback_url() -> None:
    assert make_engine(None).pool._pre_ping is True
