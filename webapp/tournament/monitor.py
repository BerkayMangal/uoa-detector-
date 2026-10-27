"""Future PAPER instructions and exact settled outcomes, independent unit budgets."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import Table, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab.db import append, digest, dumps
from webapp.alpha_lab.flow import utc
from webapp.board.db import session_factory
from webapp.calibration.model import Bar, horizon_days
from webapp.calibration.settings import calendar
from webapp.calibration.storage import DailyBar
from webapp.tournament import context, report, storage
from webapp.tournament.engine import positive
from webapp.tournament.model import Config, mask, prepare
from webapp.tournament.settings import settings

STATE: dict[str, object] = {}
_LOCK = threading.Lock()
_logger = logging.getLogger(__name__)


def observed_bars(engine: Engine, now: datetime) -> tuple[Bar, ...]:
    # JSON adapter preserves a known open even if a later close is missing.
    from sqlalchemy import inspect

    if not inspect(engine).has_table("alfa_calibration_bar"):
        return ()
    with session_factory(engine)() as session:
        rows = session.scalars(select(DailyBar).where(DailyBar.observed_at <= now))
        result = []
        for row in rows:
            data = json.loads(row.payload_json)
            values = [
                float(data[k]) if data.get(k) is not None else float("nan")
                for k in ("open", "high", "low", "close")
            ]
            result.append(
                Bar(
                    row.ticker,
                    row.day,
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    data.get("volume"),
                )
            )
        return tuple(result)


def observe(engine: Engine, now: datetime) -> None:
    now = utc(now)
    s, cal, frozen = settings(), calendar(), report.load()
    storage.ensure(engine)
    bars = tuple(b for b in observed_bars(engine, now) if b.ticker in frozen["tickers"])
    panel = {(b.ticker, b.day): b for b in bars}
    spy_days = [b.day for b in bars if b.ticker == "SPY" and b.valid]
    latest = max(spy_days, default=None)
    with session_factory(engine)() as session:
        audits = {
            a.key: json.loads(a.payload_json)
            for a in session.scalars(
                select(storage.TournamentAudit).where(storage.TournamentAudit.observed_at <= now)
            )
        }
    candidates = 0
    enabled = [w for w in frozen["winners"] if w["paper_enabled"]]
    if latest is not None and latest >= s.prospective_start and enabled:
        p = prepare(bars)
        index = p.days.index(latest)
        for winner in enabled:
            c = Config(**winner["config"])
            dates = horizon_days(latest, c.horizon)
            if dates is None:
                continue
            entry, end = dates
            opens = datetime.combine(entry, cal.regular_open, ZoneInfo(cal.timezone)).astimezone(
                UTC
            )
            if now >= opens:
                continue
            selected = np.flatnonzero(mask(p, c)[index])
            rank = p.scores[c.family, c.lookback][index, selected]
            for j0 in selected[np.argsort(-rank, kind="stable")][: c.slots]:
                j = int(j0)
                ticker = p.tickers[j]
                candidates += 1
                identity = digest((frozen["report_hash"], c.id, ticker, latest))
                audit = audits.get(identity)
                if audit is None:
                    audit = {
                        "config_id": c.id,
                        "config": asdict(c),
                        "family": c.family,
                        "ticker": ticker,
                        "day": latest,
                        "score": float(p.scores[c.family, c.lookback][index, j]),
                        "atr": float(p.atr[index, j]),
                        "close": float(p.close[index, j]),
                        "entry": entry,
                        "exit": end,
                        "context": context.capture(engine, ticker, latest, now),
                        "report_hash": frozen["report_hash"],
                        "scope": "independent_paper_units",
                    }
                    append(
                        engine,
                        cast(Table, storage.TournamentAudit.__table__),
                        [
                            dict(
                                key=identity,
                                ticker=ticker,
                                day=latest,
                                observed_at=now,
                                payload_json=dumps(audit),
                            )
                        ],
                    )
                    # Re-read the FIRST row; a concurrent observer cannot repair membership.
                    with session_factory(engine)() as session:
                        saved = session.get(storage.TournamentAudit, identity)
                        assert saved is not None
                        audit = json.loads(saved.payload_json)
                for arm in audit["context"]["arms"]:
                    key = digest((identity, arm))
                    append(
                        engine,
                        cast(Table, storage.TournamentDecision.__table__),
                        [
                            dict(
                                key=key,
                                ticker=ticker,
                                family=c.family,
                                arm=arm,
                                day=latest,
                                observed_at=now,
                                payload_json=dumps(audit),
                            )
                        ],
                    )
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
    STATE.update(
        status="ok",
        at=now.isoformat(),
        families=len(enabled),
        candidates=candidates,
        covered=len({b.ticker for b in bars if b.day == latest}),
        expected=len(frozen["tickers"]),
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
        _logger.exception("tournament monitoring failed; existing desk preserved")


def view(engine: Engine, now: datetime) -> dict[str, Any]:
    # Private JSON/template adapter; never calls a vendor or runs the optimizer.
    storage.ensure(engine)
    with session_factory(engine)() as session:
        decisions = list(
            session.scalars(
                select(storage.TournamentDecision)
                .where(storage.TournamentDecision.observed_at <= now)
                .order_by(storage.TournamentDecision.observed_at.desc())
            )
        )
        results = {
            (r.decision_key, r.kind): json.loads(r.payload_json)
            for r in session.scalars(
                select(storage.TournamentResult).where(storage.TournamentResult.observed_at <= now)
            )
        }
        audits = list(
            session.scalars(
                select(storage.TournamentAudit)
                .where(storage.TournamentAudit.observed_at <= now)
                .order_by(storage.TournamentAudit.observed_at.desc())
            )
        )
    rows = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        outcome = results.get((d.key, "closed"))
        state = (
            "closed"
            if outcome
            else "unfilled"
            if (d.key, "unfilled") in results
            else "open"
            if (d.key, "entry") in results
            else "pending"
        )
        rows.append(
            {
                "ticker": d.ticker,
                "family": d.family,
                "arm": d.arm,
                "day": d.day,
                "state": state,
                "result": outcome,
            }
        )
        group = groups.setdefault(
            (d.family, d.arm),
            {
                "family": d.family,
                "arm": d.arm,
                "total": 0,
                "closed": 0,
                "pnl": 0.0,
                "matched": 0,
                "excess_total": 0.0,
            },
        )
        group["total"] += 1
        if outcome:
            group["closed"] += 1
            group["pnl"] += outcome["pnl_usd"]
            if outcome["spy_excess_pp"] is not None:
                group["matched"] += 1
                group["excess_total"] += outcome["spy_excess_pp"]
    frozen = report.load()
    sources = observed_bars(engine, now)
    latest = max((b.day for b in sources if b.ticker == "SPY" and b.valid), default=None)
    return {
        "report": frozen,
        "rows": rows[: settings().display_limit],
        "groups": list(groups.values()),
        "total": len(rows),
        "candidates": [json.loads(a.payload_json) for a in audits[: settings().display_limit]],
        "job": {k: v for k, v in STATE.items() if k != "checked_at"},
        "latest": latest,
        "covered": len(
            {
                b.ticker
                for b in sources
                if b.day == latest and b.valid and b.ticker in frozen["tickers"]
            }
        ),
        "expected": len(frozen["tickers"]),
    }
