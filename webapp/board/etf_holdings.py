"""Focused-ETF holdings for single-bet clusters (Phase 5.2.B6a, contract §6 B6).

Daily job over ``portfolio.focused_etfs``: ``/api/etfs/{etf}/holdings``.

Live shape (probe 2026-09-15):
- ``type`` is ``stock`` or ``cash``; cash rows have a null ticker and can carry a
  negative weight. Only ``type == 'stock'`` rows with a ticker are kept.
- ``weight`` is a percent string (``'22.09'`` is 22.09%).
- ``updated`` is the holdings snapshot date, 2 to 4 days old. It is stored with every
  row so the strip can show it.
- Two share classes of one issuer appear separately (GOOG, GOOGL). They are stored
  as UW reports them; ``webapp/board/portfolio.py`` merges them through
  ``portfolio.share_class_aliases``.

An ETF with more than ``portfolio.max_focused_holdings`` holdings is too broad to
mean "the same bet". It is skipped and its previous rows are removed.

``alfa_etf_holding`` (key etf, ticker, updated) is rebuildable per snapshot: a
successful fetch replaces the ETF's rows. A failed fetch keeps them.

UW errors: NotFound is no data; rate limit, transient and an open breaker mark the ETF
degraded and the job continues; daily limit and auth errors propagate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final, cast

from sqlalchemy import Date, DateTime, Float, String, delete, select
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
    from collections.abc import Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from webapp.board.settings import BoardSettings
    from webapp.board.uw_errors import JsonClient

HOLDINGS_PATH: Final = "/api/etfs/{etf}/holdings"
_STOCK_TYPE: Final = "stock"
_DEGRADED: Final = (
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
    CircuitBreakerOpenError,
)


class AlfaEtfHolding(AlfaBase):
    """One stock holding of a focused ETF at a holdings snapshot date (rebuildable)."""

    __tablename__ = "alfa_etf_holding"

    etf: Mapped[str] = mapped_column(String, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    updated: Mapped[date] = mapped_column(Date, primary_key=True)
    weight_pct: Mapped[float] = mapped_column(Float)
    sector: Mapped[str | None] = mapped_column(String, nullable=True)
    short_name: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_etf_holding_tables(engine: Engine) -> None:
    cast("Table", AlfaEtfHolding.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class EtfHoldingsReport:
    fetched_at: datetime
    stored: tuple[tuple[str, int], ...]  # (etf, holdings stored)
    skipped_broad: tuple[tuple[str, int], ...]  # (etf, holdings count) above the cap
    no_data: tuple[str, ...]
    degraded: tuple[str, ...]
    requests: int


@dataclass(frozen=True)
class HoldingView:
    etf: str
    ticker: str
    updated: date
    weight_pct: float
    sector: str | None
    fetched_at: datetime


async def refresh_etf_holdings(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    etfs: Sequence[str] | None = None,
    settings: BoardSettings,
    now: datetime,
) -> EtfHoldingsReport:
    """Daily job: rebuild the holdings of each focused ETF (default: the profile list)."""
    fetched_at = _as_utc(now)
    cap = settings.portfolio.max_focused_holdings
    targets = list(dict.fromkeys(
        e.strip().upper() for e in (settings.portfolio.focused_etfs if etfs is None else etfs)
        if e.strip()
    ))
    stored: list[tuple[str, int]] = []
    broad: list[tuple[str, int]] = []
    no_data: list[str] = []
    degraded: list[str] = []
    requests = 0
    for etf in targets:
        requests += 1
        try:
            resp = await client.request_json(HOLDINGS_PATH.format(etf=etf))
        except UnusualWhalesDailyLimitError:
            raise
        except UnusualWhalesNotFoundError:
            no_data.append(etf)
            continue
        except _DEGRADED:
            degraded.append(etf)
            continue
        holdings = _parse_holdings(resp.get("data"), etf=etf)
        if not holdings:
            no_data.append(etf)
            continue
        count = len({ticker for ticker, _ in holdings})
        with sessions() as session, session.begin():
            session.execute(delete(AlfaEtfHolding).where(AlfaEtfHolding.etf == etf))
            if count > cap:
                broad.append((etf, count))
                continue
            for (ticker, updated), (weight, sector, name) in holdings.items():
                session.add(AlfaEtfHolding(
                    etf=etf, ticker=ticker, updated=updated, weight_pct=weight, sector=sector,
                    short_name=name, fetched_at=fetched_at,
                ))
        stored.append((etf, count))
    return EtfHoldingsReport(
        fetched_at=fetched_at, stored=tuple(stored), skipped_broad=tuple(broad),
        no_data=tuple(no_data), degraded=tuple(degraded), requests=requests,
    )


# The engine whose holdings table is known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def read_focused_holdings(engine: Engine) -> tuple[HoldingView, ...]:
    """Every stored focused-ETF holding for the render path: one query, and no UW call.

    Creates the table once per engine, so a fresh database renders no cluster
    instead of failing.
    """
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_etf_holding_tables(engine)
        _tables_ready_for[:] = [engine]
    with Session(engine) as session:
        return load_focused_holdings(session)


def load_focused_holdings(session: Session) -> tuple[HoldingView, ...]:
    rows = session.scalars(
        select(AlfaEtfHolding).order_by(AlfaEtfHolding.etf, AlfaEtfHolding.ticker),
    ).all()
    return tuple(
        HoldingView(
            etf=r.etf, ticker=r.ticker, updated=r.updated, weight_pct=r.weight_pct,
            sector=r.sector, fetched_at=_as_utc(r.fetched_at),
        )
        for r in rows
    )


def _parse_holdings(
    raw: object, *, etf: str,
) -> dict[tuple[str, date], tuple[float, str | None, str | None]]:
    """(ticker, updated) -> (summed weight %, sector, short name) for stock rows."""
    out: dict[tuple[str, date], tuple[float, str | None, str | None]] = {}
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").strip().lower() != _STOCK_TYPE:
            continue
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        row_etf = row.get("etf")
        if isinstance(row_etf, str) and row_etf.strip() and row_etf.strip().upper() != etf:
            continue
        weight = _num(row.get("weight"))
        updated = _day(row.get("updated"))
        if weight is None or weight <= 0 or updated is None:
            continue
        key = (ticker.strip().upper(), updated)
        previous = out.get(key)
        sector = _text(row.get("sector"))
        name = _text(row.get("short_name"))
        if previous is None:
            out[key] = (weight, sector, name)
        else:
            out[key] = (previous[0] + weight, previous[1] or sector, previous[2] or name)
    return out


def _num(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(Decimal(str(raw).strip()))
    except (InvalidOperation, ValueError):
        return None
    return value if math.isfinite(value) else None


def _day(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _text(raw: object) -> str | None:
    return raw.strip() or None if isinstance(raw, str) else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
