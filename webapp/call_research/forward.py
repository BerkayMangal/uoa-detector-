"""First-writer shadow cohorts with a post-commit availability receipt, never orders."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import Table, inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import close_time, next_session_after, previous_session_before
from webapp.alpha_lab.db import append, digest, dumps
from webapp.alpha_lab.research import following_sessions
from webapp.board.db import session_factory
from webapp.calibration.settings import calendar
from webapp.call_research import availability, context, coverage, report
from webapp.call_research.backtest import signals
from webapp.call_research.data import prepare
from webapp.call_research.settings import profile_hash, settings
from webapp.call_research.storage import ResearchCohort, ResearchResult, ensure
from webapp.failure_logging import log_failure
from webapp.opening.model import utc
from webapp.research_readiness import settled_day

STATE: dict[str, object] = {}
_LOCK = threading.Lock()
_LAST: WeakKeyDictionary[Engine, float] = WeakKeyDictionary()
_LOG = logging.getLogger(__name__)


def runtime_hash() -> str:
    root = Path(__file__).parent
    return hashlib.sha256(
        b"".join(
            (root / name).read_bytes()
            for name in ("forward.py", "storage.py", "coverage.py", "context.py", "availability.py")
        )
    ).hexdigest()


def _append(
    engine: Engine,
    model: type[ResearchCohort] | type[ResearchResult],
    key: str,
    at: datetime,
    payload: object,
    *,
    day: date | None = None,
    cohort: str = "",
    kind: str = "",
) -> int:
    body: dict[str, Any] = {"key": key, "observed_at": at, "payload_json": dumps(payload)}
    if day is not None:
        body["day"] = day
    else:
        body.update(cohort_key=cohort, kind=kind)
    return append(engine, cast(Table, model.__table__), [body])


def _records(
    engine: Engine, now: datetime
) -> tuple[list[ResearchCohort], dict[str, ResearchResult]]:
    with session_factory(engine)() as session:
        cohorts = list(
            session.scalars(
                select(ResearchCohort)
                .where(ResearchCohort.observed_at <= now)
                .order_by(ResearchCohort.day)
                .limit(settings().query_limit + 1)
            )
        )
        results = list(
            session.scalars(
                select(ResearchResult)
                .where(ResearchResult.observed_at <= now)
                .limit(settings().query_limit + 1)
            )
        )
    if len(cohorts) > settings().query_limit or len(results) > settings().query_limit:
        raise ValueError("research record bound exceeded")
    return cohorts, {r.key: r for r in results}


def _price(engine: Engine, now: datetime, points: tuple[coverage.Observed, ...]) -> int:
    s = settings()
    cal = calendar()
    panel = {(p.bar.ticker, p.bar.day): p for p in points}
    cohorts, existing = _records(engine, now)
    inserted = 0
    for cohort in cohorts:
        body = json.loads(cohort.payload_json)
        if body.get("profile_hash") != profile_hash():
            continue
        receipt = existing.get(digest((cohort.key, "receipt")))
        if not receipt or not json.loads(receipt.payload_json).get("valid"):
            continue
        deadline = datetime.fromisoformat(body["deadline"])
        if utc(receipt.observed_at) >= deadline:
            continue
        for arm, members in body["members"].items():
            for item in members:
                key = digest((cohort.key, arm, item["ticker"], "outcome"))
                if key in existing:
                    continue
                entry = date.fromisoformat(item["entry_day"])
                dates = (entry, *following_sessions(entry, s.primary_horizon - 1, cal))
                if len(dates) != s.primary_horizon:
                    continue
                expiry = dates[-1]
                due = datetime.combine(
                    expiry, close_time(expiry, cal), ZoneInfo(cal.timezone)
                ).astimezone(UTC)
                if now <= due:
                    continue
                first = panel.get((item["ticker"], entry))
                last = panel.get((item["ticker"], expiry))
                if not first or not last or not first.bar.valid or not last.bar.valid:
                    continue
                slip = s.slippage_bps[0] / 10000
                quantity = int(s.position_usd // (first.bar.open * (1 + slip)))
                if quantity <= 0:
                    continue
                debit = quantity * first.bar.open * (1 + slip)
                credit = quantity * last.bar.close * (1 - slip)
                references: dict[str, float | None] = {}
                for benchmark in s.benchmarks:
                    start, end = panel.get((benchmark, entry)), panel.get((benchmark, expiry))
                    references[benchmark] = (
                        (end.bar.close * (1 - slip) / (start.bar.open * (1 + slip)) - 1) * 100
                        if start and end and start.bar.valid and end.bar.valid
                        else None
                    )
                if any(value is None for value in references.values()):
                    continue
                spy_reference = references[s.benchmarks[0]]
                qqq_reference = references[s.benchmarks[1]]
                own = (credit / debit - 1) * 100
                payload = {
                    "scope": "independent_daily_stock_reference_unit",
                    "arm": arm,
                    "ticker": item["ticker"],
                    "signal_day": item["signal_day"],
                    "entry_day": entry,
                    "exit_day": expiry,
                    "quantity": quantity,
                    "debit": debit,
                    "credit": credit,
                    "pnl_usd": credit - debit,
                    "return_pct": own,
                    "references_pct": references,
                    "spy_excess_pp": own - spy_reference if spy_reference is not None else None,
                    "qqq_excess_pp": own - qqq_reference if qqq_reference is not None else None,
                    "price_available_at": [first.available_at, last.available_at],
                    "assumption": "next open / exact horizon close, not an executed fill",
                }
                inserted += _append(
                    engine, ResearchResult, key, now, payload, cohort=cohort.key, kind="outcome"
                )
    return inserted


def observe(
    engine: Engine, asof: datetime, publication_clock: Callable[[], datetime] | None = None
) -> None:
    s = settings()
    cal = calendar()
    clock = publication_clock or (lambda: datetime.now(UTC))
    now = utc(asof)
    today = now.astimezone(ZoneInfo(cal.timezone)).date()
    if today < s.prospective_start:
        STATE.update(status="before_start", start_day=s.prospective_start.isoformat())
        return
    ensure(engine)
    availability.adopt(engine, coverage.targets(), now, clock)
    points, limited = coverage.observed(engine, now, verified=True)
    if limited:
        STATE.update(status="record_limit")
        return
    contexts = context.capture(engine, points, now)
    priced = _price(engine, now, points)
    day = settled_day(now)
    STATE.update(at=now.isoformat(), contexts=contexts, priced=priced)
    if day is None or day < s.prospective_start:
        STATE.update(status="waiting_first_signal_session")
        return
    required = []
    cursor: date | None = day
    for _ in range(s.persistence_sessions + 1):
        if cursor is None:
            break
        required.append(cursor)
        cursor = previous_session_before(cursor, cal)
    names = (*coverage.prospective_stocks(), *s.benchmarks)
    panel = {(p.bar.ticker, p.bar.day): p for p in points}
    complete = sum(
        all((name, d) in panel and panel[name, d].bar.valid for d in required) for name in names
    )
    STATE.update(covered=complete, expected=len(names), day=day.isoformat())
    # Price old units even when today's ranking universe is incomplete.
    if complete != len(names) or len(required) != s.persistence_sessions + 1:
        STATE.update(status="partial_coverage")
        return
    entry = next_session_after(day, cal)
    if entry is None:
        STATE.update(status="calendar_unknown")
        return
    deadline = datetime.combine(entry, cal.regular_open, ZoneInfo(cal.timezone)).astimezone(UTC)
    key = digest((profile_hash(), day))
    with session_factory(engine)() as session:
        cohort = session.get(ResearchCohort, key)
    if cohort is not None and utc(cohort.observed_at) > now:
        STATE.update(status="future_cohort")
        return
    if cohort is None:
        publish = max(now, utc(clock()))
        if publish >= deadline:
            STATE.update(status="deadline_missed")
            return
        bars = tuple(
            p.bar for p in points if p.bar.valid and p.bar.ticker in names and p.bar.day <= day
        )
        candidates = tuple(
            f
            for f in signals(prepare(bars))
            if f.decision_day == day and f.signal_day >= s.prospective_start
        )
        members = {
            arm: [
                asdict(f)
                for f in sorted(
                    (f for f in candidates if f.arm == arm), key=lambda f: (-f.score, f.ticker)
                )[: s.max_positions]
            ]
            for arm in s.arms
        }
        evidence = [
            {
                "ticker": p.bar.ticker,
                "day": p.bar.day,
                "available_at": p.available_at,
                "open": p.bar.open,
                "high": p.bar.high,
                "low": p.bar.low,
                "close": p.bar.close,
            }
            for p in points
            if p.bar.ticker in names and p.bar.day in required
        ]
        payload = {
            "profile_hash": profile_hash(),
            "report_hash": report.load()["report_hash"],
            "runtime_hash": runtime_hash(),
            "members": members,
            "candidates": [asdict(f) for f in candidates],
            "inputs": evidence,
            "deadline": deadline,
            "measurement_scope": "independent daily units, not a cash portfolio, no trading instruction",
            "membership": "archived 64 stocks + prospective NFLX; SPY/QQQ references",
        }
        _append(engine, ResearchCohort, key, publish, payload, day=day)
    # This timestamp is sampled AFTER the cohort transaction committed. A slow
    # write crossing the opening cannot manufacture a pre-open publication.
    receipt_time = max(now, utc(clock()))
    _append(
        engine,
        ResearchResult,
        digest((key, "receipt")),
        receipt_time,
        {
            "valid": receipt_time < deadline,
            "deadline": deadline,
            "scope": "post_commit_publication_receipt",
        },
        cohort=key,
        kind="receipt",
    )
    with session_factory(engine)() as session:
        receipt = session.get(ResearchResult, digest((key, "receipt")))
        valid = bool(
            receipt
            and json.loads(receipt.payload_json).get("valid")
            and utc(receipt.observed_at) < deadline
        )
    STATE.update(status="cohort_recorded" if valid else "publication_late")


def _guarded(engine: Engine, now: datetime) -> None:
    if not _LOCK.acquire(blocking=False):
        return
    try:
        stamp = time.monotonic()
        if engine in _LAST and stamp - _LAST[engine] < settings().observer_seconds:
            return
        _LAST[engine] = stamp
        observe(engine, now)
    finally:
        _LOCK.release()


async def run(engine: Engine, now: datetime) -> None:
    try:
        if engine.dialect.name == "sqlite":
            _guarded(engine, now)
        else:
            await asyncio.wait_for(
                asyncio.to_thread(_guarded, engine, now), settings().observer_timeout_seconds
            )
    except TimeoutError:
        STATE.update(status="timeout")
    except Exception as exc:
        STATE.update(status="failed", exception=type(exc).__name__)
        log_failure(_LOG, "call-research", exc, "scan")


def view(engine: Engine, now: datetime) -> tuple[dict[str, Any], ...]:
    # Private display-only JSON adapter. No optimizer and no state advancement.
    if not inspect(engine).has_table(ResearchCohort.__tablename__) or not inspect(engine).has_table(
        ResearchResult.__tablename__
    ):
        return ()
    cohorts, results = _records(engine, now)
    rows = []
    for cohort in reversed(cohorts[-settings().display_limit :]):
        body = json.loads(cohort.payload_json)
        if body.get("profile_hash") != profile_hash():
            continue
        receipt = results.get(digest((cohort.key, "receipt")))
        valid = bool(receipt and json.loads(receipt.payload_json).get("valid"))
        for arm, members in body["members"].items():
            outcomes = []
            for member in members:
                result = results.get(digest((cohort.key, arm, member["ticker"], "outcome")))
                if result:
                    outcomes.append(json.loads(result.payload_json))
            rows.append(
                {
                    "day": cohort.day,
                    "arm": arm,
                    "members": len(members),
                    "priced": len(outcomes),
                    "valid_publication": valid,
                    "outcomes": outcomes,
                }
            )
    return tuple(rows)
