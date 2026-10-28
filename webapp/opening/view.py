"""Read-only journal projection; JSON is limited to the stored-evidence boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select, true
from sqlalchemy.engine import Engine

from webapp.alpha_lab.db import digest
from webapp.board.db import session_factory
from webapp.opening import storage
from webapp.opening.model import Advice, Clock, clock, settings, utc
from webapp.opening.policy import fresh


@dataclass(frozen=True)
class Statement:
    key: str
    advice: Advice
    at: datetime
    scan_id: str
    current: bool


@dataclass(frozen=True)
class Checkpoint:
    label: str
    status: str
    at: datetime | None
    tickers: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    ticker: str
    at: datetime
    instrument: str
    marks: tuple[tuple[str, dict[str, Any] | None], ...]
    key: str = ""
    day: date | None = None
    profile_hash: str = ""


@dataclass(frozen=True)
class View:
    clock: Clock
    now: datetime
    recorded: bool
    latest_at: datetime | None
    current: bool
    statements: tuple[Statement, ...]
    history: tuple[Statement, ...]
    checkpoints: tuple[Checkpoint, ...]
    results: tuple[Result, ...]
    count: int
    before: str
    history_day: date

    @property
    def display_limit(self) -> int:
        return settings().display_limit


def build(engine: Engine, now: datetime, before: str = "", journal_day: date | None = None) -> View:
    cfg, c = settings(), clock(now)
    history_day = journal_day or c.day
    empty = View(c, now, False, None, False, (), (), (), (), 0, "", history_day)
    if not storage.available(engine):
        return empty
    with session_factory(engine)() as s:
        latest = s.scalar(
            select(storage.OpeningCycle)
            .where(storage.OpeningCycle.day == c.day, storage.OpeningCycle.published_at <= now)
            .order_by(storage.OpeningCycle.published_at.desc(), storage.OpeningCycle.key.desc())
            .limit(1)
        )
        latest_at = utc(latest.published_at) if latest else None
        source = storage.source(engine, now)
        metadata = json.loads(latest.payload_json) if latest else {}
        current = bool(
            latest_at
            and fresh(latest_at, now, cfg.source_max_age_seconds)
            and metadata.get("profile_hash") == cfg.profile_hash
            and metadata.get("phase") == c.phase
            and source
            and latest
            and source.scan_id == latest.scan_id
            and fresh(utc(source.started_at), now, cfg.source_max_age_seconds)
        )
        rows = (
            list(
                s.scalars(
                    select(storage.OpeningDecision).where(
                        storage.OpeningDecision.cycle_key == latest.key
                    )
                )
            )
            if latest
            else []
        )

        from webapp.alpha_lab.readers import spots_at
        from webapp.desk.entry_guard import reason as entry_guard

        cache_present = inspect(engine).has_table("alfa_atm")
        spots = spots_at(engine, [r.ticker for r in rows], now) if cache_present else {}
        source_cards = (
            {r.get("ticker"): r for r in source.snapshot.get("cards", []) if isinstance(r, dict)}
            if source
            else {}
        )

        def statement(row: storage.OpeningDecision, scan_id: str, is_latest: bool) -> Statement:
            advice = Advice.model_validate_json(row.payload_json)
            valid = (
                current and is_latest and advice.expires_at is not None and now < advice.expires_at
            )
            if valid and advice.action == "BUY" and cache_present:
                spot = spots.get(advice.ticker)
                raw = source_cards.get(advice.ticker, {})
                valid = not entry_guard(raw, spot, now, True)
                valid = (
                    valid
                    and spot is not None
                    and spot.trade_date == c.day
                    and fresh(spot.fetched_at, now, cfg.source_max_age_seconds)
                )
                if valid and spot and spot.spot is not None and advice.plan:
                    limit = raw.get("stock_plan", {}).get("chase_limit")
                    valid = (
                        (spot.spot > advice.plan.stop and (limit is None or spot.spot <= limit))
                        if advice.direction == "up"
                        else (
                            spot.spot < advice.plan.stop and (limit is None or spot.spot >= limit)
                        )
                    )
            return Statement(row.key, advice, utc(row.published_at), scan_id, bool(valid))

        statements = (
            tuple(
                sorted(
                    (statement(r, latest.scan_id, True) for r in rows),
                    key=lambda item: (
                        item.advice.action == "WAIT",
                        item.advice.action == "BUY",
                        item.advice.ticker,
                    ),
                )
            )
            if latest
            else ()
        )
        history_query = (
            select(storage.OpeningDecision, storage.OpeningCycle.scan_id)
            .join(
                storage.OpeningCycle, storage.OpeningDecision.cycle_key == storage.OpeningCycle.key
            )
            .where(
                storage.OpeningDecision.day == history_day,
                storage.OpeningDecision.published_at <= now,
            )
        )
        if before:
            # An immutable key cursor, including ties in publication time.
            cursor = s.get(storage.OpeningDecision, before)
            if cursor and cursor.day == history_day:
                from sqlalchemy import and_, or_

                history_query = history_query.where(
                    or_(
                        storage.OpeningDecision.published_at < cursor.published_at,
                        and_(
                            storage.OpeningDecision.published_at == cursor.published_at,
                            storage.OpeningDecision.key < cursor.key,
                        ),
                    )
                )
        history_rows = s.execute(
            history_query.order_by(
                storage.OpeningDecision.published_at.desc(), storage.OpeningDecision.key.desc()
            ).limit(cfg.history_limit + 1)
        ).all()
        history = tuple(statement(r, scan, False) for r, scan in history_rows[: cfg.history_limit])
        from sqlalchemy import func

        count = (
            s.scalar(
                select(func.count())
                .select_from(storage.OpeningDecision)
                .where(
                    storage.OpeningDecision.day == history_day,
                    storage.OpeningDecision.published_at <= now,
                )
            )
            or 0
        )
        checkpoints = []
        for kind, label, end in (
            ("preparation", "Açılış öncesi ilk liste", c.opening),
            ("post_open", "+15 ilk liste", c.assessment),
        ):
            primary = s.get(storage.OpeningPrimary, digest((cfg.profile_hash, c.day, kind)))
            if primary and utc(primary.published_at) <= now:
                names = tuple(
                    sorted(
                        set(
                            s.scalars(
                                select(storage.OpeningDecision.ticker).where(
                                    storage.OpeningDecision.cycle_key == primary.cycle_key
                                )
                            )
                        )
                    )
                )
                checkpoints.append(
                    Checkpoint(label, "Donduruldu", utc(primary.published_at), names)
                )
            else:
                from datetime import timedelta

                deadline = (
                    end + timedelta(minutes=cfg.checkpoint_minutes)
                    if end and kind == "post_open"
                    else end
                )
                checkpoints.append(
                    Checkpoint(
                        label,
                        "Kaçırıldı · sonradan doldurulmaz"
                        if deadline and now >= deadline
                        else "Bekleniyor",
                        None,
                        (),
                    )
                )
        primaries = list(
            s.scalars(
                select(storage.OpeningPrimary)
                .where(
                    storage.OpeningPrimary.kind == "entry",
                    storage.OpeningPrimary.published_at <= now,
                    storage.OpeningPrimary.day == history_day if journal_day else true(),
                )
                .order_by(storage.OpeningPrimary.published_at.desc())
                .limit(cfg.history_limit)
            )
        )
        results = []
        labels = [(f"m{m}", f"+{m} dk") for m in cfg.intraday_minutes] + [
            (f"c{d}", "Bugün kapanış" if d == 0 else f"+{d} seans kapanış")
            for d in cfg.close_sessions
        ]
        for primary in primaries:
            decision = s.get(storage.OpeningDecision, primary.decision_key)
            if decision is None:
                continue
            advice = Advice.model_validate_json(decision.payload_json)
            outcomes = {
                r.horizon: json.loads(r.payload_json)
                for r in s.scalars(
                    select(storage.OpeningOutcome).where(
                        storage.OpeningOutcome.primary_key == primary.key,
                        storage.OpeningOutcome.observed_at <= now,
                    )
                )
            }
            cycle = s.get(storage.OpeningCycle, primary.cycle_key)
            profile_hash = (
                json.loads(cycle.payload_json).get("profile_hash", "")
                if cycle and utc(cycle.published_at) <= now
                else ""
            )
            results.append(
                Result(
                    primary.ticker,
                    utc(primary.published_at),
                    advice.instrument,
                    tuple((label, outcomes.get(kind)) for kind, label in labels),
                    key=primary.key,
                    day=primary.day,
                    profile_hash=profile_hash,
                )
            )
        return View(
            c,
            now,
            latest is not None,
            latest_at,
            current,
            statements,
            history,
            tuple(checkpoints),
            tuple(results),
            count,
            history[-1].key if len(history_rows) > cfg.history_limit and history else "",
            history_day,
        )


def tr_time(at: datetime | None) -> str:
    return at.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%H:%M") if at else "—"
