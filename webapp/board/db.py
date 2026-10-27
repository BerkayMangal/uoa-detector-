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
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from webapp.database_settings import settings as database_settings

TABLE_PREFIX = "alfa_"
_DEFAULT_URL = "sqlite:///webapp/seed.db"


class AlfaBase(DeclarativeBase):
    """Declarative base for every ``alfa_*`` table."""


def normalize_database_url(url: str) -> str:
    """Map Railway's ``postgres://`` / ``postgresql://`` URLs to the psycopg driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(database_url: str | None = None) -> Engine:
    """Engine for ``database_url``, or ``$DATABASE_URL``, or the local seed DB."""
    url = database_url or os.environ.get("DATABASE_URL", _DEFAULT_URL)
    normalized = normalize_database_url(url)
    parsed = make_url(normalized)
    if parsed.get_backend_name() == "postgresql":
        original = parsed.query.get("options", os.environ.get("PGOPTIONS", ""))
        if not isinstance(original, str):
            raise ValueError("ambiguous database connection options")
        options = (
            original
            + " -c max_parallel_workers_per_gather="
            + str(database_settings().max_parallel_workers_per_gather)
        ).strip()
        return create_engine(
            normalized, future=True, pool_pre_ping=True, connect_args={"options": options}
        )
    return create_engine(normalized, future=True, pool_pre_ping=True)


def create_tables(engine: Engine) -> None:
    """Create every missing ``alfa_*`` table. Never alters or drops anything."""
    AlfaBase.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
