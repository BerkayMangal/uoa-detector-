"""Measure how far back Unusual Whales answers a dated request, and whether it
answers with the date that was asked for.

    uv run python scripts/probe_uw_retroactive_window.py [TICKER]

Why this exists. `docs/phase-3.5.5-status.md` B1 recorded, from a probe on
2026-05-18, that "the earliest date currently available to you is 2026-05-07
(7 trading days)". That reading became the canonical clause in `docs/INDEX.md`
section 0 -- "UW-fed Track B has never been testable, because UW history is
about 7 days" -- and it is load-bearing: it is the reason the project's own
central thesis was never run.

A single reading cannot distinguish a rolling 7-day window from a fixed floor
that happens to sit 7 days back on the day it was read. Those two have opposite
consequences, and the difference is one cheap measurement. This script is that
measurement, written so the answer can be re-derived rather than remembered.

Two things are checked, because the first is worthless without the second:

  1. Does a dated request SUCCEED for a past session?
  2. Does the payload carry the date that was ASKED FOR?

(2) matters more than (1). `_decode_spot_rows` applies no date check
(`sources/unusual_whales/providers/dealer_gamma.py`), unlike its sibling
`_decode_strike_rows`, so a vendor that silently ignored `date=` would hand back
today's rows and any capture built on it would file today's data under a past
session -- undetectable a year later, when the rows look perfectly healthy.

Read-only: it issues GETs and writes nothing. It never prints the API key.
Costs roughly one request per (date, endpoint) pair asked for.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

import httpx

BASE = "https://api.unusualwhales.com"
ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

# Sessions walking back from 2026-09-21. Good Friday 2026 is April 3, so April 2
# is a session; Labor Day is September 7, so September 9 is used instead.
LADDER = (
    ("~1 seans", "2026-09-21"),
    ("~8 seans", "2026-09-09"),
    ("~15 seans", "2026-08-31"),
    ("~30 seans", "2026-08-10"),
    ("~60 seans", "2026-06-26"),
    ("~95 seans", "2026-05-12"),
    ("taban alti", "2026-05-11"),
    ("~120 seans", "2026-04-02"),
)


def days_in(payload: object) -> tuple[list[str], int]:
    """Every YYYY-MM-DD the payload's own rows carry, and the row count.

    The vendor's date field is not named consistently across these endpoints
    (`time`, `executed_at`, `start_time`, `date`), so rather than hardcode a
    field per endpoint -- which would silently return "no dates" the moment a
    field is renamed -- this reads every string value and keeps what parses as
    a leading ISO day. Over-collecting is safe here: the question is only
    whether the payload's days match the day that was requested.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        return [], 0
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            if isinstance(value, str):
                match = ISO_DAY.match(value)
                if match:
                    found.add(match.group(0))
    return sorted(found), len(rows)


async def probe(
    client: httpx.AsyncClient,
    label: str,
    want: str | None,
    path: str,
    params: dict[str, object],
) -> str:
    """One request. ``want`` is the day asked for, or None for the no-date control.

    The control has no requested day, so it is reported rather than judged;
    comparing it against a placeholder string made it raise the wrong-date alarm
    on every run, in the one script whose whole purpose is to tell those apart.
    """
    shown = want if want is not None else "(date yok)"
    try:
        response = await client.get(BASE + path, params=params)
    except Exception as error:  # a probe reports a failure, it does not raise
        return f"{label:24s} {shown}  HATA {type(error).__name__}"
    if response.status_code != 200:
        return f"{label:24s} {shown}  HTTP {response.status_code}  <- pencere disi"
    try:
        days, count = days_in(response.json())
    except ValueError:
        return f"{label:24s} {shown}  HTTP 200, JSON ayristirilamadi"
    if not days:
        return f"{label:24s} {shown}  HTTP 200 satir={count} (tarih alani yok)"
    # The whole point of the probe: a 200 that carries the WRONG day is worse
    # than a 403, because it looks like a successful capture.
    if want is None:
        return f"{label:24s} {shown}  satir={count:<6} donen gun: {days[0]}..{days[-1]}"
    verdict = "DOGRU" if days[0] == want else f"!! ISTENEN DEGIL: {days[0]}..{days[-1]}"
    return f"{label:24s} {shown}  satir={count:<6} {verdict}"


async def main() -> int:
    key = os.environ.get("UNUSUAL_WHALES_API_KEY", "")
    if not key:
        print("UNUSUAL_WHALES_API_KEY tanimli degil.")
        return 2
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    lines = [f"ticker: {ticker}", "", "--- 1. spot-exposures, geriye dogru ---"]
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        for label, day in LADDER:
            lines.append(
                await probe(
                    client, label, day, f"/api/stock/{ticker}/spot-exposures", {"date": day}
                )
            )

        deep = "2026-06-26"
        lines.append("")
        lines.append(f"--- 2. diger eksenler ayni derinlikte mi ({deep}) ---")
        lines.append(
            await probe(
                client,
                "greek-exposure/strike",
                deep,
                f"/api/stock/{ticker}/greek-exposure/strike",
                {"date": deep},
            )
        )
        lines.append(
            await probe(client, "ohlc/1m", deep, f"/api/stock/{ticker}/ohlc/1m", {"date": deep})
        )
        lines.append(
            await probe(
                client, "darkpool", deep, f"/api/darkpool/{ticker}", {"date": deep, "limit": 50}
            )
        )
        lines.append(
            await probe(
                client,
                "flow-alerts",
                deep,
                "/api/option-trades/flow-alerts",
                {
                    "ticker_symbol": ticker,
                    "limit": 50,
                    "newer_than": f"{deep}T13:30:00Z",
                    "older_than": f"{deep}T20:00:00Z",
                },
            )
        )

        lines.append("")
        lines.append("--- 3. kontrol: date parametresi yok ---")
        lines.append(
            await probe(
                client, "spot-exposures", None, f"/api/stock/{ticker}/spot-exposures", {}
            )
        )

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
