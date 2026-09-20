"""Ticker info (issue type and sector) for the Alfa Board (Phase 5.2.A3).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 (daily jobs), §4.2
(``alfa_ticker_info``: rebuildable, upsert), §4.4 (≈ 10 requests a day) and §9
(the Sektör family is ``kapsam-dışı`` only when ``issue_type`` is ETF or Index).

Endpoint, live-probed 2026-09-15 (``alfa_disc/probe_clusters.md``):

- ``GET /api/stock/{ticker}/info`` returns ONE object under ``data``, not a
  list.
  - ``issue_type`` separates ``Common Stock``, ``ETF``, ``Index`` and ``ADR``.
  - ``sector`` is one of the 11 UW sector names. It is null for an ETF (SMH
    live; SPY per the M25 provider notes) and empty for an index.

Why the board keeps its own copy: M25 answers ``no_sector`` both for an ETF,
which has no sector by construction, and for a common stock whose sector is
missing. Only ``issue_type`` tells them apart. The first is out of scope; the
second is unknown.

Error handling (UW response rules):

- ``UnusualWhalesNotFoundError`` means no data: the ticker is stored with a null
  issue type and sector, so the job does not retry it until the next day, and
  the Sektör family can never read that as out of scope.
- Rate limit, transient and open-breaker errors mark the fetch degraded, and
  nothing is written.
- ``UnusualWhalesDailyLimitError`` and other ``UnusualWhalesAuthError``
  propagate.
- A payload whose ``data`` is not an object is degraded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from weakref import WeakSet

from sqlalchemy import DateTime, String, Table, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.db import AlfaBase

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.engine import Engine

_logger = logging.getLogger(__name__)

TICKER_INFO_PATH: Final = "/api/stock/{ticker}/info"

# issue_type values (case-insensitive) with no sector by construction.
_FUND_OR_INDEX: Final = frozenset({"etf", "index"})
_DEGRADABLE = (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError)


class AlfaTickerInfo(AlfaBase):
    """``alfa_ticker_info``: the latest ``/info`` read per ticker (rebuildable)."""

    __tablename__ = "alfa_ticker_info"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    issue_type: Mapped[str | None] = mapped_column(String, nullable=True)
    sector: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def ensure_ticker_info_tables(engine: Engine) -> None:
    """Create ``alfa_ticker_info`` if missing. Never alters or drops anything."""
    cast("Table", AlfaTickerInfo.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class TickerInfoSnapshot:
    ticker: str
    issue_type: str | None
    sector: str | None


@dataclass(frozen=True)
class TickerInfoFetch:
    ticker: str
    snapshot: TickerInfoSnapshot | None  # None only when degraded
    degraded: bool


@dataclass(frozen=True)
class TickerInfoView:
    ticker: str
    issue_type: str | None
    sector: str | None
    fetched_at: datetime

    @property
    def fund_or_index(self) -> bool:
        """True only for an ETF or an index: no sector by construction."""
        return self.issue_type is not None and self.issue_type.strip().lower() in _FUND_OR_INDEX


def _symbol(ticker: str) -> str:
    return ticker.strip().upper()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def parse_ticker_info(payload: object, ticker: str) -> TickerInfoSnapshot:
    """Issue type and sector; raises ``ValueError`` when ``data`` is not an object."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        msg = "expected {'data': {...}}"
        raise ValueError(msg)
    return TickerInfoSnapshot(
        ticker=_symbol(ticker),
        issue_type=_text(data.get("issue_type")),
        sector=_text(data.get("sector")),
    )


class _JsonClient(Protocol):
    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...


async def fetch_ticker_info(client: _JsonClient, ticker: str) -> TickerInfoFetch:
    """One ``/info`` call."""
    symbol = _symbol(ticker)
    try:
        payload = await client.request_json(TICKER_INFO_PATH.format(ticker=symbol))
    except UnusualWhalesDailyLimitError:
        raise
    except UnusualWhalesNotFoundError:
        empty = TickerInfoSnapshot(ticker=symbol, issue_type=None, sector=None)
        return TickerInfoFetch(ticker=symbol, snapshot=empty, degraded=False)
    except _DEGRADABLE as exc:
        _logger.warning("alfa ticker info: /info for %s degraded (%s)", symbol, type(exc).__name__)
        return TickerInfoFetch(ticker=symbol, snapshot=None, degraded=True)
    try:
        return TickerInfoFetch(ticker=symbol, snapshot=parse_ticker_info(payload, symbol), degraded=False)
    except ValueError as exc:
        _logger.warning("alfa ticker info: unexpected /info payload for %s (%s)", symbol, exc)
        return TickerInfoFetch(ticker=symbol, snapshot=None, degraded=True)


def upsert_ticker_infos(
    engine: Engine, snapshots: Iterable[TickerInfoSnapshot], *, fetched_at: datetime,
) -> int:
    written = 0
    with Session(engine) as session:
        for snapshot in snapshots:
            session.merge(
                AlfaTickerInfo(
                    ticker=snapshot.ticker,
                    issue_type=snapshot.issue_type,
                    sector=snapshot.sector,
                    fetched_at=_as_utc(fetched_at),
                ),
            )
            written += 1
        session.commit()
    return written


# The engines whose info table is known to exist. A set, not one slot: the web app and the
# refresher hold different Engine objects for the same database (review RB-05).
_tables_ready_for: WeakSet[Engine] = WeakSet()


def _ready(engine: Engine) -> None:
    """Create ``alfa_ticker_info`` once per engine, so a fresh database reads empty (RB-04)."""
    if engine not in _tables_ready_for:
        ensure_ticker_info_tables(engine)
        _tables_ready_for.add(engine)


def read_ticker_infos(engine: Engine, tickers: Iterable[str]) -> dict[str, TickerInfoView]:
    wanted = {_symbol(t) for t in tickers if t and t.strip()}
    if not wanted:
        return {}
    _ready(engine)
    with Session(engine) as session:
        rows = session.execute(
            select(AlfaTickerInfo).where(AlfaTickerInfo.ticker.in_(wanted)),
        ).scalars().all()
        return {
            row.ticker: TickerInfoView(
                ticker=row.ticker,
                issue_type=row.issue_type,
                sector=row.sector,
                fetched_at=_as_utc(row.fetched_at),
            )
            for row in rows
        }


def tickers_needing_info(engine: Engine, tickers: Iterable[str], *, today: date) -> list[str]:
    """Tickers, in the given order, with no row or a row fetched before ``today`` (UTC)."""
    ordered = list(dict.fromkeys(_symbol(t) for t in tickers if t and t.strip()))
    known = read_ticker_infos(engine, ordered)
    return [t for t in ordered if t not in known or known[t].fetched_at.date() < today]
