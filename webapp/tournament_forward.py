"""Forward runtime v2: immutable first daily roster, archived strategy math unchanged."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import Date, DateTime, String, Table, Text, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.alpha_lab.db import append, digest, dumps
from webapp.alpha_lab.flow import utc
from webapp.board.db import AlfaBase, session_factory
from webapp.calibration.model import Bar, horizon_days
from webapp.calibration.settings import calendar
from webapp.tournament import context, report, storage
from webapp.tournament.engine import positive
from webapp.tournament.model import Config, Prepared, mask, prepare
from webapp.tournament.monitor import STATE, observed_bars
from webapp.tournament.settings import settings

VERSION = "tournament_forward_v2"
_LOCK = threading.Lock()
_logger = logging.getLogger(__name__)


class AlfaTournamentCohort(AlfaBase):
    __tablename__ = "alfa_tournament_cohort"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    config_id: Mapped[str] = mapped_column(String, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


def ensure(engine: Engine) -> None:
    storage.ensure(engine)
    cast(Table, AlfaTournamentCohort.__table__).create(engine, checkfirst=True)


def runtime_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def publication_time(asof: datetime, clock: Callable[[], datetime] | None = None) -> datetime:
    """Availability cannot precede the input snapshot or the physical write clock."""
    return max(utc(asof), utc(clock() if clock else datetime.now(UTC)))


def freeze_roster(
    engine: Engine,
    p: Prepared,
    c: Config,
    i: int,
    now: datetime,
    report_hash: str,
    legacy: dict[str, Any],
    publication_clock: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], ...]:
    """First database writer chooses; later eligible names cannot add slots."""
    identity = digest((report_hash, c.id, p.days[i]))
    with session_factory(engine)() as session:
        row = session.get(AlfaTournamentCohort, identity)
    if row is None:
        dates = horizon_days(p.days[i], c.horizon)
        if dates is None:
            return ()
        entry, end = dates
        existing = [
            a
            for a in legacy.values()
            if a.get("config_id") == c.id and str(a.get("day")) == str(p.days[i])
        ]
        blocked = len(existing) > c.slots
        members: list[dict[str, Any]] = []
        if existing:
            members = (
                []
                if blocked
                else [
                    a | {"runtime_version": VERSION, "runtime_hash": runtime_hash()}
                    for a in existing
                ]
            )
        else:
            candidates = np.flatnonzero(mask(p, c)[i])
            rank = p.scores[c.family, c.lookback][i, candidates]
            for j0 in candidates[np.argsort(-rank, kind="stable")][: c.slots]:
                j = int(j0)
                ticker = p.tickers[j]
                members.append(
                    {
                        "config_id": c.id,
                        "config": asdict(c),
                        "family": c.family,
                        "ticker": ticker,
                        "day": p.days[i],
                        "score": float(p.scores[c.family, c.lookback][i, j]),
                        "atr": float(p.atr[i, j]),
                        "close": float(p.close[i, j]),
                        "entry": entry,
                        "exit": end,
                        "context": context.capture(engine, ticker, p.days[i], now),
                        "report_hash": report_hash,
                        "scope": "independent_paper_units",
                        "runtime_version": VERSION,
                        "runtime_hash": runtime_hash(),
                    }
                )
        if not members and not blocked:
            return ()
        payload = {
            "members": members,
            "blocked_legacy_overcap": blocked,
            "runtime_version": VERSION,
            "runtime_hash": runtime_hash(),
            "report_hash": report_hash,
        }
        available = publication_time(now, publication_clock)
        opens = datetime.combine(
            entry, calendar().regular_open, ZoneInfo(calendar().timezone)
        ).astimezone(UTC)
        if available >= opens:
            return ()
        append(
            engine,
            cast(Table, AlfaTournamentCohort.__table__),
            [
                dict(
                    key=identity,
                    config_id=c.id,
                    day=p.days[i],
                    observed_at=available,
                    payload_json=dumps(payload),
                )
            ],
        )
        with session_factory(engine)() as session:
            row = session.get(AlfaTournamentCohort, identity)
    if row is None or utc(row.observed_at) > publication_time(now, publication_clock):
        return ()
    data: dict[str, Any] = json.loads(row.payload_json)
    return tuple(data["members"])


def price_existing(
    engine: Engine, now: datetime, frozen: dict[str, Any], panel: dict[tuple[str, date], Bar]
) -> None:
    """Same exact-session pricing/cost rules as v1; never changes a decision."""
    s, cal = settings(), calendar()
    with session_factory(engine)() as session:
        decisions = list(
            session.scalars(
                select(storage.TournamentDecision).where(
                    storage.TournamentDecision.observed_at <= now
                )
            )
        )
        known = {
            r.key: json.loads(r.payload_json)
            for r in session.scalars(
                select(storage.TournamentResult).where(storage.TournamentResult.observed_at <= now)
            )
        }
    slip = s.slippage_bps / 10000
    for d in decisions:
        data = json.loads(d.payload_json)
        if data.get("report_hash") != frozen["report_hash"]:
            continue
        start, end = date.fromisoformat(data["entry"]), date.fromisoformat(data["exit"])
        ek, ck, uk = (digest((d.key, x)) for x in ("entry", "closed", "unfilled"))
        if ck in known or uk in known:
            continue
        opening = panel.get((d.ticker, start))
        entry_record = known.get(ek)
        if entry_record is None and opening is not None and positive(opening.open):
            opens = datetime.combine(start, cal.regular_open, ZoneInfo(cal.timezone)).astimezone(
                UTC
            )
            if utc(d.observed_at) >= opens:
                continue
            price = opening.open * (1 + slip)
            qty = int(s.position_usd / price)
            if data["config"]["sizing"] == "atr_budget":
                atr = data["atr"]
                qty = (
                    min(qty, int(s.risk_usd / (s.risk_atr_multiple * atr)))
                    if isinstance(atr, int | float) and positive(atr)
                    else 0
                )
            if qty <= 0:
                append(
                    engine,
                    cast(Table, storage.TournamentResult.__table__),
                    [
                        dict(
                            key=uk,
                            decision_key=d.key,
                            kind="unfilled",
                            observed_at=now,
                            payload_json=dumps({"reason": "whole quantity exceeds risk/cash unit"}),
                        )
                    ],
                )
                continue
            spy = panel.get(("SPY", start))
            entry_record = {
                "price": price,
                "quantity": qty,
                "entry": start,
                "spy_open": spy.open if spy and positive(spy.open) else None,
            }
            append(
                engine,
                cast(Table, storage.TournamentResult.__table__),
                [
                    dict(
                        key=ek,
                        decision_key=d.key,
                        kind="entry",
                        observed_at=now,
                        payload_json=dumps(entry_record),
                    )
                ],
            )
            # The database's first observation wins if another observer raced.
            with session_factory(engine)() as session:
                saved_entry = session.get(storage.TournamentResult, ek)
                assert saved_entry is not None
                entry_record = json.loads(saved_entry.payload_json)
        closing = panel.get((d.ticker, end))
        if entry_record is None or closing is None or not positive(closing.close):
            continue
        price = closing.close * (1 - slip)
        qty, ep = entry_record["quantity"], entry_record["price"]
        spy = panel.get(("SPY", end))
        excess = (
            (price / ep - spy.close * (1 - slip) / (entry_record["spy_open"] * (1 + slip))) * 100
            if entry_record.get("spy_open") and spy and positive(spy.close)
            else None
        )
        outcome = {
            "exit": end,
            "price": price,
            "quantity": qty,
            "pnl_usd": (price - ep) * qty,
            "spy_excess_pp": excess,
        }
        append(
            engine,
            cast(Table, storage.TournamentResult.__table__),
            [
                dict(
                    key=ck,
                    decision_key=d.key,
                    kind="closed",
                    observed_at=now,
                    payload_json=dumps(outcome),
                )
            ],
        )


def observe(
    engine: Engine, now: datetime, publication_clock: Callable[[], datetime] | None = None
) -> None:
    now = utc(now)
    s, cal, frozen = settings(), calendar(), report.load()
    ensure(engine)
    bars = tuple(b for b in observed_bars(engine, now) if b.ticker in frozen["tickers"])
    panel = {(b.ticker, b.day): b for b in bars}
    latest = max((b.day for b in bars if b.ticker == "SPY" and b.valid), default=None)
    with session_factory(engine)() as session:
        legacy = {
            a.key: json.loads(a.payload_json)
            for a in session.scalars(
                select(storage.TournamentAudit).where(storage.TournamentAudit.observed_at <= now)
            )
        }
    enabled = [w for w in frozen["winners"] if w["paper_enabled"]]
    candidates = 0
    if latest is not None and latest >= s.prospective_start and enabled:
        p = prepare(bars)
        i = p.days.index(latest)
        for winner in enabled:
            c = Config(**winner["config"])
            dates = horizon_days(latest, c.horizon)
            if dates is None:
                continue
            opens = datetime.combine(dates[0], cal.regular_open, ZoneInfo(cal.timezone)).astimezone(
                UTC
            )
            if publication_time(now, publication_clock) >= opens:
                continue
            members = freeze_roster(
                engine, p, c, i, now, frozen["report_hash"], legacy, publication_clock
            )
            candidates += len(members)
            for member in members:
                available = publication_time(now, publication_clock)
                if available >= opens:
                    break
                ticker = member["ticker"]
                key = digest((frozen["report_hash"], c.id, ticker, latest))
                append(
                    engine,
                    cast(Table, storage.TournamentAudit.__table__),
                    [
                        dict(
                            key=key,
                            ticker=ticker,
                            day=latest,
                            observed_at=available,
                            payload_json=dumps(member),
                        )
                    ],
                )
                for arm in member["context"]["arms"]:
                    available = publication_time(now, publication_clock)
                    if available >= opens:
                        break
                    append(
                        engine,
                        cast(Table, storage.TournamentDecision.__table__),
                        [
                            dict(
                                key=digest((key, arm)),
                                ticker=ticker,
                                family=c.family,
                                arm=arm,
                                day=latest,
                                observed_at=available,
                                payload_json=dumps(member),
                            )
                        ],
                    )
    price_existing(engine, now, frozen, panel)
    STATE.update(
        status="ok",
        at=now.isoformat(),
        families=len(enabled),
        candidates=candidates,
        covered=len({b.ticker for b in bars if b.day == latest and b.valid}),
        expected=len(frozen["tickers"]),
        runtime=VERSION,
    )


def _single(engine: Engine, now: datetime) -> None:
    if not _LOCK.acquire(blocking=False):
        return
    try:
        observe(engine, now)
    finally:
        _LOCK.release()


async def run(engine: Engine, now: datetime) -> None:
    previous = STATE.get("checked_at")
    if isinstance(previous, datetime) and utc(now) - previous < timedelta(
        seconds=settings().monitor_seconds
    ):
        return
    STATE["checked_at"] = utc(now)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_single, engine, now), timeout=settings().monitor_timeout_seconds
        )
    except Exception as exc:
        STATE.update(status="failed", at=utc(now).isoformat(), error=type(exc).__name__)
        _logger.exception("tournament forward v2 failed; existing desk preserved")
