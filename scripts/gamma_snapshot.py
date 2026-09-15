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
from webapp.gamma import GammaRepo, atm_iv, compute_gamma


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gamma_snapshot")
    p.add_argument("--chains", type=Path, default=Path("data/chain_snapshots"))
    args = p.parse_args(argv)

    repo = GammaRepo()
    repo.reset()  # full daily rebuild; picks up schema changes
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

        # IV percentile of the latest ATM IV within this ticker's own history.
        hist = [
            v for d, g in chain.groupby("snapshot_date")
            if (v := atm_iv(g, as_of=str(pd.Timestamp(d).date()))) is not None
        ]
        cur = result.get("atm_iv")
        if cur is not None and hist:
            result["iv_pct"] = sum(1 for v in hist if v <= cur) / len(hist)

        repo.upsert(ticker, str(latest.date()), result)
        regime = "SHORT" if result["net_gex"] < 0 else "long"
        ivp = result.get("iv_pct")
        print(f"{ticker}: {regime:5}  spot={result['spot']:.2f}  flip={result['flip']}  "
              f"IV={result.get('atm_iv')}  IVpct={f'{ivp:.0%}' if ivp is not None else '—'}")
        done += 1
    print(f"\nstored {done} tickers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
