"""Prefix-only features and explicit next-session instructions, with no labels."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date
from functools import cache
from itertools import pairwise, product
from pathlib import Path

from uoa_detector.live_alpha.calendar import is_session_day, next_session_after
from webapp.alpha_lab.db import digest
from webapp.calibration.settings import calendar, settings


@dataclass(frozen=True)
class Bar:
    ticker: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    @property
    def valid(self) -> bool:
        return all(math.isfinite(x) and x > 0 for x in (self.open, self.high, self.low, self.close)) and self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high


@dataclass(frozen=True)
class Config:
    lookback: int
    lag: float
    recovery: float
    volume: float
    regime: str
    chase: float

    @property
    def id(self) -> str:
        return digest((self.lookback, self.lag, self.recovery, self.volume, self.regime, self.chase))[:16]


@dataclass(frozen=True)
class Feature:
    ticker: str
    day: date
    lag: float
    recovery: float
    volume: float | None
    chase: float
    spy_return: float
    spy_above_ma: bool


def grid() -> tuple[Config, ...]:
    s = settings()
    return tuple(Config(*values) for values in product(s.lookbacks, s.min_lag_pp, s.min_recovery_atr, s.min_volume_ratio, s.regimes, s.max_chase_atr))


def load_csv(path: Path) -> tuple[Bar, ...]:
    result = []
    seen = set()
    with path.open() as source:
        for row in csv.DictReader(source):
            key = row["ticker"], row["day"]
            if key in seen:
                raise ValueError(f"duplicate session: {key}")
            seen.add(key)
            result.append(Bar(row["ticker"], date.fromisoformat(row["day"]), float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"]) if row.get("volume") else None))
    return tuple(result)


def features(bars: tuple[Bar, ...], lookback: int) -> tuple[Feature, ...]:
    s, cal = settings(), calendar()
    panel: dict[str, dict[date, Bar]] = {}
    for bar in bars:
        panel.setdefault(bar.ticker, {})[bar.day] = bar
    spy = panel.get("SPY", {})
    days = sorted(spy)
    warmup = max(lookback, s.atr_sessions, s.volume_sessions, s.market_ma_sessions)
    result = []
    # Prefixes are validated against the exchange calendar, not a compressed list
    # that could silently turn a missing day into a longer holding window.
    for i in range(warmup, len(days)):
        prefix = days[i - warmup:i + 1]
        if any(next_session_after(a, cal) != b for a, b in pairwise(prefix)) or is_session_day(days[i], cal) is not True:
            continue
        if any(not spy[d].valid for d in prefix):
            continue
        market = (spy[days[i]].close / spy[days[i - lookback]].close - 1) * 100
        above = spy[days[i]].close > sum(spy[d].close for d in days[i - s.market_ma_sessions + 1:i + 1]) / s.market_ma_sessions
        for ticker, rows in panel.items():
            if ticker == "SPY" or any(d not in rows or not rows[d].valid for d in prefix):
                continue
            prev, cur = rows[days[i - 1]], rows[days[i]]
            tr = [max(rows[days[j]].high - rows[days[j]].low, abs(rows[days[j]].high - rows[days[j-1]].close), abs(rows[days[j]].low - rows[days[j-1]].close)) for j in range(i - s.atr_sessions, i)]
            atr = sum(tr) / len(tr)
            if atr <= 0:
                continue
            volumes = [rows[d].volume for d in days[i-s.volume_sessions:i]]
            volume = None
            if cur.volume is not None and math.isfinite(cur.volume) and cur.volume >= 0 and all(v is not None and math.isfinite(v) and v > 0 for v in volumes):
                volume = cur.volume / (sum(v for v in volumes if v is not None) / len(volumes))
            own = (cur.close / rows[days[i-lookback]].close - 1) * 100
            result.append(Feature(ticker, cur.day, market - own, (cur.close-cur.open)/atr, volume, (cur.close-prev.close)/atr, market, above))
    return tuple(result)


def matches(f: Feature, c: Config) -> bool:
    return f.lag >= c.lag and f.recovery >= c.recovery and (c.volume == 0 or (f.volume is not None and f.volume >= c.volume)) and f.chase <= c.chase and (c.regime == "any" or (c.regime == "positive" and f.spy_return > 0) or (c.regime == "above_ma20" and f.spy_above_ma))


@cache
def horizon_days(signal: date, horizon: int) -> tuple[date, date] | None:
    cal = calendar()
    entry = next_session_after(signal, cal)
    if entry is None:
        return None
    end = entry
    for _ in range(horizon - 1):
        nxt = next_session_after(end, cal)
        if nxt is None:
            return None
        end = nxt
    return entry, end
