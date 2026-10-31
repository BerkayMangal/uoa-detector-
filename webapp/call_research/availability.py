"""Conservative first-seen proof for committed legacy daily bars."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import cast

from sqlalchemy import Table, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab.db import append, digest
from webapp.board.db import session_factory
from webapp.calibration.storage import DailyBar
from webapp.call_research.settings import settings
from webapp.call_research.storage import DailyReceipt
from webapp.opening.model import utc


def adopt(
    engine: Engine, names: tuple[str, ...], asof: datetime, clock: Callable[[], datetime]
) -> int:
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(DailyBar)
            .outerjoin(DailyReceipt, DailyReceipt.key == DailyBar.key)
            .where(
                DailyReceipt.key.is_(None), DailyBar.ticker.in_(names), DailyBar.observed_at <= asof
            )
            .limit(settings().query_limit + 1)
        ).all()
        if len(rows) > settings().query_limit:
            raise ValueError("daily availability bound exceeded")
        if not rows:
            return 0
        # The source transaction already committed before this read succeeded.
        seen = max(utc(asof), utc(clock()))
        values = [
            dict(key=r.key, payload_hash=digest(r.payload_json), observed_at=seen) for r in rows
        ]
    return append(engine, cast(Table, DailyReceipt.__table__), values)
