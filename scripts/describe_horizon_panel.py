"""Post-hoc descriptive risk/concentration worksheet; never selects a policy."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

from webapp.history_capture.capture import digest
from webapp.horizon_research.model import settings
from webapp.horizon_research.report import read


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any(part.casefold().replace("-", "_").startswith("study_g")
           for path in (args.report.resolve(), args.output.resolve()) for part in path.parts):
        parser.error("reserved evidence path")
    if args.output.exists():
        parser.error("refusing to overwrite descriptive evidence")
    report = read(args.report, ledger=True)
    cfg = settings()
    excluded = set(report["primary_family"]["excluded_signal_keys"])
    rows = []
    horizons = [f"m{h}" for h in cfg.intraday_horizons_minutes] + [f"c{h}" for h in cfg.session_close_offsets]
    for horizon in horizons:
        for cost in cfg.costs_bps_per_side:
            units = [u for u in report["units"] if u["arm"] == cfg.primary_arm
                     and u["delay_minutes"] == cfg.primary_delay_minutes
                     and u["signal_key"] not in excluded and u["horizon"] == horizon
                     and u["cost_bps_per_side"] == cost]
            if len(units) != report["primary_family"]["administrative_roster"] or any(u["missing"] for u in units):
                raise ValueError("post-hoc panel must retain the exact complete administrative roster")
            names = defaultdict(lambda: {"count": 0, "net_usd": 0.0, "excess_usd": 0.0})
            for unit in units:
                names[unit["ticker"]]["count"] += 1
                names[unit["ticker"]]["net_usd"] += unit["net_usd"]
                names[unit["ticker"]]["excess_usd"] += unit["excess_usd"]
            net = sum(u["net_usd"] for u in units)
            excess = sum(u["excess_usd"] for u in units)
            net_wins = sorted((u["net_usd"] for u in units if u["net_usd"] > 0), reverse=True)
            excess_wins = sorted((u["excess_usd"] for u in units if u["excess_usd"] > 0), reverse=True)
            mfe = [u["gross_underlying_mfe_pct"] for u in units if u["gross_underlying_mfe_pct"] is not None]
            mae = [u["gross_underlying_mae_pct"] for u in units if u["gross_underlying_mae_pct"] is not None]
            rows.append({"horizon": horizon, "cost_bps_per_side": cost, "reference_units": len(units),
                         "net_usd": net, "spy_excess_usd": excess,
                         "price_cost_usd": sum(u["cost_usd"] for u in units),
                         "without_best_net_1_usd": net - sum(net_wins[:1]),
                         "without_best_net_3_usd": net - sum(net_wins[:3]),
                         "without_best_excess_1_usd": excess - sum(excess_wins[:1]),
                         "without_best_excess_3_usd": excess - sum(excess_wins[:3]),
                         "name_contributions": dict(sorted(names.items())),
                         "gross_mfe_observed": len(mfe), "gross_mae_observed": len(mae),
                         "gross_mfe_mean_pct": mean(mfe) if mfe else None,
                         "gross_mae_mean_pct": mean(mae) if mae else None,
                         "gross_mfe_median_pct": median(mfe) if mfe else None,
                         "gross_mae_median_pct": median(mae) if mae else None,
                         "worst_observed_gross_mae_pct": min(mae) if mae else None})
    result = {"classification": "POSTHOC_DESCRIPTIVE_CONCENTRATION_NOT_NEW_VALIDATION",
              "report_hash": report["report_hash"], "audit_hash": report["audit"]["audit_hash"],
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "winner": None, "validated_alpha": False, "rows": rows,
              "limitations": ["No replacement or capital reallocation after winner removal.",
                              "Separate best-net and best-excess deletion lists; no retuned rule.",
                              "Gross excursions are path diagnostics, not executable stops or account drawdown.",
                              "Post-hoc view of reused data, not another independent validation."]}
    result["description_hash"] = digest(result)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"description_hash": result["description_hash"], "rows": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
