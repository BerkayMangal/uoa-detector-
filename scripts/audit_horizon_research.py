"""Independent arithmetic/source audit of the frozen-roster horizon tensor.

No engine pricing, filtering, grouping or resampling functions are imported.
JSON dictionaries are confined to this artifact-adapter script. Optional --source
uses the checked source loader only; all financial calculations are independent.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ruamel.yaml import YAML

if TYPE_CHECKING:
    from webapp.horizon_research.model import Candle, Source

ROOT = Path(__file__).resolve().parents[1]
_ET = ZoneInfo("America/New_York")
_MINUTE = timedelta(minutes=1)


def checksum(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def equal(actual: object, expected: float, reason: str = "arithmetic mismatch") -> None:
    require(
        not isinstance(actual, bool) and isinstance(actual, (float, int)) and math.isfinite(actual),
        reason,
    )
    require(math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-7), reason)


def number(value: object) -> float:
    require(
        not isinstance(value, bool)
        and isinstance(value, (float, int))
        and math.isfinite(value)
        and value > 0,
        "invalid positive raw mark",
    )
    return float(value)


def stamp(value: object) -> datetime:
    require(isinstance(value, str), "missing timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    require(result.tzinfo is not None, "naive timestamp")
    return result.astimezone(UTC)


@cache
def config() -> dict[str, Any]:
    return YAML(typ="safe").load((ROOT / "profiles/horizon_research_v1.yaml").read_text())


@cache
def calendar() -> dict[str, Any]:
    return YAML(typ="safe").load((ROOT / "profiles/live_alpha_v1.yaml").read_text())["calendar"]


def session(day: date) -> bool:
    cal = calendar()
    require(day.year in cal["years"], "calendar year unavailable")
    return day.weekday() < 5 and day not in cal["holidays"]


@cache
def bounds(day: date) -> tuple[datetime, datetime]:
    require(session(day), "non-session source day")
    cal = calendar()
    closing = cal["early_close"] if day in cal["early_closes"] else cal["regular_close"]
    return tuple(
        datetime.fromisoformat(f"{day}T{value}").replace(tzinfo=_ET).astimezone(UTC)
        for value in (cal["regular_open"], closing)
    )  # type: ignore[return-value]


def target(day: date, offset: int) -> date:
    for _ in range(offset):
        day += timedelta(days=1)
        while not session(day):
            day += timedelta(days=1)
    return day


def sessions() -> list[date]:
    cfg = config()
    day, end, result = cfg["source_start"], cfg["source_end"], []
    while day <= end:
        if session(day):
            result.append(day)
        day += timedelta(days=1)
    require(len(result) == cfg["source_sessions"], "registered source session count")
    return result


def _signals(original: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cfg, result = config(), {}
    for arm in cfg["arms"]:
        rows = [
            r
            for r in original["rows"]
            if r["arm"] == arm and r["bps_per_side"] == cfg["primary_cost_bps_per_side"]
        ]
        require(len(rows) == 1, "original arm ambiguity")
        daily: Counter[date] = Counter()
        for unit in rows[0]["ledger"]:
            day, at = date.fromisoformat(unit["day"]), stamp(unit["signal_at"])
            key = checksum((original["report_hash"], arm, unit["ticker"], day, at))
            require(key not in result, "duplicate original signal")
            require(
                day in sessions()[cfg["warmup_sessions"] :] and at.astimezone(_ET).date() == day,
                "signal session mismatch",
            )
            require(bounds(day)[0] <= at < bounds(day)[1], "signal outside RTH")
            require(
                not any(
                    s["arm"] == arm and s["ticker"] == unit["ticker"] and s["day"] == day
                    for s in result.values()
                ),
                "duplicate original ticker/day",
            )
            daily[day] += 1
            require(daily[day] <= 4, "original daily roster cap")
            result[key] = dict(key=key, arm=arm, ticker=unit["ticker"], day=day, at=at)
    return result


class SourceAudit:
    """Independent exact-minute/gate/path replay over a checked typed source."""

    def __init__(self, source: Source) -> None:
        self.source = source
        self.points: dict[tuple[str, date, datetime], Candle] = {}
        self.paths: dict[
            tuple[str, int, str], tuple[float | None, float | None, int, list[str]]
        ] = {}
        for candle in source.candles:
            at = candle.at.astimezone(UTC)
            op, hi, lo, cl = (
                number(x) for x in (candle.open, candle.high, candle.low, candle.close)
            )
            require(
                candle.at.tzinfo is not None and at.second == at.microsecond == 0,
                "invalid source minute clock",
            )
            require(
                bounds(candle.day)[0] <= at < bounds(candle.day)[1]
                and at.astimezone(_ET).date() == candle.day,
                "source minute outside RTH",
            )
            require(lo <= min(op, cl) <= max(op, cl) <= hi, "source OHLC geometry")
            key = candle.ticker, candle.day, at
            require(key not in self.points or self.points[key] == candle, "source minute conflict")
            self.points[key] = candle

    def gate(self, signal: dict[str, Any], minutes: int) -> dict[str, Any]:
        if not minutes:
            return dict(state=True, start=None, end=None, available_at=None, missing=[])
        cfg = config()
        opening = bounds(signal["day"])[0]
        latest = signal["at"] - timedelta(minutes=cfg["assumed_bar_lag_minutes"])
        blocks = math.floor((latest - opening).total_seconds() / (minutes * 60))
        if blocks < 1:
            return dict(
                state=None,
                start=None,
                end=None,
                available_at=None,
                missing=["no_completed_initial_bar"],
            )
        end = opening + timedelta(minutes=blocks * minutes)
        start = end - timedelta(minutes=minutes)
        points = [
            self.points.get((signal["ticker"], signal["day"], start + n * _MINUTE))
            for n in range(minutes)
        ]
        missing = ["completed_bar_minute_missing"] if any(p is None for p in points) else []
        return dict(
            state=None if missing else points[-1].close > points[0].open,
            start=start,
            end=end,
            available_at=end + timedelta(minutes=cfg["assumed_bar_lag_minutes"]),
            missing=missing,
        )

    def marks(
        self, signal: dict[str, Any], delay: int, horizon: str
    ) -> tuple[dict[str, Any] | None, str]:
        cfg = config()
        due = signal["at"] + timedelta(minutes=delay)
        at = due.replace(second=0, microsecond=0) + _MINUTE
        opening, closing = bounds(signal["day"])
        entry = None
        while at <= due + timedelta(minutes=cfg["entry_tolerance_minutes"]):
            if opening <= at < closing:
                entry = self.points.get((signal["ticker"], signal["day"], at))
                if entry is not None:
                    break
            at += _MINUTE
        if entry is None:
            return None, "entry_minute_missing"
        spy_entry = self.points.get(("SPY", signal["day"], at))
        if spy_entry is None:
            return None, "exact_entry_spy_missing"
        if horizon.startswith("m"):
            exit_at = at + timedelta(minutes=int(horizon[1:]))
            if exit_at >= closing:
                return None, "intraday_endpoint_outside_rth"
            exit_source, day, kind = exit_at, signal["day"], "minute_open"
        else:
            day = target(signal["day"], int(horizon[1:]))
            if day > min(cfg["source_end"], max(self.source.sessions)):
                return None, "right_censored_source_end"
            if day not in self.source.sessions:
                return None, "target_session_missing"
            exit_at = bounds(day)[1]
            exit_source, kind = exit_at - _MINUTE, "final_rth_minute_close"
        exit_ = self.points.get((signal["ticker"], day, exit_source))
        spy_exit = self.points.get(("SPY", day, exit_source))
        if exit_ is None:
            return None, "exact_exit_minute_missing"
        if spy_exit is None:
            return None, "exact_exit_spy_missing"
        return dict(
            entry_at=at,
            exit_at=exit_at,
            exit_source_at=exit_source,
            exit_kind=kind,
            entry_price=entry.open,
            exit_price=exit_.open if kind == "minute_open" else exit_.close,
            spy_entry_price=spy_entry.open,
            spy_exit_price=spy_exit.open if kind == "minute_open" else spy_exit.close,
        ), ""

    def path(
        self, signal: dict[str, Any], delay: int, horizon: str, marks: dict[str, Any]
    ) -> tuple[float | None, float | None, int, list[str]]:
        key = signal["key"], delay, horizon
        if key not in self.paths:
            high = low = marks["entry_price"]
            high, low = max(high, marks["exit_price"]), min(low, marks["exit_price"])
            negative = 0
            for day in sorted(self.source.sessions):
                if not signal["day"] <= day <= marks["exit_source_at"].astimezone(_ET).date():
                    continue
                at = max(bounds(day)[0], marks["entry_at"])
                end = min(bounds(day)[1], marks["exit_at"])
                while at < end:
                    point = self.points.get((signal["ticker"], day, at))
                    if point is None:
                        self.paths[key] = None, None, negative, ["regular_minute_path_gap"]
                        return self.paths[key]
                    high, low = max(high, point.high), min(low, point.low)
                    negative += point.volume is not None and point.volume < 0
                    at += _MINUTE
            self.paths[key] = (
                (high / marks["entry_price"] - 1) * 100,
                (low / marks["entry_price"] - 1) * 100,
                negative,
                [],
            )
        return self.paths[key]


def _financial(unit: dict[str, Any], signal: dict[str, Any], source: SourceAudit | None) -> None:
    cfg = config()
    delay, horizon, cost = unit["delay_minutes"], unit["horizon"], unit["cost_bps_per_side"]
    expected_key = hashlib.sha256(
        json.dumps((signal["key"], delay, horizon, cost)).encode()
    ).hexdigest()
    require(unit["key"] == expected_key, "unit key mismatch")
    require(
        unit["arm"] == signal["arm"]
        and unit["ticker"] == signal["ticker"]
        and unit["day"] == str(signal["day"])
        and stamp(unit["signal_at"]) == signal["at"],
        "unit signal identity",
    )
    marks, error = source.marks(signal, delay, horizon) if source else (None, "")
    if (
        source
        and not error
        and marks
        and int(cfg["unit_budget_usd"] // (marks["entry_price"] * (1 + cost / 10000))) < 1
    ):
        error = "no_affordable_whole_share"
    if unit["missing"]:
        require(
            isinstance(unit["missing"], list) and len(unit["missing"]) > 0, "missing reason shape"
        )
        if source:
            require(
                unit["missing"] == [error] and bool(error), "missing endpoint disagrees with source"
            )
        for field in (
            "entry_at",
            "exit_at",
            "exit_source_at",
            "exit_kind",
            "entry_price",
            "exit_price",
            "spy_entry_price",
            "spy_exit_price",
            "quantity",
            "debit",
            "gross_usd",
            "cost_usd",
            "net_usd",
            "spy_matched_usd",
            "excess_usd",
            "return_pct",
            "gross_underlying_mfe_pct",
            "gross_underlying_mae_pct",
        ):
            require(unit[field] is None, "missing outcome represented as a number")
        return
    require(not error, "resolved endpoint absent from source")
    entry, exit_, exit_source = (
        stamp(unit[name]) for name in ("entry_at", "exit_at", "exit_source_at")
    )
    due = signal["at"] + timedelta(minutes=delay)
    require(due < entry <= due + timedelta(minutes=cfg["entry_tolerance_minutes"]), "entry timing")
    require(
        entry.second == entry.microsecond == 0
        and bounds(signal["day"])[0] <= entry < bounds(signal["day"])[1],
        "entry outside RTH minute",
    )
    if horizon.startswith("m"):
        require(
            exit_ == entry + timedelta(minutes=int(horizon[1:]))
            and exit_source == exit_
            and exit_ < bounds(signal["day"])[1]
            and unit["exit_kind"] == "minute_open",
            "intraday exit semantics",
        )
    else:
        day = target(signal["day"], int(horizon[1:]))
        require(
            day <= cfg["source_end"]
            and exit_ == bounds(day)[1]
            and exit_source == exit_ - _MINUTE
            and unit["exit_kind"] == "final_rth_minute_close",
            "session close semantics",
        )
    raw, out, spy_in, spy_out = (
        number(unit[k]) for k in ("entry_price", "exit_price", "spy_entry_price", "spy_exit_price")
    )
    if source:
        assert marks is not None
        for field, actual in marks.items():
            if isinstance(actual, datetime):
                require(stamp(unit[field]) == actual, "source endpoint clock mismatch")
            elif isinstance(actual, str):
                require(unit[field] == actual, "source endpoint kind mismatch")
            else:
                equal(unit[field], actual, "raw source mark mismatch")
    rate = cost / 10000
    quantity = int(cfg["unit_budget_usd"] // (raw * (1 + rate)))
    require(
        type(unit["quantity"]) is int and unit["quantity"] == quantity and quantity > 0,
        "whole-share sizing",
    )
    debit = quantity * raw * (1 + rate)
    gross = quantity * (out - raw)
    modeled_cost = quantity * rate * (raw + out)
    net = gross - modeled_cost
    benchmark = debit * (spy_out * (1 - rate) / (spy_in * (1 + rate)) - 1)
    require(debit <= cfg["unit_budget_usd"] + 1e-7, "reference budget exceeded")
    for field, expected in (
        ("debit", debit),
        ("gross_usd", gross),
        ("cost_usd", modeled_cost),
        ("net_usd", net),
        ("spy_matched_usd", benchmark),
        ("excess_usd", net - benchmark),
        ("return_pct", net / debit * 100),
    ):
        equal(unit[field], expected, f"{field} arithmetic mismatch")
    mfe, mae = unit["gross_underlying_mfe_pct"], unit["gross_underlying_mae_pct"]
    if unit["path_missing"]:
        require(mfe is None and mae is None, "missing excursion replaced with zero")
    else:
        move = (out / raw - 1) * 100
        require(
            isinstance(mfe, (float, int)) and isinstance(mae, (float, int)),
            "excursion absent without reason",
        )
        require(
            math.isfinite(mfe)
            and math.isfinite(mae)
            and mfe >= max(0, move) - 1e-7
            and mae <= min(0, move) + 1e-7,
            "excursion endpoint bounds",
        )
    if source:
        assert marks is not None
        expected_mfe, expected_mae, negative, path_missing = source.path(
            signal, delay, horizon, marks
        )
        require(
            unit["path_missing"] == path_missing and unit["negative_volume_path_bars"] == negative,
            "source path coverage",
        )
        if path_missing:
            require(mfe is None and mae is None, "source path missing")
        else:
            assert expected_mfe is not None and expected_mae is not None
            equal(mfe, expected_mfe, "MFE source path mismatch")
            equal(mae, expected_mae, "MAE source path mismatch")


def audit(
    report: dict[str, Any], original: dict[str, Any], source: Source | None = None
) -> dict[str, Any]:
    cfg = config()
    original_hash = original["report_hash"]
    require(
        checksum({k: v for k, v in original.items() if k != "report_hash"}) == original_hash,
        "original report checksum",
    )
    require(report["original_report_hash"] == original_hash, "original report identity")
    require(
        report["profile_hash"]
        == hashlib.sha256((ROOT / "profiles/horizon_research_v1.yaml").read_bytes()).hexdigest(),
        "protocol profile changed",
    )
    require(
        report.get("winner") is None and report.get("validated_alpha") is False,
        "invalid promotion claim",
    )
    require(
        report["classification"] == "EXPLORATORY_REUSED_VENDOR_EVENT_TIME_ASSUMPTION",
        "research classification",
    )
    require(
        report["source_sessions"] == cfg["source_sessions"]
        and report["evaluation_sessions"] == cfg["evaluation_sessions"],
        "source session counts",
    )
    units = report["units"]
    if "ledger_content_hash" in report:
        require(checksum(units) == report["ledger_content_hash"], "ledger content hash")
    if "report_hash" in report:
        require(
            checksum({k: v for k, v in report.items() if k not in ("units", "report_hash")})
            == report["report_hash"],
            "report checksum",
        )
    implementation = hashlib.sha256(
        b"".join(
            p.name.encode() + p.read_bytes()
            for p in sorted((ROOT / "webapp/horizon_research").glob("*.py"))
        )
        + b"".join(
            p.name.encode() + p.read_bytes()
            for p in (
                ROOT / "docs/phase-5.54-horizon-study-acceptance.md",
                ROOT / "docs/phase-5.54-volume-quality-addendum.md",
            )
        )
    ).hexdigest()
    require(report["code_hash"] == implementation, "implementation changed")
    signals = _signals(original)
    manifest = tuple(sorted(original["source_packet_hashes"].items()))
    require(
        [list(p) for p in manifest] == [list(p) for p in report["source_packet_hashes"]],
        "source packet manifest mismatch",
    )
    expected_source_hash = checksum(
        dict(
            original_report_hash=original_hash,
            new_profile_hash=report["profile_hash"],
            signals=sorted(signals.values(), key=lambda s: (s["at"], s["arm"], s["ticker"])),
            packet_hashes=manifest,
        )
    )
    require(report["source_hash"] == expected_source_hash, "source hash mismatch")
    indexed_source = SourceAudit(source) if source else None
    if source:
        require(
            source.source_hash == expected_source_hash
            and source.original_report_hash == original_hash,
            "typed source identity",
        )
        require(tuple(source.packet_hashes) == manifest, "typed source packet manifest")
    horizons = [f"m{x}" for x in cfg["intraday_horizons_minutes"]] + [
        f"c{x}" for x in cfg["session_close_offsets"]
    ]
    lookup: dict[tuple[str, int, str, int], dict[str, Any]] = {}
    by_key: dict[str, dict[str, Any]] = {}
    for unit in units:
        signal = signals.get(unit["signal_key"])
        require(signal is not None, "unit outside frozen roster")
        assert signal is not None
        require(
            unit["delay_minutes"] in cfg["entry_delays_minutes"]
            and unit["horizon"] in horizons
            and unit["cost_bps_per_side"] in cfg["costs_bps_per_side"],
            "unregistered outcome unit",
        )
        identity = (
            signal["key"],
            unit["delay_minutes"],
            unit["horizon"],
            unit["cost_bps_per_side"],
        )
        require(identity not in lookup and unit["key"] not in by_key, "duplicate outcome unit")
        _financial(unit, signal, indexed_source)
        lookup[identity], by_key[unit["key"]] = unit, unit
    expected_units = (
        len(signals)
        * len(cfg["entry_delays_minutes"])
        * len(horizons)
        * len(cfg["costs_bps_per_side"])
    )
    require(
        len(lookup) == report["deduplicated_outcome_units"] == expected_units,
        "outcome tensor incomplete",
    )
    gates = {(g["signal_key"], g["minutes"]): g for g in report["gates"]}
    require(
        len(gates) == len(report["gates"]) == len(signals) * len(cfg["bar_filters_minutes"]),
        "gate tensor incomplete",
    )
    if "signal_gates" in report:
        require(report["signal_gates"] == report["gates"], "gate aliases disagree")
    for (key, minutes), gate in gates.items():
        require(
            key in signals
            and minutes in cfg["bar_filters_minutes"]
            and type(gate["state"]) in (bool, type(None)),
            "unregistered gate",
        )
        signal = signals[key]
        if indexed_source:
            expected = indexed_source.gate(signal, minutes)
            for field, value in expected.items():
                require(
                    (stamp(gate[field]) if isinstance(value, datetime) else gate[field]) == value,
                    "source candle gate mismatch",
                )
        if minutes == 0:
            require(
                gate["state"] is True
                and gate["missing"] == []
                and gate["start"] is None
                and gate["end"] is None,
                "no-filter gate",
            )
        else:
            opening = bounds(signal["day"])[0]
            blocks = math.floor(
                (
                    (signal["at"] - timedelta(minutes=cfg["assumed_bar_lag_minutes"])) - opening
                ).total_seconds()
                / (minutes * 60)
            )
            if blocks < 1:
                require(
                    gate["state"] is None
                    and gate["start"] is None
                    and gate["end"] is None
                    and gate["missing"] == ["no_completed_initial_bar"],
                    "initial candle unknown",
                )
            else:
                end = opening + timedelta(minutes=blocks * minutes)
                require(
                    stamp(gate["end"]) == end
                    and stamp(gate["start"]) == end - timedelta(minutes=minutes),
                    "anchored last completed candle",
                )
                require(
                    stamp(gate["available_at"])
                    == end + timedelta(minutes=cfg["assumed_bar_lag_minutes"])
                    <= signal["at"],
                    "candle lookahead",
                )
                require(bool(gate["missing"]) == (gate["state"] is None), "gate missingness")
    cells = {}
    for row in report["rows"]:
        identity = (
            row["arm"],
            row["delay_minutes"],
            row["filter_minutes"],
            row["horizon"],
            row["cost_bps_per_side"],
        )
        arm, delay, minutes, horizon, cost = identity
        require(
            arm in cfg["arms"]
            and delay in cfg["entry_delays_minutes"]
            and minutes in cfg["bar_filters_minutes"]
            and horizon in horizons
            and cost in cfg["costs_bps_per_side"]
            and identity not in cells,
            "unregistered/duplicate cell",
        )
        roster = [s for s in signals.values() if s["arm"] == arm]
        true = [s for s in roster if gates[s["key"], minutes]["state"] is True]
        false = [s["key"] for s in roster if gates[s["key"], minutes]["state"] is False]
        unknown = [s["key"] for s in roster if gates[s["key"], minutes]["state"] is None]
        selected = [lookup[s["key"], delay, horizon, cost] for s in true]
        resolved = [u for u in selected if not u["missing"]]
        require(
            set(row["unit_keys"]) == {u["key"] for u in selected}
            and len(row["unit_keys"]) == len(selected),
            "replacement or selected roster mismatch",
        )
        require(
            set(row["false_signal_keys"]) == set(false)
            and set(row["unknown_signal_keys"]) == set(unknown),
            "filter no-replacement mismatch",
        )
        for field, expected in (
            ("roster_count", len(roster)),
            ("gate_true", len(true)),
            ("gate_false", len(false)),
            ("gate_unknown", len(unknown)),
            ("selected", len(selected)),
            ("resolved", len(resolved)),
            ("missing", len(selected) - len(resolved)),
        ):
            require(type(row[field]) is int and row[field] == expected, "cell coverage mismatch")
        reasons = Counter(r for u in selected for r in u["missing"])
        for key in unknown:
            reasons.update(gates[key, minutes]["missing"])
        require(row["missing_reasons"] == dict(sorted(reasons.items())), "missing reasons mismatch")
        complete = not unknown and len(selected) == len(resolved)
        require(
            row["status"] == ("COMPLETE" if complete else "PARTIAL" if resolved else "BLOCKED")
            and row["known_totals_are_partial"] is (not complete),
            "cell strict status",
        )
        net, excess = (
            math.fsum(u["net_usd"] for u in resolved),
            math.fsum(u["excess_usd"] for u in resolved),
        )
        equal(row["known_net_usd"], net)
        equal(row["known_excess_usd"], excess)
        if complete:
            equal(row["strict_net_usd"], net)
            equal(row["strict_excess_usd"], excess)
        else:
            require(
                row["strict_net_usd"] is None and row["strict_excess_usd"] is None,
                "unknown strict total replaced with zero",
            )
        cells[identity] = row
    require(
        len(cells)
        == report["registered_cells"]
        == report["assessed_cells"]
        == cfg["registered_cells"],
        "registered cell tensor incomplete",
    )
    for status, field in (
        ("COMPLETE", "complete_cells"),
        ("PARTIAL", "partial_cells"),
        ("BLOCKED", "blocked_cells"),
    ):
        require(
            report[field] == sum(r["status"] == status for r in cells.values()),
            "tensor status counts",
        )
    controls = []
    require(len(original["rows"]) == 12, "original controls missing")
    for old in original["rows"]:
        arm, cost = old["arm"], old["bps_per_side"]
        row = cells[arm, 1, 0, "m60", cost]
        selected = [by_key[k] for k in row["unit_keys"]]
        mapping = {(u["ticker"], u["day"], stamp(u["signal_at"])): u for u in selected}
        require(
            len(mapping)
            == len(selected)
            == old["selected"]
            == old["resolved"]
            == len(old["ledger"])
            and old["unresolved"] == 0
            and old["missing"] == [],
            "original selected/resolved coverage",
        )
        for old_unit in old["ledger"]:
            actual = mapping.pop(
                (old_unit["ticker"], old_unit["day"], stamp(old_unit["signal_at"]))
            )
            for field in ("entry_at", "exit_at"):
                require(stamp(actual[field]) == stamp(old_unit[field]), "original control time")
            require(actual["quantity"] == old_unit["quantity"], "original control quantity")
            for field in ("debit", "net_usd", "spy_matched_usd", "excess_usd", "return_pct"):
                equal(actual[field], old_unit[field], "original control ledger mismatch")
        require(not mapping, "new original-control replacements")
        net, excess = (
            math.fsum(u["net_usd"] for u in selected),
            math.fsum(u["excess_usd"] for u in selected),
        )
        for field in ("net_unit_usd", "net_known_unit_usd"):
            equal(old[field], net, "original aggregate mismatch")
        equal(old["spy_excess_known_unit_usd"], excess, "original excess aggregate")
        ordered = sorted((u["net_usd"] for u in selected), reverse=True)
        equal(old["without_best_1_usd"], math.fsum(ordered[1:]), "original best-one removal")
        equal(old["without_best_3_usd"], math.fsum(ordered[3:]), "original best-three removal")
        controls.append(dict(arm=arm, cost_bps_per_side=cost, units=len(selected), net_usd=net))
    _primary(report, signals, lookup, horizons)
    result = dict(
        status="RECONCILED",
        report_hash=report.get(
            "report_hash", checksum({k: v for k, v in report.items() if k != "units"})
        ),
        original_report_hash=original_hash,
        profile_hash=report["profile_hash"],
        code_hash=report["code_hash"],
        source_hash=report["source_hash"],
        audit_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        audited_cells=len(cells),
        audited_outcome_units=len(units),
        audited_original_controls=len(controls),
        controls=controls,
        source_replay="ALL_MARKS_GATES_AND_EXCURSIONS"
        if indexed_source
        else "NOT_REPLAYED_LOADER_HASH_BOUND_ONLY",
        source_limitations=[]
        if indexed_source
        else [
            "Raw OHLC/gate/path bytes were not independently replayed; only artifact arithmetic, timing, roster and published controls reconciled."
        ],
        winner=None,
        validated_alpha=False,
    )
    result["audit_hash"] = checksum(result)
    return result


def _primary(
    report: dict[str, Any],
    signals: dict[str, dict[str, Any]],
    lookup: dict[tuple[str, int, str, int], dict[str, Any]],
    horizons: list[str],
) -> None:
    cfg, primary = config(), report["primary_family"]
    evaluation = sessions()[cfg["warmup_sessions"] :]
    days = [
        d for d in evaluation if target(d, max(cfg["session_close_offsets"])) <= cfg["source_end"]
    ]
    source = [s for s in signals.values() if s["arm"] == cfg["primary_arm"]]
    chosen = [
        s
        for s in source
        if s["day"] in days
        and s["at"]
        + timedelta(
            minutes=max(cfg["entry_delays_minutes"])
            + cfg["entry_tolerance_minutes"]
            + max(cfg["intraday_horizons_minutes"])
        )
        < bounds(s["day"])[1]
    ]
    require(
        primary["administrative_days"] == [str(d) for d in days]
        and primary["right_censored_days_excluded"]
        == [str(d) for d in evaluation if d not in days],
        "calendar-only common date panel",
    )
    require(
        primary["full_baseline_roster"] == len(source)
        and primary["administrative_roster"] == len(chosen)
        and set(primary["excluded_signal_keys"]) == {s["key"] for s in source if s not in chosen},
        "administrative roster changed",
    )
    vectors = {}
    incomplete = {}
    for day in days:
        vector = {}
        for horizon in horizons:
            units = [
                lookup[
                    s["key"],
                    cfg["primary_delay_minutes"],
                    horizon,
                    cfg["primary_cost_bps_per_side"],
                ]
                for s in chosen
                if s["day"] == day
            ]
            vector[horizon] = (
                None
                if any(u["missing"] for u in units)
                else math.fsum(u["excess_usd"] for u in units)
            )
        vectors[str(day)] = vector
        if any(v is None for v in vector.values()):
            incomplete[str(day)] = [h for h, v in vector.items() if v is None]
        actual = primary["daily_vectors"][str(day)]
        for horizon, value in vector.items():
            if value is None:
                require(actual[horizon] is None, "unknown paired daily mark replaced with zero")
            else:
                equal(actual[horizon], value, "paired daily vector mismatch")
    common = [d for d in days if str(d) not in incomplete]
    require(
        primary["complete_common_days"] == [str(d) for d in common]
        and primary["incomplete_administrative_days"] == incomplete,
        "complete common day coverage",
    )
    differences = {}
    for row in primary["contrasts"]:
        horizon = row["horizon"]
        require(
            horizon in horizons and row["complete_days"] == len(common), "paired horizon identity"
        )
        vals = [vectors[str(d)][horizon] for d in common]
        diffs = [vectors[str(d)][horizon] - vectors[str(d)][cfg["anchor_horizon"]] for d in common]
        differences[horizon] = diffs
        if not common:
            require(
                all(
                    row[k] is None
                    for k in (
                        "total_excess_usd",
                        "mean_daily_excess_usd",
                        "mean_daily_difference_vs_m60_usd",
                    )
                ),
                "empty paired totals",
            )
        else:
            equal(row["total_excess_usd"], math.fsum(vals))
            equal(row["mean_daily_excess_usd"], math.fsum(vals) / len(vals))
            equal(row["mean_daily_difference_vs_m60_usd"], math.fsum(diffs) / len(diffs))
    require(
        len(differences) == len(primary["contrasts"]) == len(horizons), "primary horizon tensor"
    )
    bootstrap = primary["bootstrap"]
    require(bootstrap.get("formal_p_value") is None, "formal p-value claim")
    if common and len(common) == len(days) and len(days) >= 2 * cfg["bootstrap_block_sessions"]:
        require(
            bootstrap["status"] == "DESCRIPTIVE_ONLY"
            and bootstrap["draws"] == cfg["bootstrap_draws"]
            and bootstrap["seed"] == cfg["bootstrap_seed"]
            and bootstrap["block_sessions"] == cfg["bootstrap_block_sessions"]
            and bootstrap["shared_day_indices"] is True,
            "shared descriptive resampling registration",
        )
        rng = random.Random(cfg["bootstrap_seed"])
        draws = {h: [] for h in horizons}
        for _ in range(cfg["bootstrap_draws"]):
            indices = []
            while len(indices) < len(days):
                start = rng.randrange(len(days))
                indices += [(start + j) % len(days) for j in range(cfg["bootstrap_block_sessions"])]
            for h in horizons:
                draws[h].append(
                    math.fsum(differences[h][i] for i in indices[: len(days)]) / len(days)
                )
        centers = {h: math.fsum(differences[h]) / len(days) for h in horizons}
        deviation = sorted(
            max(abs(draws[h][i] - centers[h]) for h in horizons)
            for i in range(cfg["bootstrap_draws"])
        )
        radius = deviation[int(0.95 * (len(deviation) - 1))]
        for row in primary["contrasts"]:
            h = row["horizon"]
            ordered = sorted(draws[h])
            for actual, expected in zip(
                row["pointwise_descriptive_95pct_difference"],
                (
                    ordered[int(0.025 * (len(ordered) - 1))],
                    ordered[int(0.975 * (len(ordered) - 1))],
                ),
                strict=True,
            ):
                equal(actual, expected, "descriptive percentile mismatch")
            if h == cfg["anchor_horizon"]:
                # The anchor is subtracted from itself in every day and every
                # bootstrap draw. Its uncertainty is exactly zero even when
                # other horizon contrasts have a positive shared radius.
                band = row["shared_max_deviation_descriptive_95pct_band"]
                require(
                    isinstance(band, list)
                    and len(band) == 2
                    and all(type(value) in (int, float) and value == 0 for value in band),
                    "anchor self-difference band must be exact zero",
                )
            else:
                for actual, expected in zip(
                    row["shared_max_deviation_descriptive_95pct_band"],
                    (centers[h] - radius, centers[h] + radius),
                    strict=True,
                ):
                    equal(actual, expected, "descriptive shared band mismatch")
    else:
        require(bootstrap["status"] == "UNAVAILABLE", "incomplete vector must block resampling")


def _safe(path: Path) -> Path:
    require(
        not any(p.casefold().replace("-", "_").startswith("study_g") for p in path.parts),
        "reserved evidence path",
    )
    resolved = path.resolve(strict=True)
    require(
        not any(p.casefold().replace("-", "_").startswith("study_g") for p in resolved.parts),
        "reserved evidence path",
    )
    return resolved


def load_report(path: Path) -> dict[str, Any]:
    path = _safe(path)
    body = json.loads(path.read_text())
    require(body["ledger_file"] == "units.json.gz", "unexpected ledger filename")
    ledger = _safe(path.parent / body["ledger_file"])
    compressed = ledger.read_bytes()
    require(
        hashlib.sha256(compressed).hexdigest() == body["ledger_sha256"], "compressed ledger hash"
    )
    with gzip.GzipFile(fileobj=__import__("io").BytesIO(compressed)) as stream:
        raw = stream.read(256 * 1024 * 1024 + 1)
    require(len(raw) <= 256 * 1024 * 1024, "ledger storage bound")
    body["units"] = json.loads(raw)
    require(checksum(body["units"]) == body["ledger_content_hash"], "ledger content checksum")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--original", type=Path, default=ROOT / "artifacts/vendor-history-v1/report.json"
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "refusing to overwrite audit evidence")
    original = json.loads(_safe(args.original).read_text())
    source = None
    if args.source is not None:
        from webapp.horizon_research.source import load

        source = load(args.source)
    result = audit(load_report(args.report), original, source)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "status",
                    "report_hash",
                    "audited_cells",
                    "audited_outcome_units",
                    "audited_original_controls",
                    "source_replay",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
