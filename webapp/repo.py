"""SignalRepo — read detector signals for the screener UI.

Reads the ``signal`` table (the same schema the backtest/live store writes:
top-level columns for filtering + ``full_record_json`` holding the full
``StoredSignal``). One repo works for both SQLite (local dev) and Postgres
(Railway) — only the ``DATABASE_URL`` differs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.backtest.store import StoredSignal

if TYPE_CHECKING:
    from collections.abc import Sequence

_logger = logging.getLogger(__name__)
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
    run_id: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    count: int
    latest_ts: datetime | None

    @property
    def is_live(self) -> bool:
        return self.run_id.startswith("live-")

    @property
    def label(self) -> str:
        if self.is_live:
            return f"Live · {self.run_id.removeprefix('live-')}"
        if self.run_id == "seed":
            return "Sample (Jul 2025 backtest)"
        return self.run_id


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
        if filters.run_id:
            stmt = stmt.where(SignalRow.run_id == filters.run_id)
        stmt = stmt.limit(filters.limit)
        with self._session() as session:
            rows: Sequence[SignalRow] = session.execute(stmt).scalars().all()
            out: list[StoredSignal] = []
            for r in rows:
                # One schema-drifted / malformed row must not blank the page.
                try:
                    out.append(StoredSignal.model_validate_json(r.full_record_json))
                except Exception:
                    _logger.warning("skipping unparseable signal %s/%s", r.run_id, r.event_id)
            return out

    def get_signal(self, run_id: str, event_id: str) -> StoredSignal | None:
        stmt = select(SignalRow).where(
            SignalRow.run_id == run_id, SignalRow.event_id == event_id,
        )
        with self._session() as session:
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None
            try:
                return StoredSignal.model_validate_json(row.full_record_json)
            except Exception:
                _logger.warning("unparseable signal %s/%s", run_id, event_id)
                return None

    def ticker_label_options(
        self, run_id: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Distinct tickers + labels for the filter dropdowns, in ONE DB
        round-trip (the dashboard hot path; remote Postgres RTT dominates, so
        merging two SELECTs into one DISTINCT (ticker,label) query halves it).
        Cross-DB (plain SQL) — no Postgres-only array_agg."""
        stmt = select(SignalRow.ticker, SignalRow.label).distinct()
        if run_id:
            stmt = stmt.where(SignalRow.run_id == run_id)
        with self._session() as session:
            rows = session.execute(stmt).all()
        return sorted({r[0] for r in rows}), sorted({r[1] for r in rows})

    def runs(self) -> list[RunInfo]:
        """All runs with signal counts + latest event time, newest first.

        Live runs (``live-YYYY-MM-DD``) sort to the top by latest_ts, so the
        dashboard defaults to today's live flow when the worker is running.
        """
        stmt = (
            select(
                SignalRow.run_id,
                func.count().label("n"),
                func.max(SignalRow.ts).label("latest"),
            )
            .group_by(SignalRow.run_id)
        )
        with self._session() as session:
            rows = session.execute(stmt).all()
        infos = [RunInfo(run_id=r[0], count=int(r[1]), latest_ts=r[2]) for r in rows]
        infos.sort(key=lambda i: (i.latest_ts is not None, i.latest_ts), reverse=True)
        return infos
