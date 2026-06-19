"""Compute the dealer-gamma map per ticker and store it for the screener.

Reads the latest option-chain snapshot per ticker (ThetaData chain), computes
net GEX / flip / call+put walls, and upserts into the ``gamma_regime`` table
the screener reads. Refresh it daily wherever the ThetaData chain is available
(Theta Terminal up). Structural CONTEXT, not a directional signal — see
scripts/study_gamma_regime.py for why.

Usage:
    DATABASE_URL=postgres://...  PYTHONPATH=src .venv/bin/python \\
        scripts/gamma_snapshot.py --chains data/chain_snapshots
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from webapp.gamma import GammaRepo, compute_gamma


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gamma_snapshot")
    p.add_argument("--chains", type=Path, default=Path("data/chain_snapshots"))
    args = p.parse_args(argv)

    repo = GammaRepo()
    done = 0
    for path in sorted(args.chains.glob("*.parquet")):
        ticker = path.stem
        chain = pd.read_parquet(path)
        chain["snapshot_date"] = pd.to_datetime(chain["snapshot_date"])
        latest = chain["snapshot_date"].max()
        snap = chain[chain["snapshot_date"] == latest]
        result = compute_gamma(snap, as_of=str(latest.date()))
        if result is None:
            print(f"{ticker}: no usable chain rows")
            continue
        repo.upsert(ticker, str(latest.date()), result)
        regime = "SHORT (amplify)" if result["net_gex"] < 0 else "long (suppress)"
        print(f"{ticker}: {regime}  spot={result['spot']:.2f}  "
              f"flip={result['flip']}  call_wall={result['call_wall']}  "
              f"put_wall={result['put_wall']}  ({latest.date()})")
        done += 1
    print(f"\nstored {done} tickers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
