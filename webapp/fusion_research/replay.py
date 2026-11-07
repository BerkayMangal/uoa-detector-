"""Paired next-open/daily-close reference units; short mirrors are diagnostic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import cast

import numpy as np

from webapp.fusion_research.features import BoolArray, FloatArray, IntArray, Prepared
from webapp.fusion_research.model import Candidate, digest
from webapp.fusion_research.settings import settings


@dataclass(frozen=True)
class Outcomes:
    quantity: FloatArray
    entry: FloatArray
    exit: FloatArray
    net: FloatArray
    excess: FloatArray
    gross: FloatArray
    cost: FloatArray
    valid: BoolArray
    exits: IntArray


@dataclass(frozen=True)
class Metrics:
    selected: int
    resolved: int
    missing: int
    unfunded: int
    boundary_excluded: int
    days: int
    net_usd: float | None
    excess_usd: float | None
    mean_daily_excess_usd: float | None
    wins: int
    losses: int
    gross_usd: float | None
    price_cost_usd: float | None


@dataclass(frozen=True)
class Unit:
    ticker: str
    signal_day: date
    entry_day: date
    exit_day: date
    direction: str
    quantity: int | None
    entry: float | None
    exit: float | None
    net_usd: float | None
    excess_usd: float | None
    missing: str


@dataclass(frozen=True)
class Replay:
    metrics: Metrics
    daily_excess: FloatArray
    daily_net: FloatArray
    ledger: tuple[Unit, ...]
    ledger_identity: str


def outcomes(
    p: Prepared, direction: str, horizon: int, bps: float, *, atr_sizing: bool = False
) -> Outcomes:
    cfg = settings()
    if direction not in cfg.directions or horizon not in cfg.horizons or not 0 <= bps < 10000:
        raise ValueError("unregistered outcome reference")
    shape = p.open.shape
    entry = np.full(shape, np.nan, dtype=np.float64)
    exit_ = np.full(shape, np.nan, dtype=np.float64)
    spy_entry = np.full(shape, np.nan, dtype=np.float64)
    spy_exit = np.full(shape, np.nan, dtype=np.float64)
    entry[:-1] = p.open[1:]
    exit_[:-horizon] = p.close[horizon:]
    spy = p.tickers.index("SPY")
    spy_entry[:-1] = p.open[1:, spy, None]
    spy_exit[:-horizon] = p.close[horizon:, spy, None]
    sign = 1 if direction == "up" else -1
    slip = bps / 10000
    filled_entry = entry * (1 + sign * slip)
    filled_exit = exit_ * (1 - sign * slip)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        # Mirror sizing uses the same underlying notional cap, not short proceeds.
        quantity = np.floor(cfg.reference_usd / (entry * (1 + slip)))
        if atr_sizing:
            quantity = np.minimum(
                quantity, np.floor(cfg.risk_usd / (cfg.risk_atr_multiple * p.atr))
            )
        gross = sign * (exit_ - entry) * quantity
        cost = slip * (entry + exit_) * quantity
        net = gross - cost
        benchmark = (
            sign * (spy_exit - spy_entry) * quantity * entry / spy_entry
            - slip * (spy_entry + spy_exit) * quantity * entry / spy_entry
        )
        excess = net - benchmark
    source_valid = np.zeros(shape, dtype=np.bool_)
    source_valid[:-horizon] = (
        p.valid[1 : len(p.days) - horizon + 1]
        & p.valid[horizon:]
        & p.valid[1 : len(p.days) - horizon + 1, spy, None]
        & p.valid[horizon:, spy, None]
    )
    valid = source_valid & (
        np.isfinite(entry)
        & np.isfinite(exit_)
        & (entry > 0)
        & (exit_ > 0)
        & np.isfinite(spy_entry)
        & np.isfinite(spy_exit)
        & (spy_entry > 0)
        & (spy_exit > 0)
        & np.isfinite(quantity)
        & (quantity > 0)
        & np.isfinite(net)
        & np.isfinite(excess)
    )
    exits = cast(IntArray, np.arange(len(p.days), dtype=np.int64) + horizon)
    return Outcomes(quantity, filled_entry, filled_exit, net, excess, gross, cost, valid, exits)


def replay(
    p: Prepared,
    c: Candidate,
    selected: tuple[IntArray, IntArray],
    o: Outcomes,
    start: date,
    end: date,
    *,
    ledger: bool = False,
) -> Replay:
    day_index = np.array([start <= d <= end for d in p.days], dtype=np.bool_)
    if not np.any(day_index):
        raise ValueError("empty evaluation window")
    last = int(np.flatnonzero(day_index)[-1])
    rows, cols = selected
    included = day_index[rows]
    inside = included & (o.exits[rows] <= last)
    boundary = int(np.sum(included & ~inside))
    rr, cc = rows[inside], cols[inside]
    valid = o.valid[rr, cc]
    qty = o.quantity[rr, cc]
    unfunded = int(np.sum(np.isfinite(qty) & (qty <= 0)))
    n = int(np.sum(valid))
    missing = len(rr) - n
    daily_net = np.zeros(len(p.days), dtype=np.float64)
    daily_excess = np.zeros(len(p.days), dtype=np.float64)
    if n:
        np.add.at(daily_net, rr[valid], o.net[rr[valid], cc[valid]])
        np.add.at(daily_excess, rr[valid], o.excess[rr[valid], cc[valid]])
    if missing:
        daily_net[np.unique(rr[~valid])] = np.nan
        daily_excess[np.unique(rr[~valid])] = np.nan
    active_days = len(np.unique(rr))
    complete = missing == 0
    net = float(np.sum(o.net[rr[valid], cc[valid]])) if complete else None
    excess = float(np.sum(o.excess[rr[valid], cc[valid]])) if complete else None
    gross = float(np.sum(o.gross[rr[valid], cc[valid]])) if complete else None
    cost = float(np.sum(o.cost[rr[valid], cc[valid]])) if complete else None
    ms = Metrics(
        len(rr),
        n,
        missing,
        unfunded,
        boundary,
        active_days,
        net,
        excess,
        float(np.mean(daily_excess[day_index])) if complete else None,
        int(np.sum(o.net[rr[valid], cc[valid]] > 0)),
        int(np.sum(o.net[rr[valid], cc[valid]] < 0)),
        gross,
        cost,
    )
    units = []
    if ledger:
        for r, j, good in zip(rr, cc, valid, strict=True):
            units.append(
                Unit(
                    p.tickers[j],
                    p.days[r],
                    p.days[r + 1],
                    p.days[o.exits[r]],
                    c.direction,
                    int(o.quantity[r, j]) if np.isfinite(o.quantity[r, j]) else None,
                    float(o.entry[r, j]) if np.isfinite(o.entry[r, j]) else None,
                    float(o.exit[r, j]) if np.isfinite(o.exit[r, j]) else None,
                    float(o.net[r, j]) if good else None,
                    float(o.excess[r, j]) if good else None,
                    "" if good else "price_or_affordability_unknown",
                )
            )
    identity = digest(
        (
            c.direction,
            c.horizon,
            start,
            end,
            [(int(r), int(j)) for r, j in zip(rr, cc, strict=True)],
        )
    )
    return Replay(
        ms,
        daily_excess[day_index],
        daily_net[day_index],
        tuple(units),
        identity,
    )
