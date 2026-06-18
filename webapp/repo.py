"""SignalRepo — read detector signals for the screener UI.

Reads the ``signal`` table (the same schema the backtest/live store writes:
top-level columns for filtering + ``full_record_json`` holding the full
``StoredSignal``). One repo works for both SQLite (local dev) and Postgres
(Railway) — only the ``DATABASE_URL`` differs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.backtest.store import StoredSignal

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULT_URL = "sqlite:///webapp/seed.db"


def _normalize_url(url: str) -> str:
    # Railway hands out ``postgres://``; we use the psycopg (v3) driver, so
    # normalise to ``postgresql+psycopg://``.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# Sort key -> (column, descending). Only SQL-backed columns so ORDER BY +
# LIMIT stay correct (no post-load reordering of a truncated page).
_SORTS = {
    "score": (SignalRow.combined_score_post, True),
    "newest": (SignalRow.ts, True),
    "oldest": (SignalRow.ts, False),
    "size": (SignalRow.max_r, True),
}
DEFAULT_SORT = "score"


@dataclass(frozen=True)
class SignalFilters:
    ticker: str | None = None
    label: str | None = None
    min_score: float | None = None
    since: datetime | None = None
    sort: str = DEFAULT_SORT
    limit: int = 100


class SignalRepo:
    """Read-only access to stored signals, filterable for the dashboard."""

    def __init__(self, database_url: str | None = None) -> None:
        url = _normalize_url(
            database_url or os.environ.get("DATABASE_URL", _DEFAULT_URL),
        )
        self._engine = create_engine(url, future=True)
        self._session = sessionmaker(self._engine, future=True)

    def signals(self, filters: SignalFilters) -> list[StoredSignal]:
        column, descending = _SORTS.get(filters.sort, _SORTS[DEFAULT_SORT])
        order = column.desc() if descending else column.asc()
        stmt = select(SignalRow).order_by(order)
        if filters.ticker:
            stmt = stmt.where(SignalRow.ticker == filters.ticker.upper())
        if filters.label:
            stmt = stmt.where(SignalRow.label == filters.label)
        if filters.min_score is not None:
            stmt = stmt.where(SignalRow.combined_score_post >= filters.min_score)
        if filters.since is not None:
            stmt = stmt.where(SignalRow.ts >= filters.since)
        stmt = stmt.limit(filters.limit)
        with self._session() as session:
            rows: Sequence[SignalRow] = session.execute(stmt).scalars().all()
            return [StoredSignal.model_validate_json(r.full_record_json) for r in rows]

    def tickers(self) -> list[str]:
        with self._session() as session:
            return sorted(
                r[0] for r in session.execute(
                    select(SignalRow.ticker).distinct(),
                ).all()
            )

    def labels(self) -> list[str]:
        with self._session() as session:
            return sorted(
                r[0] for r in session.execute(
                    select(SignalRow.label).distinct(),
                ).all()
            )

    def count(self) -> int:
        with self._session() as session:
            return len(session.execute(select(SignalRow.event_id)).all())
