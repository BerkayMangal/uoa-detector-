"""Descriptive saved-quote arithmetic; no historical option P&L or selection."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from statistics import median
from typing import Any

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def audit() -> dict[str, Any]:
    profile_path = ROOT / "profiles/event_quote_audit_v1.yaml"
    cfg = YAML(typ="safe").load(profile_path.read_text())
    fee = Decimal(str(cfg["commission_per_contract_side"]))
    multiplier = cfg["assumed_multiplier"]
    sources = []
    records = {}
    conflicts = set()
    rejection = Counter()
    for path in sorted((ROOT / "artifacts/options-alpha-v1/replay").glob("*/chain*.json")):
        body = path.read_bytes()
        sources.append(
            {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(body).hexdigest()}
        )
        data = json.loads(body)
        day = date.fromisoformat(data["date"])
        for row in data["rows"]:
            rejection["raw_rows"] += 1
            if row.get("option_type") != "call":
                rejection["not_call"] += 1
                continue
            key = data["date"], data["ticker"], row["option_symbol"]
            values = tuple(
                row.get(k) for k in ("expires", "strike", "nbbo_bid", "nbbo_ask", "delta")
            )
            if key in records:
                rejection["duplicate_identity"] += 1
                if records[key][1] != values:
                    conflicts.add(key)
            else:
                records[key] = day, values
    calls = []
    groups = defaultdict(list)
    for key, (day, values) in records.items():
        if key in conflicts:
            rejection["conflicting_quote"] += 1
            continue
        expiry, strike, bid, ask, delta = values
        if not cfg["minimum_dte"] <= (date.fromisoformat(expiry) - day).days <= cfg["maximum_dte"]:
            rejection["outside_dte"] += 1
            continue
        strike, bid, ask, delta = (decimal(v) for v in (strike, bid, ask, delta))
        if strike is None or bid is None or ask is None or not 0 < bid <= ask or strike <= 0:
            rejection["invalid_quote"] += 1
            continue
        mid_delta = (
            delta is not None
            and cfg["minimum_absolute_delta"] <= abs(delta) <= cfg["maximum_absolute_delta"]
        )
        item = {
            "day": key[0],
            "ticker": key[1],
            "symbols": [key[2]],
            "expiry": expiry,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "delta_band": bool(mid_delta),
        }
        groups[key[0], key[1], expiry].append(item)
        debit = ask * multiplier + fee
        credit = bid * multiplier - fee
        calls.append(
            {
                "kind": "long_call",
                **item,
                "debit_usd": debit,
                "immediate_exit_usd": credit,
                "roundtrip_cost_usd": debit - credit,
                "cost_pct": 100 * (debit - credit) / debit,
            }
        )
    spreads = []
    for items in groups.values():
        items.sort(key=lambda r: r["strike"])
        for long, short in pairwise(items):
            if long["strike"] == short["strike"]:
                rejection["ambiguous_adjacent_strike"] += 1
                continue
            debit = (long["ask"] - short["bid"]) * multiplier + 2 * fee
            width = (short["strike"] - long["strike"]) * multiplier
            if not 0 < debit < width:
                rejection["invalid_vertical_debit"] += 1
                continue
            credit = (long["bid"] - short["ask"]) * multiplier - 2 * fee
            spreads.append(
                {
                    "kind": "adjacent_call_debit",
                    "day": long["day"],
                    "ticker": long["ticker"],
                    "expiry": long["expiry"],
                    "symbols": [long["symbols"][0], short["symbols"][0]],
                    "delta_band": long["delta_band"],
                    "debit_usd": debit,
                    "immediate_exit_usd": credit,
                    "roundtrip_cost_usd": debit - credit,
                    "cost_pct": 100 * (debit - credit) / debit,
                }
            )
    ledger = calls + spreads
    summary = []
    for kind in ("long_call", "adjacent_call_debit"):
        for band in ("all_calls", "abs_delta_025_075"):
            rows = [
                r for r in ledger if r["kind"] == kind and (band == "all_calls" or r["delta_band"])
            ]
            costs = sorted(r["cost_pct"] for r in rows)
            summary.append(
                {
                    "kind": kind,
                    "stratum": band,
                    "count": len(rows),
                    "median_cost_pct": float(median(costs)) if costs else None,
                    "p90_cost_pct": float(costs[int(0.9 * (len(costs) - 1))]) if costs else None,
                    "within_25pct": sum(r["cost_pct"] <= cfg["cost_cap_pct"] for r in rows),
                    "one_unit_within_100usd": sum(
                        r["debit_usd"] <= cfg["unit_budget_usd"] for r in rows
                    ),
                    "both_gates": sum(
                        r["cost_pct"] <= cfg["cost_cap_pct"]
                        and r["debit_usd"] <= cfg["unit_budget_usd"]
                        for r in rows
                    ),
                }
            )
    return {
        "classification": "DESCRIPTIVE_ARCHIVED_QUOTE_ARITHMETIC",
        "option_return_backtests": 0,
        "optimal_cutoff_estimated": False,
        "source_files": sources,
        "profile": cfg,
        "profile_hash": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "script_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rejection_counts": dict(rejection),
        "summary": summary,
        "ledger": ledger,
        "limitations": [
            "Eight snapshots/two dates; combinations are overlapping, not independent trades.",
            "Assumed standard 100 multiplier; corporate adjustments unverified.",
            "Session-dated quality B quotes have no exchange/concurrency timestamp.",
            "Immediate quote-crossing cost only: no execution or future return.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite evidence")
    report = audit()
    # Decimal ledger values are serialized as exact strings; summaries as floats.
    report["report_hash"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, default=str).encode()
    ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as h:
        json.dump(report, h, indent=2, default=str)
        h.write("\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "summary": report["summary"],
                "report_hash": report["report_hash"],
            }
        )
    )


if __name__ == "__main__":
    main()
