"""Database base for the Alfa Board tables (Phase 5.2, contract §4.2).

Every board table:
- uses this declarative base, separate from the backtest store and journal bases;
- carries the ``alfa_`` prefix (the production Postgres is shared with another service);
- is created with ``create_all``, which only adds missing tables.

Existing tables (``signal``, ``trade``, ...) are never altered. Append-only
tables (telemetry, decision cards, fills, outcomes) are never dropped or reset.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Phase 5.2.PERF7: the board reads about twenty sources per render and Railway's
# Postgres lives in another project, so a connection the proxy has already dropped
# costs a full reconnect on its next use (0.37 s, measured live). pool_pre_ping
# checks a connection before handing it out and replaces only a dead one. An earlier
# attempt also set pool_recycle=280 s and made things worse: these pages are read
# minutes apart, so every render exceeded the window and paid the reconnect anyway.

TABLE_PREFIX = "alfa_"
_DEFAULT_URL = "sqlite:///webapp/seed.db"


class AlfaBase(DeclarativeBase):
    """Declarative base for every ``alfa_*`` table."""


def normalize_database_url(url: str) -> str:
    """Map Railway's ``postgres://`` / ``postgresql://`` URLs to the psycopg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(database_url: str | None = None) -> Engine:
    """Engine for ``database_url``, or ``$DATABASE_URL``, or the local seed DB."""
    url = database_url or os.environ.get("DATABASE_URL", _DEFAULT_URL)
    return create_engine(normalize_database_url(url), future=True, pool_pre_ping=True)


def create_tables(engine: Engine) -> None:
    """Create every missing ``alfa_*`` table. Never alters or drops anything."""
    AlfaBase.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
