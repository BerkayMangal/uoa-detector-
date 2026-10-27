"""Read-only reporting over stored PAPER positions and frozen peer-lag readings.

JSON decoding is confined to the existing persistence adapter. No decision,
pricing rule, position state or external API is changed by this module.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import inspect

from webapp.board import lag_record
from webapp.board.daily_close import ClosePoint, load_closes_by_ticker
from webapp.live_alpha import store
from webapp.live_alpha.inputs import as_utc

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class PaperResult:
    paper_id: str
    opportunity_id: str
    ticker: str
    instrument: str
    policy: str
    created_at: datetime
    exit_at: datetime | None
    state: str
    category: str
    pnl_usd: float | None
    exit_reason: str


def paper_result(p: store.LivePaper) -> PaperResult:
    plan = json.loads(p.plan_json)
    entered = p.entry_at is not None and p.entry_price is not None and (p.quantity or 0) > 0
    priced = (
        entered and p.exit_at is not None and p.exit_price is not None
        and p.pnl_usd is not None and math.isfinite(p.pnl_usd)
    )
    category = {
        "PAPER_PENDING": "pending",
        "PAPER_OPEN": "open",
        "EXIT_SIGNALLED": "exit_pending",
    }.get(p.state, "unknown")
    if p.state == "CLOSED_SIMULATED":
        if priced:
            category = "closed"
        elif p.entry_at is None and p.entry_price is None and not p.quantity:
            category = "unfilled"
    return PaperResult(
        paper_id=p.paper_id, opportunity_id=p.opportunity_id, ticker=p.ticker,
        instrument=p.instrument, policy=str(plan.get("policy_version") or "bilinmiyor"),
        created_at=as_utc(p.created_at), exit_at=as_utc(p.exit_at) if p.exit_at else None,
        state=p.state, category=category,
        pnl_usd=p.pnl_usd if category == "closed" else None,
        exit_reason=p.exit_reason or "",
    )


@dataclass(frozen=True)
class PaperSummary:
    total: int
    pending: int
    open: int
    exit_pending: int
    closed: int
    unfilled: int
    unknown: int
    pnl_usd: float | None
    average_usd: float | None
    wins: int
    losses: int
    flats: int
    realised_decline_usd: float | None


def summarise(rows: Sequence[PaperResult]) -> PaperSummary:
    """Count every row; only priced closed records enter P&L and its decline."""
    closed = [p for p in rows if p.category == "closed" and p.pnl_usd is not None]
    pnls = [p.pnl_usd for p in closed if p.pnl_usd is not None]
    by_exit: dict[datetime, float] = defaultdict(float)
    for p in closed:
        if p.exit_at is not None and p.pnl_usd is not None:
            by_exit[p.exit_at] += p.pnl_usd
    cumulative = peak = decline = 0.0
    for at in sorted(by_exit):
        cumulative += by_exit[at]
        peak = max(peak, cumulative)
        decline = max(decline, peak - cumulative)
    return PaperSummary(
        total=len(rows), pending=sum(p.category == "pending" for p in rows),
        open=sum(p.category == "open" for p in rows),
        exit_pending=sum(p.category == "exit_pending" for p in rows),
        closed=len(closed), unfilled=sum(p.category == "unfilled" for p in rows),
        unknown=sum(p.category == "unknown" for p in rows),
        pnl_usd=round(sum(pnls), 2) if pnls else None,
        average_usd=round(sum(pnls) / len(pnls), 2) if pnls else None,
        wins=sum(x > 0 for x in pnls), losses=sum(x < 0 for x in pnls), flats=sum(x == 0 for x in pnls),
        realised_decline_usd=round(decline, 2) if pnls else None,
    )


@dataclass(frozen=True)
class PaperGroup:
    policy: str
    instrument: str
    summary: PaperSummary


@dataclass(frozen=True)
class LagSummary:
    version: str
    group: str
    sessions: int
    total: int
    priced: int
    mean_pp: float | None


def summarise_lags(
    records: Sequence[lag_record.LagRecord], closes: Mapping[str, Sequence[ClosePoint]],
) -> tuple[LagSummary, ...]:
    """Descriptive peer-relative readings, not simulated trades or hypothesis tests."""
    out: list[LagSummary] = []
    for version in sorted({r.version for r in records}):
        versioned = [r for r in records if r.version == version]
        groups = (
            ("Tüm ölçümler", versioned),
            ("Geride kalanlar", [r for r in versioned if r.lagging]),
            ("Önde olanlar", [r for r in versioned if r.leading]),
        )
        for name, group in groups:
            for horizon in lag_record.FORWARD_SESSIONS:
                values = [v for r in group if (v := lag_record.forward_result(r, closes, horizon)) is not None]
                out.append(LagSummary(
                    version=version, group=name, sessions=horizon, total=len(group), priced=len(values),
                    mean_pp=round(sum(values) / len(values), 3) if values else None,
                ))
    return tuple(out)


@dataclass(frozen=True)
class ResultsView:
    summary: PaperSummary
    groups: tuple[PaperGroup, ...]
    rows: tuple[PaperResult, ...]
    shown: int
    lag_status: str
    lags: tuple[LagSummary, ...]


def build_results(engine: Engine) -> ResultsView:
    rows = [paper_result(p) for p in store.papers(engine)]
    grouped: dict[tuple[str, str], list[PaperResult]] = defaultdict(list)
    for row in rows:
        grouped[row.policy, row.instrument].append(row)
    groups = tuple(PaperGroup(p, i, summarise(items)) for (p, i), items in sorted(grouped.items()))
    inspector = inspect(engine)
    lag_status = "Akran ölçümü tablosu henüz yok."
    lags: tuple[LagSummary, ...] = ()
    if inspector.has_table("alfa_lag_record"):
        records = lag_record.load_records(engine)
        if not records:
            lag_status = "Henüz kaydedilmiş akran ölçümü yok."
        elif not inspector.has_table("alfa_daily_close"):
            lag_status = "Ölçümler var; kapanış verisi tablosu yok, ileri sonuç bilinmiyor."
        else:
            tickers = sorted({t for r in records for t in (r.ticker, *r.peers)})
            lags = summarise_lags(records, load_closes_by_ticker(engine, tickers))
            lag_status = "Yalnız kaydedilmiş ölçümler ve o günün akranları kullanılır."
    # A display bound only; all metrics and groups above use the complete history.
    shown = min(len(rows), 200)
    return ResultsView(summarise(rows), groups, tuple(rows[:shown]), shown, lag_status, lags)
