"""Reconcile immutable historical ledgers to private source marks, independently."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


def checksum(body: object) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def equal(actual: float, expected: float) -> None:
    require(
        math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-7), "ledger arithmetic mismatch"
    )


def audit(source: Path, original: Path) -> dict:
    report = json.loads(original.read_text())
    saved = report.pop("report_hash")
    require(checksum(report) == saved, "diagnostic checksum")
    require(
        report["script_hash"]
        == hashlib.sha256((ROOT / "scripts/diagnose_vendor_history.py").read_bytes()).hexdigest(),
        "diagnostic implementation changed",
    )
    cfg = YAML(typ="safe").load((ROOT / "profiles/history_diagnostic_v1.yaml").read_text())
    require(checksum(cfg) == report["profile_hash"], "diagnostic profile changed")
    state = json.loads((source / "state.json").read_text())
    require(checksum(state) == report["source_state_hash"], "source state changed")
    for name, expected in report["dependency_hashes"].items():
        require(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, "dependency changed"
        )
    packets = {}
    counts = Counter()
    hashes = {}
    for path in sorted(source.rglob("*.json.gz")):
        packet = json.loads(gzip.decompress(path.read_bytes()))
        expected = packet.pop("packet_hash")
        require(checksum(packet) == expected, "source checksum")
        require(
            packet["complete"] and packet["profile_hash"] == state["profile_hash"],
            "source identity",
        )
        relative = str(path.relative_to(source))
        if relative in report["source_packet_hashes"]:
            require(
                expected == report["source_packet_hashes"][relative], "diagnostic source mismatch"
            )
        hashes[relative] = expected
        packets[relative] = packet
        kind = packet["kind"]
        data = packet["payload"].get("data", packet["payload"].get("chains", []))
        counts[kind + "_rows"] += len(data)
        require(
            datetime.fromisoformat(packet["received_at"]).date().isoformat()
            > state["profile"]["end"],
            "historical receipt was backdated",
        )
        if kind == "flow":
            require(all(r["ticker"] == packet["ticker"] for r in data), "flow ticker contamination")
    require(set(report["source_packet_hashes"]) <= set(packets), "source absent")
    price_cache = {}

    def price(ticker: str, day: str, stamp: str) -> float:
        key = ticker, day
        if key not in price_cache:
            packet = packets[f"{day}/{ticker}-minute.json.gz"]
            require(packet["ticker"] == ticker and packet["day"] == day, "minute identity")
            points = {}
            for item in packet["payload"]["data"]:
                at = datetime.fromisoformat(
                    item.get("start_time", item.get("time")).replace("Z", "+00:00")
                )
                opening = float(item["open"])
                require(at not in points or points[at] == opening, "conflicting minute")
                points[at] = opening
            price_cache[key] = points
        return price_cache[key][datetime.fromisoformat(stamp)]

    output = []
    total_units = 0
    selections = {}
    for row in report["rows"]:
        units = row["ledger"]
        require(row["unresolved"] == len(row["missing"]) == 0, "unresolved ledger")
        require(row["selected"] == row["resolved"] == len(units), "ledger count")
        selected = [(u["ticker"], u["day"], u["signal_at"]) for u in units]
        require(selections.setdefault(row["arm"], selected) == selected, "cost changed selection")
        require(len({(u["ticker"], u["day"]) for u in units}) == len(units), "duplicate ticker/day")
        require(
            max(Counter(u["day"] for u in units).values(), default=0) <= cfg["max_units_per_day"],
            "daily limit",
        )
        nets = []
        excess = []
        gross = []
        costs = []
        slip = row["bps_per_side"] / 10000
        for unit in units:
            signal, entry, exit_ = (
                datetime.fromisoformat(unit[k]) for k in ("signal_at", "entry_at", "exit_at")
            )
            require(
                signal + timedelta(minutes=cfg["delay_minutes"])
                < entry
                <= signal + timedelta(minutes=cfg["delay_minutes"] + cfg["mark_tolerance_minutes"]),
                "entry delay",
            )
            require(exit_ - entry == timedelta(minutes=cfg["hold_minutes"]), "holding horizon")
            require(
                state["profile"]["start"] <= unit["day"] <= state["profile"]["end"],
                "source date bounds",
            )
            opening, closing = (
                price(unit["ticker"], unit["day"], unit[k]) for k in ("entry_at", "exit_at")
            )
            q = unit["quantity"]
            require(isinstance(q, int) and q > 0, "whole-share quantity")
            require(
                q == int(cfg["unit_budget_usd"] // (opening * (1 + slip))), "whole-share sizing"
            )
            debit = q * opening * (1 + slip)
            require(debit <= cfg["unit_budget_usd"] + 1e-7, "unit budget")
            net = q * (closing * (1 - slip) - opening * (1 + slip))
            benchmark_entry, benchmark_exit = (
                price(state["profile"]["benchmark"], unit["day"], unit[k])
                for k in ("entry_at", "exit_at")
            )
            reference = debit * (benchmark_exit * (1 - slip) / (benchmark_entry * (1 + slip)) - 1)
            equal(unit["debit"], debit)
            equal(unit["net_usd"], net)
            equal(unit["spy_matched_usd"], reference)
            equal(unit["excess_usd"], net - reference)
            equal(unit["return_pct"], net / debit * 100)
            nets.append(net)
            excess.append(net - reference)
            gross.append(q * (closing - opening))
            costs.append(q * slip * (opening + closing))
        total_units += len(units)
        equal(row["net_unit_usd"], math.fsum(nets))
        equal(row["net_known_unit_usd"], math.fsum(nets))
        equal(row["spy_excess_known_unit_usd"], math.fsum(excess))
        ordered = sorted(nets, reverse=True)
        equal(row["without_best_1_usd"], math.fsum(ordered[1:]))
        equal(row["without_best_3_usd"], math.fsum(ordered[3:]))
        equal(math.fsum(nets), math.fsum(gross) - math.fsum(costs))
        output.append(
            {
                "arm": row["arm"],
                "bps_per_side": row["bps_per_side"],
                "units": len(units),
                "gross_fixed_unit_usd": math.fsum(gross),
                "modeled_price_cost_usd": math.fsum(costs),
                "net_unit_usd": math.fsum(nets),
            }
        )
    require(len(output) == report["executed_fixed_replays"] == 12, "replay count")
    return {
        "status": "RECONCILED",
        "original_report_hash": saved,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_state_hash": report["source_state_hash"],
        "source_manifest_hash": checksum(hashes),
        "source_start": state["profile"]["start"],
        "source_end": state["profile"]["end"],
        "tickers": state["profile"]["tickers"],
        "benchmark": state["profile"]["benchmark"],
        "verified_packets": len(packets),
        "verified_reference_units_all_costs": total_units,
        "source_row_counts": dict(counts),
        "capture_runs": state["previous_runs"],
        "resume_http_calls": state["calls_this_run"],
        "resumed_packets": state["resumed"],
        "rows": output,
        "interpretation": "Algebraic attribution of original fixed units, not a new zero-cost selection or cash portfolio replay.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.source, args.diagnostic)
    result["audit_hash"] = checksum(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "status",
                    "verified_packets",
                    "verified_reference_units_all_costs",
                    "audit_hash",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
