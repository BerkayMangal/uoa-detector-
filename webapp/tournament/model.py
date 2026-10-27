"""Causal price-volume features, stored once and shared across the fixed grid."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from itertools import product

import numpy as np
from numpy.typing import NDArray

from uoa_detector.live_alpha.calendar import is_session_day
from webapp.alpha_lab.db import digest
from webapp.calibration.model import Bar
from webapp.calibration.settings import calendar
from webapp.tournament.settings import settings

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]


@dataclass(frozen=True)
class Config:
    family: str
    lookback: int
    threshold: float
    volume: float
    regime: str
    horizon: int
    sizing: str
    slots: int

    @property
    def id(self) -> str:
        return digest(asdict(self))[:16]

    @property
    def mask_key(self) -> tuple[str, int, float, float, str]:
        return self.family, self.lookback, self.threshold, self.volume, self.regime


@dataclass(frozen=True)
class Prepared:
    days: tuple[date, ...]
    tickers: tuple[str, ...]
    open: FloatArray
    close: FloatArray
    atr: FloatArray
    volume: FloatArray
    valid: BoolArray
    body: FloatArray
    chase: FloatArray
    above: BoolArray
    below: BoolArray
    scores: dict[tuple[str, int], FloatArray]
    conditions: dict[tuple[str, int], BoolArray]


def grid() -> tuple[Config, ...]:
    s = settings()
    return tuple(
        Config(family, *args)
        for family, thresholds in s.thresholds.items()
        for args in product(
            s.lookbacks, thresholds, s.volume_gates, s.regimes, s.horizons, s.sizing_modes, s.slots
        )
    )


def prepare(bars: tuple[Bar, ...]) -> Prepared:
    s, cal = settings(), calendar()
    symbols = tuple(sorted({b.ticker for b in bars}))
    if "SPY" not in symbols:
        raise ValueError("SPY reference is required")
    known = [b.day for b in bars if b.ticker == "SPY"]
    all_days = []
    d = min(known)
    while d <= max(known):
        if is_session_day(d, cal) is True:
            all_days.append(d)
        d += timedelta(days=1)
    days = tuple(all_days)
    di, ti = {d: i for i, d in enumerate(days)}, {t: i for i, t in enumerate(symbols)}
    shape = len(days), len(symbols)
    op, hi, lo, cl, vol = (np.full(shape, np.nan) for _ in range(5))
    valid = np.zeros(shape, dtype=np.bool_)
    seen = set()
    for b in bars:
        if b.day not in di:
            continue
        key = di[b.day], ti[b.ticker]
        if key in seen:
            raise ValueError("conflicting/duplicate source session")
        seen.add(key)
        op[key], hi[key], lo[key], cl[key] = b.open, b.high, b.low, b.close
        vol[key] = b.volume if b.volume is not None else np.nan
        valid[key] = b.valid
    atr, volume, body, chase = (np.full(shape, np.nan) for _ in range(4))
    above = np.zeros(len(days), dtype=np.bool_)
    below = np.zeros(len(days), dtype=np.bool_)
    scores = {(f, lb): np.full(shape, np.nan) for f in s.thresholds for lb in s.lookbacks}
    conditions = {
        (f, lb): np.zeros(shape, dtype=np.bool_) for f in s.thresholds for lb in s.lookbacks
    }
    tr = np.full(shape, np.nan)
    tr[1:] = np.maximum(hi[1:] - lo[1:], np.maximum(abs(hi[1:] - cl[:-1]), abs(lo[1:] - cl[:-1])))
    spy = ti["SPY"]
    warmup = max(
        max(s.lookbacks), s.compression_slow + 1, s.atr_sessions + 1, s.volume_sessions, s.market_ma
    )
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for i in range(warmup, len(days)):
            atr[i] = np.mean(tr[i - s.atr_sessions : i], axis=0)
            prior_volume = vol[i - s.volume_sessions : i]
            volume[i] = vol[i] / np.mean(prior_volume, axis=0)
            volume[i, ~np.all(np.isfinite(prior_volume) & (prior_volume > 0), axis=0)] = np.nan
            volume[i, (vol[i] < 0) | ~np.isfinite(vol[i])] = np.nan
            body[i] = (cl[i] - op[i]) / atr[i]
            chase[i] = (cl[i] - cl[i - 1]) / atr[i]
            market_average = np.mean(cl[i - s.market_ma + 1 : i + 1, spy])
            above[i] = cl[i, spy] > market_average
            below[i] = cl[i, spy] < market_average
            compression = np.mean(tr[i - s.compression_fast : i], axis=0) / np.mean(
                tr[i - s.compression_slow : i], axis=0
            )
            for lb in s.lookbacks:
                window = max(lb, s.compression_slow + 1, s.atr_sessions + 1, s.volume_sessions)
                ok = (
                    np.all(valid[i - window : i + 1], axis=0)
                    & np.all(valid[i - window : i + 1, spy])
                    & np.isfinite(atr[i])
                    & (atr[i] > 0)
                )
                own = (cl[i] / cl[i - lb] - 1) * 100
                relative = own - own[spy]
                high = np.max(hi[i - lb : i], axis=0)
                specs = {
                    "relative_recovery": (
                        -relative,
                        (body[i] >= 0) & (chase[i] <= s.recovery_chase_atr),
                    ),
                    "relative_momentum": (
                        relative,
                        (body[i] >= 0) & (chase[i] <= s.recovery_chase_atr),
                    ),
                    "trend_pullback": (
                        (cl[i - 1] - cl[i]) / atr[i],
                        cl[i] > np.mean(cl[i - lb : i], axis=0),
                    ),
                    "oversold_reversal": ((cl[i - lb] - cl[i]) / atr[i], body[i] >= 0),
                    "range_breakout": ((cl[i] - high) / atr[i], chase[i] <= s.breakout_chase_atr),
                    "volatility_compression": (-compression, cl[i] > high),
                }
                for family, (score, gate) in specs.items():
                    scores[family, lb][i] = score
                    conditions[family, lb][i] = ok & gate & np.isfinite(score)
                    conditions[family, lb][i, spy] = False
    return Prepared(
        days, symbols, op, cl, atr, volume, valid, body, chase, above, below, scores, conditions
    )


def mask(p: Prepared, c: Config) -> BoolArray:
    score = p.scores[c.family, c.lookback]
    threshold = -c.threshold if c.family == "volatility_compression" else c.threshold
    result = p.conditions[c.family, c.lookback] & (score >= threshold)
    if c.volume > 0:
        result = result & (p.volume >= c.volume)
    if c.regime != "any":
        result = result & (p.above[:, None] if c.regime == "above_ma20" else p.below[:, None])
    return result
