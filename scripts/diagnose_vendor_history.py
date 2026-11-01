"""Twelve fixed historical-reference diagnostics; no selection or live promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from ruamel.yaml import YAML
from webapp.alpha_lab.flow import timestamp
from webapp.history_capture.capture import ROOT, digest, read

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings

ET = ZoneInfo("America/New_York")


def number(value: object) -> float | None:
    try:
        result = float(str(value))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def money(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def flow_value(
    records: list[dict[str, Any]], at: datetime, cfg: dict[str, Any]
) -> tuple[float, float, int] | None:
    up = down = unknown = Decimal(0)
    contracts = set()
    for row in records:
        created = timestamp(row.get("created_at"))
        end = number(row.get("end_time"))
        final = datetime.fromtimestamp(end / 1000, UTC) if end and end > 0 else created
        # Availability here is a declared historical assumption, not receipt evidence.
        available = (
            max(created, final) + timedelta(minutes=cfg["assumed_event_lag_minutes"])
            if created and final
            else None
        )
        if available is None:
            return None
        if not at - timedelta(seconds=cfg["flow_window_seconds"]) <= available <= at:
            continue
        if row.get("has_multileg") is not False or row.get("type") not in ("call", "put"):
            return None
        total, ask, bid = (
            money(row.get(k))
            for k in ("total_premium", "total_ask_side_prem", "total_bid_side_prem")
        )
        if (
            total is None
            or ask is None
            or bid is None
            or total <= 0
            or min(ask, bid) < 0
            or ask + bid > total
        ):
            return None
        if row["type"] == "call":
            up += ask
            down += bid
        else:
            up += bid
            down += ask
        unknown += total - ask - bid
        chain = row.get("option_chain")
        if not isinstance(chain, str) or not chain:
            return None
        contracts.add(chain)
    total = up + down + unknown
    if total and (up + down) / total < cfg["minimum_known_share"]:
        return None
    return float(up), float(up / (up + down)) if up + down else 0.0, len(contracts)


def prices(packet: dict[str, Any]) -> dict[datetime, tuple[float, float]]:
    result = {}
    for row in packet["payload"]["data"]:
        at = timestamp(row.get("start_time", row.get("time")))
        opening, closing = number(row.get("open")), number(row.get("close"))
        if at is None or opening is None or closing is None or min(opening, closing) <= 0:
            continue
        if at in result and result[at] != (opening, closing):
            raise ValueError("conflicting native minute prices")
        result[at] = (opening, closing)
    return result


def reference(
    s: dict[str, Any], tapes: dict, benchmark: str, cfg: dict[str, Any], bps: int
) -> dict[str, Any] | None:
    tape = tapes[s["ticker"], s["day"]]
    spy = tapes[benchmark, s["day"]]
    due = s["at"] + timedelta(minutes=cfg["delay_minutes"])
    options = sorted(
        t for t in tape if due < t <= due + timedelta(minutes=cfg["mark_tolerance_minutes"])
    )
    entry_at = options[0] if options else None
    exit_at = entry_at + timedelta(minutes=cfg["hold_minutes"]) if entry_at else None
    if entry_at is None or exit_at not in tape or entry_at not in spy or exit_at not in spy:
        return None
    slip = bps / 10000
    entry = tape[entry_at][0] * (1 + slip)
    exit_ = tape[exit_at][0] * (1 - slip)
    quantity = int(cfg["unit_budget_usd"] // entry)
    if quantity <= 0:
        return None
    debit = quantity * entry
    net = quantity * (exit_ - entry)
    benchmark_return = (spy[exit_at][0] * (1 - slip) / (spy[entry_at][0] * (1 + slip)) - 1) * debit
    return {
        "ticker": s["ticker"],
        "day": s["day"],
        "signal_at": s["at"],
        "entry_at": entry_at,
        "exit_at": exit_at,
        "quantity": quantity,
        "debit": debit,
        "net_usd": net,
        "spy_matched_usd": benchmark_return,
        "excess_usd": net - benchmark_return,
        "return_pct": net / debit * 100,
    }


def timing_control(
    samples: list[dict[str, Any]],
    selected: dict[str, list[dict[str, Any]]],
    tapes: dict,
    benchmark: str,
    cfg: dict[str, Any],
    days: list[date],
) -> dict[str, Any]:
    timing = YAML(typ="safe").load((ROOT / "profiles/history_timing_control_v1.yaml").read_text())
    choices = defaultdict(list)
    for sample in samples:
        choices[sample["ticker"], sample["day"]].append(sample)
    keys = sorted({(s["ticker"], s["day"]) for group in selected.values() for s in group})
    cache = {
        (s["ticker"], s["day"], s["at"]): reference(
            s, tapes, benchmark, cfg, cfg["slippage_bps"][0]
        )
        for s in samples
    }
    rng = random.Random(timing["seed"])
    scores = {arm: [] for arm in selected}
    invalid = Counter()
    for _ in range(timing["draws"]):
        draw = {key: rng.choice(choices[key]) for key in keys}
        for arm, group in selected.items():
            units = [cache[s["ticker"], s["day"], draw[s["ticker"], s["day"]]["at"]] for s in group]
            if any(unit is None for unit in units):
                invalid[arm] += 1
            else:
                scores[arm].append(sum(unit["excess_usd"] for unit in units) / len(days))
    rows = []
    for arm, group in selected.items():
        units = [cache[s["ticker"], s["day"], s["at"]] for s in group]
        actual = (
            sum(u["excess_usd"] for u in units) / len(days)
            if units and all(u is not None for u in units)
            else None
        )
        null = sorted(scores[arm])
        rows.append(
            {
                "arm": arm,
                "actual_mean_daily_excess_usd": actual,
                "draws": timing["draws"],
                "valid_draws": len(null),
                "invalid_draws": invalid[arm],
                "fraction_valid_null_below_actual": sum(v < actual for v in null) / len(null)
                if actual is not None and null
                else None,
                "descriptive_null_range_usd": [
                    null[int(0.025 * (len(null) - 1))],
                    null[int(0.975 * (len(null) - 1))],
                ]
                if null
                else None,
            }
        )
    return {
        "classification": "DESCRIPTIVE_SHARED_TIMING_RESAMPLES_NOT_INDEPENDENT_TRIALS",
        "profile_hash": digest(timing),
        "rows": rows,
        "formal_p_value": None,
        "limitation": "Same ticker/day control; does not preserve all intraday volatility or liquidity conditions.",
    }


def diagnose(source: Path) -> dict[str, Any]:
    cfg = YAML(typ="safe").load((ROOT / "profiles/history_diagnostic_v1.yaml").read_text())
    state = json.loads((source / "state.json").read_text())
    report = {
        "script_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "classification": cfg["classification"],
        "status": "CAPTURE_INCOMPLETE",
        "winner": None,
        "validated_alpha": False,
        "full_438_calibration_executed": False,
        "executed_fixed_replays": 0,
        "profile_hash": digest(cfg),
        "dependency_hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "webapp/history_capture/capture.py",
                "src/uoa_detector/live_alpha/calendar.py",
                "src/uoa_detector/live_alpha/settings.py",
                "profiles/live_alpha_v1.yaml",
            )
        },
        "source_state_hash": digest(state),
        "rows": [],
        "limitations": [
            "Historical alert aggregates are final snapshots; first-seen/revision availability is unverified.",
            "Event lag is assumed; no original LIVE/READY decision or poll was reconstructed.",
            "Minute opens are model references, not executable quotes/fills.",
            "No option P&L, source-only gamma/news/sector feature, or Study G price file was used.",
        ],
    }
    if state["status"] != "complete":
        return report
    start, end = (
        date.fromisoformat(state["profile"]["start"]),
        date.fromisoformat(state["profile"]["end"]),
    )
    tickers = state["profile"]["tickers"]
    benchmark = state["profile"]["benchmark"]
    cal = load_live_alpha_settings().calendar
    days = []
    cursor = start
    while cursor <= end:
        if is_session_day(cursor, cal):
            days.append(cursor)
        cursor += timedelta(days=1)
    tapes = {}
    flows = {}
    hashes = {}
    for day in days:
        for ticker in [*tickers, benchmark]:
            path = source / day.isoformat() / f"{ticker}-minute.json.gz"
            packet = read(path)
            if (
                not packet["complete"]
                or packet.get("profile_hash") != state["profile_hash"]
                or packet.get("day") != day.isoformat()
                or packet.get("ticker") != ticker
            ):
                raise ValueError("incomplete or mismatched minute packet")
            tapes[ticker, day] = prices(packet)
            hashes[str(path.relative_to(source))] = packet["packet_hash"]
            if ticker != benchmark:
                path = source / day.isoformat() / f"{ticker}-flow.json.gz"
                packet = read(path)
                if (
                    not packet["complete"]
                    or packet.get("profile_hash") != state["profile_hash"]
                    or packet.get("day") != day.isoformat()
                    or packet.get("ticker") != ticker
                ):
                    raise ValueError("incomplete or mismatched flow packet")
                flows[ticker, day] = packet["payload"]["data"]
                hashes[str(path.relative_to(source))] = packet["packet_hash"]
    samples = []
    history = defaultdict(list)
    unknown = Counter()
    for day_index, day in enumerate(days):
        opening = datetime.combine(day, cal.regular_open, ET).astimezone(UTC)
        closing = datetime.combine(day, close_time(day, cal), ET).astimezone(UTC)
        at = opening + timedelta(minutes=cfg["opening_delay_minutes"])
        bucket = 0
        while (
            at
            + timedelta(
                minutes=cfg["delay_minutes"]
                + cfg["hold_minutes"]
                + 2 * cfg["mark_tolerance_minutes"]
            )
            < closing
        ):
            for ticker in tickers:
                flow = flow_value(flows[ticker, day], at, cfg)
                old = history[ticker, bucket][-cfg["history_sessions"] :]
                eligible = []
                current_at = at - timedelta(minutes=cfg["assumed_event_lag_minutes"] + 1)
                prior_at = current_at - timedelta(minutes=cfg["price_lookback_minutes"])
                tape = tapes[ticker, day]
                accept = (
                    tape[current_at][1] / tape[prior_at][1] - 1
                    if current_at in tape and prior_at in tape
                    else None
                )
                if flow is None:
                    unknown["flow_window"] += 1
                else:
                    up, dominance, contracts = flow
                    centre = median(old) if old else 0
                    ratio = (
                        up / centre
                        if centre > 0 and len(old) >= cfg["minimum_history_sessions"]
                        else None
                    )
                    rank = (
                        sum(v <= up for v in old) / len(old)
                        if len(old) >= cfg["minimum_history_sessions"]
                        else None
                    )
                    if (
                        up >= cfg["minimum_directional_premium"]
                        and dominance >= cfg["dominance"]
                        and contracts >= cfg["minimum_contracts"]
                        and accept is not None
                        and accept > 0
                    ):
                        eligible.append("baseline_proxy")
                    normalized = (
                        ratio is not None
                        and rank is not None
                        and ratio >= cfg["ratio"]
                        and rank >= cfg["percentile"]
                        and dominance >= cfg["dominance"]
                    )
                    if normalized:
                        eligible.append("normalized")
                    if normalized and accept is not None and accept > 0:
                        eligible.append("normalized_price")
                    history[ticker, bucket].append(up)
                if day_index >= cfg["warmup_sessions"]:
                    samples.append(
                        {
                            "ticker": ticker,
                            "day": day,
                            "at": at,
                            "eligible": eligible,
                            "close": closing,
                        }
                    )
            at += timedelta(minutes=cfg["bucket_minutes"])
            bucket += 1
    evaluation = days[cfg["warmup_sessions"] :]
    selected_by_arm = {}
    for arm in cfg["arms"]:
        chosen = []
        occupied = set()
        slots = Counter()
        for s in sorted(samples, key=lambda r: (r["at"], r["ticker"])):
            key = s["ticker"], s["day"]
            if (
                arm in s["eligible"]
                and key not in occupied
                and slots[s["day"]] < cfg["max_units_per_day"]
            ):
                occupied.add(key)
                slots[s["day"]] += 1
                chosen.append(s)
        selected_by_arm[arm] = chosen
        for bps in cfg["slippage_bps"]:
            units = []
            missing = []
            for s in chosen:
                unit = reference(s, tapes, benchmark, cfg, bps)
                if unit is None:
                    missing.append(
                        {
                            "ticker": s["ticker"],
                            "day": s["day"],
                            "reason": "exact_native_mark_missing_or_unaffordable",
                        }
                    )
                else:
                    units.append(unit)
            byday = defaultdict(list)
            for u in units:
                byday[u["day"]].append(u["excess_usd"])
            daily = [sum(byday[d]) for d in evaluation]
            interval = None
            if not missing and daily:
                rng = random.Random(cfg["bootstrap_seed"])
                draws = []
                for _ in range(cfg["bootstrap_draws"]):
                    values = []
                    while len(values) < len(daily):
                        begin = rng.randrange(len(daily))
                        values.extend(
                            daily[(begin + j) % len(daily)]
                            for j in range(cfg["bootstrap_block_days"])
                        )
                    draws.append(mean(values[: len(daily)]))
                draws.sort()
                interval = [
                    draws[int(0.025 * (len(draws) - 1))],
                    draws[int(0.975 * (len(draws) - 1))],
                ]
            net_values = sorted((u["net_usd"] for u in units), reverse=True)
            report["rows"].append(
                {
                    "arm": arm,
                    "bps_per_side": bps,
                    "selected": len(chosen),
                    "resolved": len(units),
                    "unresolved": len(missing),
                    "net_unit_usd": None if missing else sum(net_values),
                    "net_known_unit_usd": sum(net_values),
                    "spy_excess_known_unit_usd": sum(u["excess_usd"] for u in units),
                    "without_best_1_usd": sum(net_values[1:]),
                    "without_best_3_usd": sum(net_values[3:]),
                    "descriptive_daily_excess_interval_usd": interval,
                    "ledger": units,
                    "missing": missing,
                }
            )
            report["executed_fixed_replays"] += 1
    report["timing_controls"] = timing_control(
        samples, selected_by_arm, tapes, benchmark, cfg, evaluation
    )
    report.update(
        status="EXPLORATORY_FIXED_DIAGNOSTICS",
        sessions=len(days),
        warmup_sessions=cfg["warmup_sessions"],
        evaluation_sessions=len(evaluation),
        source_packet_hashes=hashes,
        unknown_features=dict(unknown),
        source_received_at="Actual download times retained in the sealed source packets, never backdated.",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite evidence")
    result = diagnose(args.source)
    result["report_hash"] = digest(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as h:
        json.dump(result, h, indent=2, default=str)
        h.write("\n")
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in ("status", "sessions", "executed_fixed_replays", "winner", "report_hash")
            }
        )
    )

    return 0 if result["status"] == "EXPLORATORY_FIXED_DIAGNOSTICS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
