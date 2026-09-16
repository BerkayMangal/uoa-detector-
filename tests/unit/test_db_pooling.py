"""Phase 5.2.PERF5: every engine the web app uses reuses warm connections.

The board reads about twenty sources per render. Railway's Postgres lives in
another project, so a connection the proxy has already dropped costs a full
reconnect on its next use — measured at 0.37 s each, which is why the render
swung between 0.4 s and 8.6 s on identical code. pool_pre_ping checks a
connection before handing it out; pool_recycle retires it before the proxy does.
"""

from __future__ import annotations

from pathlib import Path

from webapp import gamma, journal, repo
from webapp.board.db import make_engine


def _engines(tmp_path: Path) -> list[object]:
    url = f"sqlite:///{tmp_path / 'pool.db'}"
    return [
        make_engine(url),
        gamma.GammaRepo(url)._engine,
        journal.JournalRepo(url)._engine,
        repo.SignalRepo(url)._engine,
    ]


def test_every_web_engine_pre_pings_and_recycles(tmp_path: Path) -> None:
    for engine in _engines(tmp_path):
        pool = engine.pool  # type: ignore[attr-defined]
        assert pool._pre_ping is True, engine
        assert 0 < pool._recycle <= 300, engine


def test_make_engine_still_pre_pings_on_the_fallback_url() -> None:
    """No URL means the seed database, and it is pooled like every other engine."""
    engine = make_engine(None)
    assert engine.pool._pre_ping is True
