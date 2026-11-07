"""Prefix-only daily features; no Study G or post-cutoff source is admitted."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from uoa_detector.live_alpha.calendar import is_session_day
from webapp.calibration.model import Bar
from webapp.calibration.settings import calendar
from webapp.fusion_research.model import Candidate
from webapp.fusion_research.settings import settings

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]
IntArray = NDArray[np.int64]


@dataclass(frozen=True)
class Prepared:
    days: tuple[date, ...]
    tickers: tuple[str, ...]
    open: FloatArray
    close: FloatArray
    atr: FloatArray
    volume: FloatArray
    valid: BoolArray
    above: BoolArray
    below: BoolArray
    scores: dict[tuple[str, int, str], FloatArray]
    conditions: dict[tuple[str, int, str], BoolArray]
    ranks: dict[tuple[str, int, str], IntArray]


def source_path(path: Path) -> Path:
    absolute = path.absolute()
    if any("study_g" in part.casefold().replace("-", "_") for part in absolute.parts):
        raise ValueError("reserved source path")
    resolved = absolute.resolve(strict=True)
    if resolved != absolute or not resolved.is_file():
        raise ValueError("source redirects or is not a file")
    return resolved


def load(path: Path) -> tuple[Bar, ...]:
    cfg = settings()
    rows = []
    seen: set[tuple[str, date]] = set()
    with source_path(path).open() as stream:
        for raw in csv.DictReader(stream):
            day = date.fromisoformat(raw["day"])
            # Check the protected date boundary before parsing numeric labels.
            if not cfg.source_start <= day <= cfg.source_end:
                raise ValueError("source outside registered reused window")
            key = raw["ticker"], day
            if key in seen:
                raise ValueError("duplicate source day")
            seen.add(key)
            rows.append(
                Bar(
                    key[0],
                    day,
                    float(raw["open"]),
                    float(raw["high"]),
                    float(raw["low"]),
                    float(raw["close"]),
                    float(raw["volume"]) if raw.get("volume") else None,
                )
            )
    return tuple(rows)


def prepare(bars: tuple[Bar, ...]) -> Prepared:
    cfg, cal = settings(), calendar()
    if not bars or any(not cfg.source_start <= b.day <= cfg.source_end for b in bars):
        raise ValueError("empty or unregistered source window")
    tickers = tuple(sorted({b.ticker for b in bars}))
    if "SPY" not in tickers:
        raise ValueError("SPY reference required")
    spy_days = [b.day for b in bars if b.ticker == "SPY"]
    days_list = []
    day = min(spy_days)
    while day <= max(spy_days):
        if is_session_day(day, cal) is True:
            days_list.append(day)
        day += timedelta(days=1)
    days = tuple(days_list)
    ti, di = {v: i for i, v in enumerate(tickers)}, {v: i for i, v in enumerate(days)}
    shape = len(days), len(tickers)
    op, hi, lo, cl, vol = (np.full(shape, np.nan, dtype=np.float64) for _ in range(5))
    valid = np.zeros(shape, dtype=np.bool_)
    seen: set[tuple[int, int]] = set()
    for bar in bars:
        if bar.day not in di:
            continue
        key = di[bar.day], ti[bar.ticker]
        if key in seen:
            raise ValueError("duplicate source identity")
        seen.add(key)
        op[key], hi[key], lo[key], cl[key] = bar.open, bar.high, bar.low, bar.close
        vol[key] = bar.volume if bar.volume is not None else np.nan
        valid[key] = bar.valid
    atr, volume, body, chase = (np.full(shape, np.nan, dtype=np.float64) for _ in range(4))
    above, below = np.zeros(len(days), dtype=np.bool_), np.zeros(len(days), dtype=np.bool_)
    scores: dict[tuple[str, int, str], FloatArray] = {
        (f, lb, d): np.full(shape, np.nan, dtype=np.float64)
        for f in cfg.families
        for lb in cfg.lookbacks
        for d in cfg.directions
    }
    conditions: dict[tuple[str, int, str], BoolArray] = {
        key: np.zeros(shape, dtype=np.bool_) for key in scores
    }
    tr = np.full(shape, np.nan, dtype=np.float64)
    tr[1:] = np.maximum(hi[1:] - lo[1:], np.maximum(abs(hi[1:] - cl[:-1]), abs(lo[1:] - cl[:-1])))
    spy = ti["SPY"]
    warmup = max(
        max(cfg.lookbacks),
        cfg.compression_slow_sessions + 1,
        cfg.atr_sessions + 1,
        cfg.volume_sessions,
        cfg.market_ma_sessions,
    )
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for i in range(warmup, len(days)):
            atr[i] = np.mean(tr[i - cfg.atr_sessions : i], axis=0)
            pv = vol[i - cfg.volume_sessions : i]
            volume[i] = vol[i] / np.mean(pv, axis=0)
            volume[i, ~np.all(np.isfinite(pv) & (pv > 0), axis=0)] = np.nan
            volume[i, (vol[i] < 0) | ~np.isfinite(vol[i])] = np.nan
            body[i], chase[i] = (cl[i] - op[i]) / atr[i], (cl[i] - cl[i - 1]) / atr[i]
            ma = np.mean(cl[i - cfg.market_ma_sessions + 1 : i + 1, spy])
            above[i], below[i] = cl[i, spy] > ma, cl[i, spy] < ma
            compression = np.mean(tr[i - cfg.compression_fast_sessions : i], axis=0) / np.mean(
                tr[i - cfg.compression_slow_sessions : i], axis=0
            )
            for lb in cfg.lookbacks:
                window = max(
                    lb,
                    cfg.compression_slow_sessions + 1,
                    cfg.atr_sessions + 1,
                    cfg.volume_sessions,
                    cfg.market_ma_sessions,
                )
                ok = (
                    np.all(valid[i - window : i + 1], axis=0)
                    & np.all(valid[i - window : i + 1, spy])
                    & np.isfinite(atr[i])
                    & (atr[i] > 0)
                )
                own = (cl[i] / cl[i - lb] - 1) * 100
                relative = own - own[spy]
                high, low = np.max(hi[i - lb : i], axis=0), np.min(lo[i - lb : i], axis=0)
                trend = np.mean(cl[i - lb : i], axis=0)
                for direction in cfg.directions:
                    sign = 1 if direction == "up" else -1
                    specs = {
                        "relative_recovery": (
                            -sign * relative,
                            (sign * body[i] >= 0) & (sign * chase[i] <= cfg.recovery_chase_atr),
                        ),
                        "relative_momentum": (
                            sign * relative,
                            (sign * body[i] >= 0) & (sign * chase[i] <= cfg.recovery_chase_atr),
                        ),
                        "trend_pullback": (
                            -sign * (cl[i] - cl[i - 1]) / atr[i],
                            sign * (cl[i] - trend) > 0,
                        ),
                        "oversold_reversal": (
                            -sign * (cl[i] - cl[i - lb]) / atr[i],
                            sign * body[i] >= 0,
                        ),
                        "range_breakout": (
                            sign * (cl[i] - (high if direction == "up" else low)) / atr[i],
                            sign * chase[i] <= cfg.breakout_chase_atr,
                        ),
                        "volatility_compression": (
                            -compression,
                            sign * (cl[i] - (high if direction == "up" else low)) > 0,
                        ),
                    }
                    for family, (score, gate) in specs.items():
                        feature_key = family, lb, direction
                        scores[feature_key][i] = score
                        conditions[feature_key][i] = ok & gate & np.isfinite(score)
                        conditions[feature_key][i, spy] = False
    ranks = {
        key: np.argsort(-np.nan_to_num(value, nan=-np.inf), axis=1, kind="stable")
        for key, value in scores.items()
    }
    return Prepared(
        days, tickers, op, cl, atr, volume, valid, above, below, scores, conditions, ranks
    )


def roster(p: Prepared, c: Candidate) -> tuple[IntArray, IntArray]:
    key = c.family, c.lookback, c.direction
    score = p.scores[key]
    threshold = -c.threshold if c.family == "volatility_compression" else c.threshold
    mask = p.conditions[key] & (score >= threshold)
    if c.volume > 0:
        mask = mask & (p.volume >= c.volume)
    if c.regime != "any":
        mask = mask & (p.above[:, None] if c.regime == "above_ma20" else p.below[:, None])
    ordered = np.take_along_axis(mask, p.ranks[key], axis=1)
    chosen = ordered & (np.cumsum(ordered, axis=1) <= settings().max_new_units_per_day)
    rows, ranks = np.nonzero(chosen)
    return rows, p.ranks[key][rows, ranks]
