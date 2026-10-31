"""Run exactly the frozen 144 daily proxies; never a web-triggered optimizer."""

from __future__ import annotations

import argparse
from pathlib import Path

from webapp.call_research.offline import run
from webapp.call_research.report import DIRECTORY
from webapp.call_research.settings import settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DIRECTORY)
    args = parser.parse_args()
    result = run(args.output)
    print(
        f"{result['classification']}: {result['replays']} replays, {result['optimization_trials']} optimization trials; {result['report_hash']}"
    )
    for row in result["scores"]:
        if (
            row["window"] == "audit"
            and row["horizon"] == settings().primary_horizon
            and row["cost_bps"] == settings().slippage_bps[0]
        ):
            print(
                row["arm"],
                "closed=",
                row["closed"],
                "net_usd=",
                round(row["net_usd"], 2),
                "SPY excess=",
                round(row["spy_excess_usd"], 2) if row["spy_excess_usd"] is not None else None,
            )


if __name__ == "__main__":
    main()
