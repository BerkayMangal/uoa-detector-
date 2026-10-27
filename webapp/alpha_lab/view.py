"""Bounded private reporting, with descriptive outcomes and explicit missingness."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab import db
from webapp.alpha_lab.archive import STATE as ARCHIVE_STATE
from webapp.alpha_lab.engine import STATE
from webapp.alpha_lab.settings import load_settings
from webapp.board.db import session_factory


@dataclass(frozen=True)
class Group:
    version: str
    arm: str
    horizon: int
    cohorts: int
    priced: int
    net_pct: float | None
    peer_excess: float | None
    spy_excess: float | None


@dataclass(frozen=True)
class LabView:
    captured: int
    polls: int
    cohorts: int
    latest: dict[str, Any]  # JSON persistence boundary
    observations: tuple[dict[str, Any], ...]
    groups: tuple[Group, ...]
    errors: dict[str, object]
    news_failures: tuple[dict[str, Any], ...]


def build(engine: Engine) -> LabView:
    cfg = load_settings()
    db.ensure(engine)
    with session_factory(engine)() as s:
        captured = s.scalar(select(func.count()).select_from(db.Alert)) or 0
        polls = s.scalar(select(func.count()).select_from(db.Poll)) or 0
        count = s.scalar(select(func.count()).select_from(db.Cohort)) or 0
        cycle = s.scalar(select(db.Cycle).order_by(db.Cycle.observed_at.desc()).limit(1))
        latest = json.loads(cycle.payload_json) if cycle else {}
        # Latest audit PER ticker, not merely the first N rows of one giant scan.
        newest = (
            select(db.Audit.ticker, func.max(db.Audit.observed_at).label("at"))
            .group_by(db.Audit.ticker)
            .subquery()
        )
        query = select(db.Audit).join(
            newest, (db.Audit.ticker == newest.c.ticker) & (db.Audit.observed_at == newest.c.at)
        )
        records = list(s.scalars(query.order_by(db.Audit.ticker).limit(cfg.display_limit)))
        observations = tuple(json.loads(r.payload_json) for r in records)
        cohort_counts = Counter(
            {
                (v, arm): n
                for v, arm, n in s.execute(
                    select(db.Cohort.version, db.Cohort.arm, func.count()).group_by(
                        db.Cohort.version, db.Cohort.arm
                    )
                )
            }
        )
        outcomes = s.execute(
            select(
                db.Outcome.version,
                db.Outcome.arm,
                db.Outcome.horizon,
                func.count(),
                func.avg(db.Outcome.stock_net_pct),
                func.avg(db.Outcome.peer_excess_pp),
                func.avg(db.Outcome.spy_excess_pp),
            ).group_by(db.Outcome.version, db.Outcome.arm, db.Outcome.horizon)
        ).all()
    sums = {(r[0], r[1], r[2]): (r[3], r[4], r[5], r[6]) for r in outcomes}
    groups = []
    for (version, arm), total in sorted(cohort_counts.items()):
        for horizon in (cfg.primary_sessions, *cfg.secondary_sessions):
            n, net, peer, spy = sums.get((version, arm, horizon), (0, None, None, None))
            groups.append(Group(version, arm, horizon, total, n, net, peer, spy))
    failures = tuple(
        r for r in observations if r["news"]["state"] in ("failed", "not_checked", "stale")
    )
    return LabView(
        captured,
        polls,
        count,
        latest,
        observations,
        tuple(groups),
        {"capture": dict(ARCHIVE_STATE), "job": dict(STATE)},
        failures,
    )
