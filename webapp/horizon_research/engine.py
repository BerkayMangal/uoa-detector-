"""Fixed sensitivity tensor on reused rosters; JSON is the report adapter boundary.

No selection, physical point-in-time claim, vendor access, portfolio NAV or
executable fill reconstruction. Numeric protocol choices come from model.settings.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from webapp.horizon_research.model import Candle, Signal, Source, code_hash, profile_hash, settings

from uoa_detector.live_alpha.calendar import close_time, is_session_day, next_session_after
from uoa_detector.live_alpha.settings import load_live_alpha_settings

_ET = ZoneInfo("America/New_York")
_MINUTE = timedelta(minutes=1)  # The source candle duration, not a searched parameter.


@cache
def _bounds(day: date) -> tuple[datetime, datetime]:
    cal = load_live_alpha_settings().calendar
    if is_session_day(day, cal) is not True:
        raise ValueError("source contains a non-session or unknown-calendar day")
    return (
        datetime.combine(day, cal.regular_open, _ET).astimezone(UTC),
        datetime.combine(day, close_time(day, cal), _ET).astimezone(UTC),
    )


def _aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("source times must be timezone-aware")
    return at.astimezone(UTC)


def _target(day: date, offset: int) -> date:
    cal = load_live_alpha_settings().calendar
    for _ in range(offset):
        following = next_session_after(day, cal)
        if following is None:
            raise ValueError("target calendar unknown")
        day = following
    return day


@dataclass(frozen=True)
class Gate:
    signal_key: str
    minutes: int
    state: bool | None
    start: datetime | None = None
    end: datetime | None = None
    available_at: datetime | None = None
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unit:
    key: str
    signal_key: str
    arm: str
    ticker: str
    day: date
    signal_at: datetime
    delay_minutes: int
    horizon: str
    cost_bps_per_side: int
    entry_at: datetime | None = None
    exit_at: datetime | None = None
    exit_source_at: datetime | None = None
    exit_kind: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    spy_entry_price: float | None = None
    spy_exit_price: float | None = None
    quantity: int | None = None
    debit: float | None = None
    gross_usd: float | None = None
    cost_usd: float | None = None
    net_usd: float | None = None
    spy_matched_usd: float | None = None
    excess_usd: float | None = None
    return_pct: float | None = None
    gross_underlying_mfe_pct: float | None = None
    gross_underlying_mae_pct: float | None = None
    negative_volume_path_bars: int = 0
    path_missing: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Marks:
    entry: Candle
    exit: Candle
    spy_entry: Candle
    spy_exit: Candle
    exit_at: datetime
    exit_kind: str
    exit_value: float
    spy_exit_value: float


class Tape:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.sessions = tuple(sorted(source.sessions))
        self.end = (
            min(settings().source_end, max(self.sessions))
            if self.sessions
            else settings().source_end
        )
        if len(set(self.sessions)) != len(self.sessions):
            raise ValueError("duplicate source sessions")
        if len({s.key for s in source.signals}) != len(source.signals):
            raise ValueError("duplicate signal identities")
        self.points: dict[tuple[str, date, datetime], Candle] = {}
        self.by_day: dict[tuple[str, date], list[Candle]] = {}
        for candle in source.candles:
            at = _aware(candle.at)
            opening, closing = _bounds(candle.day)
            values = (candle.open, candle.high, candle.low, candle.close)
            if (
                at.astimezone(_ET).date() != candle.day
                or at.second
                or at.microsecond
                or not opening <= at < closing
                or any(isinstance(x, bool) or not math.isfinite(x) or x <= 0 for x in values)
                or candle.low > min(candle.open, candle.close)
                or candle.high < max(candle.open, candle.close)
                or candle.low > candle.high
            ):
                raise ValueError("invalid regular-minute OHLC source")
            if (
                candle.day not in self.sessions
                or not settings().source_start <= candle.day <= self.end
            ):
                raise ValueError("candle outside registered source window")
            key = candle.ticker, candle.day, at
            if key in self.points and self.points[key] != candle:
                raise ValueError("conflicting minute source")
            if key not in self.points:
                self.points[key] = candle
                self.by_day.setdefault((candle.ticker, candle.day), []).append(candle)
        for points in self.by_day.values():
            points.sort(key=lambda candle: candle.at)
        for signal in source.signals:
            at = _aware(signal.at)
            opening, closing = _bounds(signal.day)
            if (
                not signal.key
                or signal.arm not in settings().arms
                or not signal.ticker
                or signal.day not in source.evaluation_sessions
                or signal.day not in self.sessions
                or not opening <= at < closing
                or at.astimezone(_ET).date() != signal.day
            ):
                raise ValueError("signal outside its frozen roster/session")
        if len(set(source.evaluation_sessions)) != len(source.evaluation_sessions) or not set(
            source.evaluation_sessions
        ) <= set(self.sessions):
            raise ValueError("invalid evaluation session vector")
        self.entry_cache: dict[tuple[str, int], Candle | None] = {}
        self.marks_cache: dict[tuple[str, int, str], tuple[Marks | None, str]] = {}
        self.path_cache: dict[
            tuple[str, int, str], tuple[float | None, float | None, int, tuple[str, ...]]
        ] = {}

    def entry(self, signal: Signal, delay: int) -> Candle | None:
        key = signal.key, delay
        if key not in self.entry_cache:
            due = signal.at + timedelta(minutes=delay)
            self.entry_cache[key] = next(
                (
                    point
                    for point in self.by_day.get((signal.ticker, signal.day), ())
                    if due < point.at <= due + timedelta(minutes=settings().entry_tolerance_minutes)
                ),
                None,
            )
        return self.entry_cache[key]

    def gate(self, signal: Signal, minutes: int) -> Gate:
        if minutes == 0:
            return Gate(signal.key, minutes, True)
        opening, _ = _bounds(signal.day)
        duration = timedelta(minutes=minutes)
        available = signal.at - timedelta(minutes=settings().assumed_bar_lag_minutes)
        blocks = (available - opening) // duration
        if blocks < 1:
            return Gate(signal.key, minutes, None, missing=("no_completed_initial_bar",))
        end = opening + blocks * duration
        start = end - duration
        points = [
            self.points.get((signal.ticker, signal.day, start + i * _MINUTE))
            for i in range(minutes)
        ]
        if any(point is None for point in points):
            return Gate(
                signal.key,
                minutes,
                None,
                start,
                end,
                end + timedelta(minutes=settings().assumed_bar_lag_minutes),
                ("completed_bar_minute_missing",),
            )
        known = [point for point in points if point is not None]
        return Gate(
            signal.key,
            minutes,
            known[-1].close > known[0].open,
            start,
            end,
            end + timedelta(minutes=settings().assumed_bar_lag_minutes),
        )

    def marks(self, signal: Signal, delay: int, horizon: str) -> tuple[Marks | None, str]:
        key = signal.key, delay, horizon
        if key not in self.marks_cache:
            self.marks_cache[key] = self._marks(signal, delay, horizon)
        return self.marks_cache[key]

    def _marks(self, signal: Signal, delay: int, horizon: str) -> tuple[Marks | None, str]:
        entry = self.entry(signal, delay)
        if entry is None:
            return None, "entry_minute_missing"
        spy_entry = self.points.get(("SPY", signal.day, entry.at))
        if spy_entry is None:
            return None, "exact_entry_spy_missing"
        if horizon.startswith("m"):
            exit_at = entry.at + timedelta(minutes=int(horizon[1:]))
            _, closing = _bounds(signal.day)
            if exit_at >= closing:
                return None, "intraday_endpoint_outside_rth"
            exit_source = exit_at
            exit_day = signal.day
            kind = "minute_open"
        else:
            exit_day = _target(signal.day, int(horizon[1:]))
            if exit_day > self.end:
                return None, "right_censored_source_end"
            if exit_day not in self.sessions:
                return None, "target_session_missing"
            _, exit_at = _bounds(exit_day)
            exit_source = exit_at - _MINUTE
            kind = "final_rth_minute_close"
        exit_point = self.points.get((signal.ticker, exit_day, exit_source))
        spy_exit = self.points.get(("SPY", exit_day, exit_source))
        if exit_point is None:
            return None, "exact_exit_minute_missing"
        if spy_exit is None:
            return None, "exact_exit_spy_missing"
        return Marks(
            entry,
            exit_point,
            spy_entry,
            spy_exit,
            exit_at,
            kind,
            exit_point.open if kind == "minute_open" else exit_point.close,
            spy_exit.open if kind == "minute_open" else spy_exit.close,
        ), ""

    def path(
        self, signal: Signal, delay: int, horizon: str, marks: Marks
    ) -> tuple[float | None, float | None, int, tuple[str, ...]]:
        key = signal.key, delay, horizon
        if key in self.path_cache:
            return self.path_cache[key]
        highs, lows = [marks.entry.open, marks.exit_value], [marks.entry.open, marks.exit_value]
        negative = 0
        result: tuple[float | None, float | None, int, tuple[str, ...]]
        expected_days = []
        cursor = signal.day
        while cursor <= marks.exit.day:
            expected_days.append(cursor)
            if cursor == marks.exit.day:
                break
            cursor = _target(cursor, 1)
        for day in expected_days:
            if day not in self.sessions:
                result = None, None, negative, ("regular_session_path_gap",)
                self.path_cache[key] = result
                return result
            opening, closing = _bounds(day)
            at = max(opening, marks.entry.at)
            end = min(closing, marks.exit_at)
            while at < end:
                candle = self.points.get((signal.ticker, day, at))
                if candle is None:
                    result = None, None, negative, ("regular_minute_path_gap",)
                    self.path_cache[key] = result
                    return result
                highs.append(candle.high)
                lows.append(candle.low)
                negative += candle.volume is not None and candle.volume < 0
                at += _MINUTE
        result = (
            (max(highs) / marks.entry.open - 1) * 100,
            (min(lows) / marks.entry.open - 1) * 100,
            negative,
            (),
        )
        self.path_cache[key] = result
        return result


def _key(signal: Signal, delay: int, horizon: str, cost: int) -> str:
    return hashlib.sha256(json.dumps((signal.key, delay, horizon, cost)).encode()).hexdigest()


def _unit(tape: Tape, signal: Signal, delay: int, horizon: str, cost: int) -> Unit:
    result = Unit(
        _key(signal, delay, horizon, cost),
        signal.key,
        signal.arm,
        signal.ticker,
        signal.day,
        signal.at,
        delay,
        horizon,
        cost,
    )
    marks, error = tape.marks(signal, delay, horizon)
    if marks is None:
        return replace(result, missing=(error,))
    rate = cost / 10000
    entry, exit_value = marks.entry.open * (1 + rate), marks.exit_value * (1 - rate)
    quantity = int(settings().unit_budget_usd // entry)
    if quantity < 1:
        return replace(result, missing=("no_affordable_whole_share",))
    debit = quantity * entry
    net = quantity * (exit_value - entry)
    benchmark = debit * (
        marks.spy_exit_value * (1 - rate) / (marks.spy_entry.open * (1 + rate)) - 1
    )
    gross = quantity * (marks.exit_value - marks.entry.open)
    values = (entry, exit_value, debit, net, benchmark, gross, net - benchmark)
    if not all(math.isfinite(value) for value in values):
        return replace(result, missing=("nonfinite_reference_arithmetic",))
    mfe, mae, negative, missing_path = tape.path(signal, delay, horizon, marks)
    return replace(
        result,
        entry_at=marks.entry.at,
        exit_at=marks.exit_at,
        exit_source_at=marks.exit.at,
        exit_kind=marks.exit_kind,
        entry_price=marks.entry.open,
        exit_price=marks.exit_value,
        spy_entry_price=marks.spy_entry.open,
        spy_exit_price=marks.spy_exit_value,
        quantity=quantity,
        debit=debit,
        gross_usd=gross,
        cost_usd=gross - net,
        net_usd=net,
        spy_matched_usd=benchmark,
        excess_usd=net - benchmark,
        return_pct=net / debit * 100,
        gross_underlying_mfe_pct=mfe,
        gross_underlying_mae_pct=mae,
        negative_volume_path_bars=negative,
        path_missing=missing_path,
    )


def _json(value: Unit | Gate) -> dict[str, Any]:
    # Structured data crosses into the standalone JSON report here only.
    return {
        key: item.isoformat() if isinstance(item, (datetime, date)) else item
        for key, item in asdict(value).items()
    }


def _cell(
    arm: str,
    delay: int,
    minutes: int,
    horizon: str,
    cost: int,
    signals: tuple[Signal, ...],
    gates: dict[tuple[str, int], Gate],
    units: dict[tuple[str, int, str, int], Unit],
) -> dict[str, Any]:
    selected, false_keys, unknown_keys = [], [], []
    for signal in signals:
        state = gates[signal.key, minutes].state
        if state is True:
            selected.append(units[signal.key, delay, horizon, cost])
        elif state is False:
            false_keys.append(signal.key)
        else:
            unknown_keys.append(signal.key)
    resolved = [unit for unit in selected if not unit.missing]
    unknown = len(unknown_keys)
    complete = not unknown and len(resolved) == len(selected)
    reasons = Counter(reason for unit in selected for reason in unit.missing)
    for key in unknown_keys:
        reasons.update(gates[key, minutes].missing)
    net, excess = (
        sum(unit.net_usd or 0 for unit in resolved),
        sum(unit.excess_usd or 0 for unit in resolved),
    )
    return {
        "key": f"{arm}|d{delay}|f{minutes}|{horizon}|c{cost}",
        "arm": arm,
        "delay_minutes": delay,
        "filter_minutes": minutes,
        "horizon": horizon,
        "cost_bps_per_side": cost,
        "roster_count": len(signals),
        "gate_true": len(selected),
        "gate_false": len(false_keys),
        "gate_unknown": unknown,
        "selected": len(selected),
        "resolved": len(resolved),
        "missing": len(selected) - len(resolved),
        "status": "COMPLETE" if complete else "PARTIAL" if resolved else "BLOCKED",
        "strict_net_usd": net if complete else None,
        "strict_excess_usd": excess if complete else None,
        "known_net_usd": net,
        "known_excess_usd": excess,
        "known_totals_are_partial": not complete,
        "missing_reasons": dict(sorted(reasons.items())),
        "unit_keys": [unit.key for unit in selected],
        "false_signal_keys": false_keys,
        "unknown_signal_keys": unknown_keys,
        "mfe_observed": sum(unit.gross_underlying_mfe_pct is not None for unit in resolved),
        "mae_observed": sum(unit.gross_underlying_mae_pct is not None for unit in resolved),
        "negative_volume_overlap_units": sum(
            unit.negative_volume_path_bars > 0 for unit in resolved
        ),
    }


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


def _primary(
    tape: Tape, horizons: tuple[str, ...], units: dict[tuple[str, int, str, int], Unit]
) -> dict[str, Any]:
    cfg = settings()
    max_delay, max_hold = max(cfg.entry_delays_minutes), max(cfg.intraday_horizons_minutes)
    max_offset = max(cfg.session_close_offsets)
    days = tuple(
        day
        for day in sorted(tape.source.evaluation_sessions)
        if _target(day, max_offset) <= tape.end
    )
    source_signals = [signal for signal in tape.source.signals if signal.arm == cfg.primary_arm]
    chosen = [
        signal
        for signal in source_signals
        if signal.day in days
        and signal.at + timedelta(minutes=max_delay + cfg.entry_tolerance_minutes + max_hold)
        < _bounds(signal.day)[1]
    ]
    daily: dict[date, dict[str, float | None]] = {}
    daily_net: dict[date, dict[str, float | None]] = {}
    incomplete: dict[str, list[str]] = {}
    for day in days:
        vector: dict[str, float | None] = {}
        net_vector: dict[str, float | None] = {}
        for horizon in horizons:
            selected = [
                units[s.key, cfg.primary_delay_minutes, horizon, cfg.primary_cost_bps_per_side]
                for s in chosen
                if s.day == day
            ]
            vector[horizon] = (
                None
                if any(unit.missing for unit in selected)
                else sum(unit.excess_usd or 0 for unit in selected)
            )
            net_vector[horizon] = (
                None
                if any(unit.missing for unit in selected)
                else sum(unit.net_usd or 0 for unit in selected)
            )
        daily[day] = vector
        daily_net[day] = net_vector
        if any(value is None for value in vector.values()):
            incomplete[day.isoformat()] = [h for h, value in vector.items() if value is None]
    common = [day for day in days if all(value is not None for value in daily[day].values())]
    vectors: dict[str, list[float]] = {
        h: [float(daily[day][h] or 0) for day in common] for h in horizons
    }
    differences = {
        h: [v - anchor for v, anchor in zip(vector, vectors[cfg.anchor_horizon], strict=True)]
        for h, vector in vectors.items()
    }
    contrasts: list[dict[str, Any]] = [
        {
            "horizon": h,
            "complete_days": len(common),
            "total_net_usd": sum(float(daily_net[day][h] or 0) for day in common)
            if common
            else None,
            "mean_daily_net_usd": mean(float(daily_net[day][h] or 0) for day in common)
            if common
            else None,
            "total_excess_usd": sum(vectors[h]) if common else None,
            "mean_daily_excess_usd": mean(vectors[h]) if common else None,
            "mean_daily_difference_vs_m60_usd": mean(differences[h]) if common else None,
        }
        for h in horizons
    ]
    bootstrap: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "reason": "incomplete_or_empty_administrative_vector",
        "formal_p_value": None,
    }
    if common and len(common) == len(days) and len(days) >= 2 * cfg.bootstrap_block_sessions:
        rng = random.Random(cfg.bootstrap_seed)
        draws: dict[str, list[float]] = {h: [] for h in horizons}
        for _ in range(cfg.bootstrap_draws):
            indices: list[int] = []
            while len(indices) < len(days):
                begin = rng.randrange(len(days))
                indices.extend(
                    (begin + offset) % len(days) for offset in range(cfg.bootstrap_block_sessions)
                )
            indices = indices[: len(days)]
            for h in horizons:
                draws[h].append(mean(differences[h][index] for index in indices))
        centers = {h: mean(differences[h]) for h in horizons}
        max_deviation = [
            max(abs(draws[h][index] - centers[h]) for h in horizons)
            for index in range(cfg.bootstrap_draws)
        ]
        radius = _quantile(max_deviation, 0.95)
        for row in contrasts:
            h = row["horizon"]
            row["pointwise_descriptive_95pct_difference"] = [
                _quantile(draws[h], 0.025),
                _quantile(draws[h], 0.975),
            ]
            row["shared_max_deviation_descriptive_95pct_band"] = (
                [0.0, 0.0]
                if h == cfg.anchor_horizon
                else [
                    centers[h] - radius,
                    centers[h] + radius,
                ]
            )
        bootstrap = {
            "status": "DESCRIPTIVE_ONLY",
            "draws": cfg.bootstrap_draws,
            "block_sessions": cfg.bootstrap_block_sessions,
            "seed": cfg.bootstrap_seed,
            "shared_day_indices": True,
            "formal_p_value": None,
            "claim": "Reused-sample descriptive bands; not validated FWER, win odds or fresh OOS inference.",
        }
    if common and len(common) == len(days) and len(days) < 2 * cfg.bootstrap_block_sessions:
        bootstrap["reason"] = "insufficient_calendar_blocks"
    return {
        "administrative_days": [day.isoformat() for day in days],
        "right_censored_days_excluded": [
            day.isoformat() for day in tape.source.evaluation_sessions if day not in days
        ],
        "full_baseline_roster": len(source_signals),
        "administrative_roster": len(chosen),
        "excluded_signal_keys": [s.key for s in source_signals if s not in chosen],
        "complete_common_days": [day.isoformat() for day in common],
        "incomplete_administrative_days": incomplete,
        "daily_vectors": {day.isoformat(): daily[day] for day in days},
        "daily_net_vectors": {day.isoformat(): daily_net[day] for day in days},
        "contrasts": contrasts,
        "bootstrap": bootstrap,
        "coverage_note": "Paired complete cases only; any internal missing marks remain disclosed and cannot be zero-filled.",
    }


def run(data: Source) -> dict[str, Any]:
    cfg = settings()
    tape = Tape(data)
    horizons = tuple(f"m{h}" for h in cfg.intraday_horizons_minutes) + tuple(
        f"c{offset}" for offset in cfg.session_close_offsets
    )
    gates = {
        (signal.key, minutes): tape.gate(signal, minutes)
        for signal in data.signals
        for minutes in cfg.bar_filters_minutes
    }
    units = {
        (signal.key, delay, horizon, cost): _unit(tape, signal, delay, horizon, cost)
        for signal in data.signals
        for delay in cfg.entry_delays_minutes
        for horizon in horizons
        for cost in cfg.costs_bps_per_side
    }
    rows = [
        _cell(
            arm,
            delay,
            minutes,
            horizon,
            cost,
            tuple(s for s in data.signals if s.arm == arm),
            gates,
            units,
        )
        for arm in cfg.arms
        for delay in cfg.entry_delays_minutes
        for minutes in cfg.bar_filters_minutes
        for horizon in horizons
        for cost in cfg.costs_bps_per_side
    ]
    if len(rows) != cfg.registered_cells:
        raise ValueError("registered tensor size mismatch")
    return {
        "version": cfg.version,
        "classification": "EXPLORATORY_REUSED_VENDOR_EVENT_TIME_ASSUMPTION",
        "profile_hash": profile_hash(),
        "code_hash": code_hash(),
        "source_hash": data.source_hash,
        "original_report_hash": data.original_report_hash,
        "source_packet_hashes": data.packet_hashes,
        "source_quality": dict(data.quality),
        "source_sessions": len(data.sessions),
        "evaluation_sessions": len(data.evaluation_sessions),
        "registered_cells": cfg.registered_cells,
        "assessed_cells": len(rows),
        "repeated_control_cells": len(cfg.arms) * len(cfg.costs_bps_per_side),
        "complete_cells": sum(row["status"] == "COMPLETE" for row in rows),
        "partial_cells": sum(row["status"] == "PARTIAL" for row in rows),
        "blocked_cells": sum(row["status"] == "BLOCKED" for row in rows),
        "deduplicated_outcome_units": len(units),
        "winner": None,
        "validated_alpha": False,
        "rows": rows,
        "units": [_json(unit) for unit in units.values()],
        "gates": [_json(gate) for gate in gates.values()],
        "signal_gates": [_json(gate) for gate in gates.values()],
        "primary_family": _primary(tape, horizons, units),
        "limitations": [
            "Already inspected three-name history; not fresh OOS evidence.",
            "Provider source/event clocks are assumed; physical availability was not reconstructed.",
            "Cross-session corporate actions/dividends unverified: price references, not total returns.",
            "Independent reference units, not a cash portfolio: no account Sharpe or drawdown.",
            "No option P&L, full fundamental valuation or automatic live-policy promotion.",
            "Candle gates apply only to original rosters and never generate replacement signals.",
        ],
    }
