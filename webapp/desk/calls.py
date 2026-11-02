"""Read original daily first-entry publications; never infer a current BUY."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from webapp.board.db import session_factory
from webapp.call_research.scorecard import Brief
from webapp.opening import storage
from webapp.opening.model import Advice, clock, utc


def today(engine: Engine, now: datetime) -> tuple[Brief, ...] | None:
    if not storage.available(engine):
        return None
    with session_factory(engine)() as session:
        rows = session.scalars(
            select(storage.OpeningDecision)
            .join(storage.OpeningPrimary, storage.OpeningPrimary.decision_key == storage.OpeningDecision.key)
            .where(
                storage.OpeningPrimary.kind == "entry",
                storage.OpeningPrimary.published_at <= now,
                storage.OpeningPrimary.day == clock(now).day,
                storage.OpeningDecision.day == clock(now).day,
                storage.OpeningDecision.published_at <= now,
                storage.OpeningDecision.action == "BUY",
            )
            .order_by(storage.OpeningDecision.published_at.desc(), storage.OpeningDecision.key.desc())
        ).all()
        return tuple(
            Brief(row.key, row.ticker, row.day, utc(row.published_at), row.action,
                  Advice.model_validate_json(row.payload_json).headline)
            for row in rows
        )
