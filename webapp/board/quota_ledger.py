"""Atomic Unusual Whales request reservation (Phase 5.24, options PAPER tracker).

The refresher's ``SoftCap`` OBSERVES the key-wide daily count after requests
have been made. That is enough for one loop, but not when two consumers ask at
the same moment: both read "below the cap", both spend, and the cap is crossed
by the sum of what they asked for. This module makes the decision before the
spend and makes it atomic.

**One row per ET date** in ``alfa_uw_quota``: ``reserved`` is the number of
requests the key is known to have spent or promised today.

**Two statements, each atomic on its own** (no read-modify-write in Python):

1. ``raise_floor`` — ``reserved = max(reserved, observed)``. The vendor's own
   count (``last_daily_request_count``) includes requests made by consumers that
   never reserved, e.g. the live worker. Folding it in means a reservation can
   never be granted against a stale, too-low number.
2. ``reserve`` — ``UPDATE ... SET reserved = reserved + n WHERE et_date = d AND
   reserved + n <= cap``. The database evaluates the condition and the increment
   together, so of two concurrent requests that would jointly cross the cap,
   exactly one row update succeeds. ``rowcount == 1`` is the grant.

A refused reservation spends nothing and means "stop for today", not "retry in
a loop". A granted reservation that ends up not spending (the fetch failed
before sending) is NOT refunded: over-counting is the safe direction.

The cap is the board profile's ``refresh.daily_request_soft_cap`` (D8: the
profile, never a literal).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import Date, DateTime, Integer, case, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.engine import Engine


class AlfaUwQuota(AlfaBase):
    """Requests spent or promised on the UW key, per ET date (rebuildable)."""

    __tablename__ = "alfa_uw_quota"

    et_date: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_quota_tables(engine: Engine) -> None:
    """Create ``alfa_uw_quota`` if missing. Never alters or drops anything."""
    cast("Table", AlfaUwQuota.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class Reservation:
    granted: bool
    requested: int
    reserved_after: int
    cap: int


def _ensure_row(engine: Engine, day: date, now: datetime) -> None:
    """Insert the day's row once. A concurrent insert of the same key is fine."""
    with engine.begin() as conn:
        exists = conn.execute(select(AlfaUwQuota.et_date).where(AlfaUwQuota.et_date == day)).first()
        if exists is not None:
            return
    try:
        with engine.begin() as conn:
            conn.execute(insert(AlfaUwQuota).values(et_date=day, reserved=0, updated_at=now))
    except IntegrityError:
        # Another consumer created it between our read and our insert.
        return


def reserved_on(engine: Engine, day: date) -> int:
    with engine.begin() as conn:
        value = conn.execute(
            select(AlfaUwQuota.reserved).where(AlfaUwQuota.et_date == day)
        ).scalar_one_or_none()
    return int(value or 0)


def reserve(
    engine: Engine,
    *,
    day: date,
    n: int,
    cap: int,
    observed: int | None = None,
    now: datetime | None = None,
) -> Reservation:
    """Atomically reserve ``n`` requests for ``day`` if that keeps the day at or below ``cap``."""
    if n <= 0:
        msg = f"n must be positive, got {n!r}"
        raise ValueError(msg)
    moment = now or datetime.now(UTC)
    _ensure_row(engine, day, moment)
    with engine.begin() as conn:
        if observed is not None and observed > 0:
            conn.execute(
                update(AlfaUwQuota)
                .where(AlfaUwQuota.et_date == day)
                .values(
                    reserved=case(
                        (AlfaUwQuota.reserved < observed, observed), else_=AlfaUwQuota.reserved,
                    ),
                    updated_at=moment,
                )
            )
        result = conn.execute(
            update(AlfaUwQuota)
            .where(AlfaUwQuota.et_date == day, AlfaUwQuota.reserved + n <= cap)
            .values(reserved=AlfaUwQuota.reserved + n, updated_at=moment)
        )
        granted = result.rowcount == 1
        after = conn.execute(
            select(AlfaUwQuota.reserved).where(AlfaUwQuota.et_date == day)
        ).scalar_one()
    return Reservation(granted=granted, requested=n, reserved_after=int(after), cap=cap)
