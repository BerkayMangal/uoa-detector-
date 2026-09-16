"""Fetch coverage for the Alfa Board delayed families (Phase 5.2.D1).

Contract ``docs/phase-5.2-alfa-board-acceptance.md`` §7 together with §2 R-UN1:
a family that was never fetched must read ``bilinmiyor``, never "no records".

``alfa_delayed`` is append-only, so a family whose source answered with nothing
stores no row at all — which on its own is indistinguishable from a family that
was never asked. This table records the attempt itself, the way
``alfa_catalyst_fetch`` does for catalysts (decision P19).

- Key (ticker, family); one row per pair, rebuildable: the newest attempt
  replaces the previous one.
- ``last_status`` is the delayed job's own status: ``ok`` and ``no_data`` mean
  the source answered, ``degraded`` means it did not.
- ``last_success_at`` only moves when the source answered, so a degraded
  attempt never makes stored rows look freshly confirmed.
- ``truncated`` is the source's own "there are more rows than I returned"
  signal, kept from the last answered attempt so the render can disclose it.

It holds coverage, never evidence: nothing here is counted (R-DL1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import Boolean, DateTime, String, Table, select
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from webapp.board.daily_close import FetchStatus

# Statuses that mean the source answered (with rows or with nothing).
_ANSWERED: frozenset[str] = frozenset({"ok", "no_data"})


class AlfaDelayedFetch(AlfaBase):
    """The newest fetch attempt per (ticker, family). Rebuildable, never evidence."""

    __tablename__ = "alfa_delayed_fetch"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    family: Mapped[str] = mapped_column(String, primary_key=True)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String)  # ok | no_data | degraded
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)


def ensure_delayed_coverage_tables(engine: Engine) -> None:
    """Create ``alfa_delayed_fetch`` when missing. Never alters or drops anything."""
    cast("Table", AlfaDelayedFetch.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class CoverageView:
    """One family's fetch coverage, as the render reads it."""

    ticker: str
    family: str
    last_attempt_at: datetime
    last_status: str
    last_success_at: datetime | None
    truncated: bool

    @property
    def answered(self) -> bool:
        """True once the source has answered at least once."""
        return self.last_success_at is not None

    @property
    def last_attempt_failed(self) -> bool:
        """True when the newest attempt did not reach the source."""
        return self.last_status not in _ANSWERED


def record_fetch(
    factory: sessionmaker[Session],
    ticker: str,
    family: str,
    *,
    status: FetchStatus,
    at: datetime,
    truncated: bool,
) -> None:
    """Record one attempt for (ticker, family). Only an answered attempt moves the success time."""
    symbol = ticker.strip().upper()
    moment = _utc(at)
    answered = status in _ANSWERED
    with factory() as session:
        row = session.get(AlfaDelayedFetch, (symbol, family))
        if row is None:
            session.add(
                AlfaDelayedFetch(
                    ticker=symbol,
                    family=family,
                    last_attempt_at=moment,
                    last_status=status,
                    last_success_at=moment if answered else None,
                    truncated=truncated if answered else False,
                ),
            )
        else:
            row.last_attempt_at = moment
            row.last_status = status
            if answered:
                row.last_success_at = moment
                row.truncated = truncated
        session.commit()


def load_coverage(engine: Engine, tickers: Iterable[str]) -> dict[tuple[str, str], CoverageView]:
    """Coverage of every requested ticker in one query, keyed by (ticker, family). Read-only."""
    symbols = _normalized(tickers)
    if not symbols:
        return {}
    stmt = select(AlfaDelayedFetch).where(AlfaDelayedFetch.ticker.in_(symbols))
    with session_factory(engine)() as session:
        rows = session.execute(stmt).scalars().all()
    return {(row.ticker, row.family): _view(row) for row in rows}


def coverage_by_family(
    coverage: Mapping[tuple[str, str], CoverageView], ticker: str,
) -> dict[str, CoverageView]:
    """The family → coverage slice of one ticker."""
    symbol = ticker.strip().upper()
    return {family: view for (row_ticker, family), view in coverage.items() if row_ticker == symbol}


def _view(row: AlfaDelayedFetch) -> CoverageView:
    return CoverageView(
        ticker=row.ticker,
        family=row.family,
        last_attempt_at=_utc(row.last_attempt_at),
        last_status=row.last_status,
        last_success_at=None if row.last_success_at is None else _utc(row.last_success_at),
        truncated=bool(row.truncated),
    )


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _normalized(tickers: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in tickers:
        symbol = raw.strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out
