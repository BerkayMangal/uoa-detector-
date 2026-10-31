"""Bounded point lookups; no quadratic full-tape scans inside the search."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from webapp.event_calibration.model import Dataset, Price, settings


@dataclass(frozen=True)
class Series:
    points: dict[str, tuple[Price, ...]]
    times: dict[str, tuple[datetime, ...]]
    exact: dict[tuple[str, datetime], Price]

    @classmethod
    def make(cls, data: Dataset) -> Series:
        groups: dict[str, list[Price]] = defaultdict(list)
        for p in data.prices:
            groups[p.ticker].append(p)
        points = {
            ticker: tuple(sorted(rows, key=lambda p: p.at)) for ticker, rows in groups.items()
        }
        return cls(
            points,
            {ticker: tuple(p.at for p in rows) for ticker, rows in points.items()},
            {(p.ticker, p.at): p for p in data.prices},
        )

    def known(self, ticker: str, at: datetime) -> Price | None:
        times = self.times.get(ticker, ())
        lo = bisect_left(times, at - timedelta(seconds=settings().price_max_age_seconds))
        hi = bisect_right(times, at)
        rows = [p for p in self.points.get(ticker, ())[lo:hi] if p.known_at <= at]
        return rows[-1] if rows else None

    def after(self, ticker: str, due: datetime, strict: bool = False) -> Price | None:
        times = self.times.get(ticker, ())
        end = due + timedelta(minutes=settings().mark_tolerance_minutes)
        lo = bisect_right(times, due) if strict else bisect_left(times, due)
        hi = bisect_right(times, end)
        points = [p for p in self.points.get(ticker, ())[lo:hi] if p.known_at <= end]
        return min(points, key=lambda p: (p.known_at, p.at)) if points else None
