"""Harvest dated option chains for the H03 window, resumably.

    uv run python scripts/options_alpha_harvest_chains.py [--start 2026-05-14] [--end 2026-09-15]

One request per (ticker, session) covers everything H03 needs, which is why the
chain is fetched rather than the screener: a single dated chain carries, for every
listed contract, the NBBO used to price an entry or an exit, the greeks used by the
eligibility gates, and the open interest whose change between consecutive sessions
IS the hypothesis. Fetching candidates and prices and OI separately would cost
three times the quota for the same rows.

Idempotent and resumable by construction: a session-ticker already on disk is
skipped, so a killed run is restarted by running it again. Every file records how
many rows the vendor returned before trimming, so a thin day is visible as a thin
day rather than looking like a quiet market.

The only trim is transport-level and deliberately WIDER than the frozen
eligibility gates (DTE 10-70 against the gate's 14-60, and a bid must merely
exist). Trimming at the gate's own thresholds would pre-empt the selection rules
and make the funnel unable to report what it rejected.

Read-only against the vendor. Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from datetime import date
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from webapp.ohlc import regular_session_bars

from uoa_detector.options_alpha.settings import load_settings

BASE = "https://api.unusualwhales.com"
OUT_ROOT = pathlib.Path("artifacts/options-alpha-v1/harvest")

# Transport-level only. Wider than the frozen gates on purpose.
KEEP_MIN_DTE = 10
KEEP_MAX_DTE = 70

# Only the fields the study actually reads. The vendor row carries gamma, theta,
# vega, rho and last_tape_time as well; keeping them multiplied the harvest by
# roughly three for data no rule consults. At ~1 MB per (ticker, session) the full
# window would have been several hundred megabytes of dead weight.
#
# last_tape_time is dropped deliberately rather than carelessly: it is a TRADE
# time, not a quote time, so it cannot date these quotes anyway. The session the
# chain was requested for is the provenance, and M0 verified that dated requests
# really answer with the date they were asked for.
KEEP_FIELDS = (
    "option_symbol",
    "option_type",
    "strike",
    "expires",
    "nbbo_bid",
    "nbbo_ask",
    "open_interest",
    "volume",
    "delta",
    "implied_volatility",
)


def rows_of(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("chains", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def keep_row(row: dict[str, Any], session: date) -> bool:
    expires = row.get("expires")
    if not isinstance(expires, str):
        return False
    try:
        dte = (date.fromisoformat(expires) - session).days
    except ValueError:
        return False
    if not KEEP_MIN_DTE <= dte <= KEEP_MAX_DTE:
        return False
    return row.get("nbbo_bid") is not None


async def sessions_in(
    client: httpx.AsyncClient, start: date, end: date
) -> list[date]:
    """Real trading sessions, taken from SPY's own regular-session bars.

    A weekday list would include market holidays, spend a full day of requests on
    them and store nothing — and the empty result would be indistinguishable from
    a quiet day. The calendar comes from the data instead.
    """
    response = await client.get(f"{BASE}/api/stock/SPY/ohlc/1d")
    response.raise_for_status()
    bars = regular_session_bars(response.json().get("data"))
    return [b.day for b in bars if start <= b.day <= end]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-05-14")
    parser.add_argument("--end", default="2026-09-15")
    parser.add_argument("--tickers", default=None, help="virgullu liste; varsayilan profil evreni")
    args = parser.parse_args()

    key = os.environ.get("UNUSUAL_WHALES_API_KEY", "")
    if not key:
        print("UNUSUAL_WHALES_API_KEY tanimli degil.")
        return 2

    settings = load_settings()
    tickers = (
        [t.strip().upper() for t in args.tickers.split(",")]
        if args.tickers
        else list(settings.universe.tickers)
    )
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    spent = 0
    stored = 0
    skipped = 0
    empty: list[str] = []

    async with httpx.AsyncClient(headers=headers, timeout=90.0) as client:
        sessions = await sessions_in(client, start, end)
        spent += 1
        print(f"{len(sessions)} seans  {sessions[0]} .. {sessions[-1]}")
        print(f"{len(tickers)} isim -> en fazla {len(sessions) * len(tickers)} istek\n")

        for session in sessions:
            day_dir = OUT_ROOT / session.isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            line = [session.isoformat()]
            for ticker in tickers:
                out = day_dir / f"{ticker}.json"
                if out.exists():
                    skipped += 1
                    line.append(f"{ticker}=cache")
                    continue

                response = await client.get(
                    f"{BASE}/api/stock/{ticker}/option-chains",
                    params={"date": session.isoformat(), "greeks": "true"},
                )
                spent += 1
                if response.status_code != 200:
                    line.append(f"{ticker}=HTTP{response.status_code}")
                    empty.append(f"{session} {ticker} HTTP{response.status_code}")
                    continue

                raw = rows_of(response.json())
                kept = [
                    {k: r.get(k) for k in KEEP_FIELDS} for r in raw if keep_row(r, session)
                ]
                out.write_text(
                    json.dumps(
                        {
                            "ticker": ticker,
                            "date": session.isoformat(),
                            "rows_returned": len(raw),
                            "rows_kept": len(kept),
                            "trim": f"DTE {KEEP_MIN_DTE}-{KEEP_MAX_DTE}, bid mevcut",
                            "fields": list(KEEP_FIELDS),
                            "rows": kept,
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                stored += 1
                line.append(f"{ticker}={len(kept)}")
                if not kept:
                    empty.append(f"{session} {ticker} bos")

            print("  " + " ".join(line))

    print(f"\nistek {spent} | yazilan {stored} | onbellekten {skipped}")
    if empty:
        print(f"bos/hatali: {len(empty)}")
        for item in empty[:10]:
            print(f"  {item}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
