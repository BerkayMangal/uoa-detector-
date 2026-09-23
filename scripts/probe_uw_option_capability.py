"""Measure what the Unusual Whales account can actually do for OPTION research.

    uv run python scripts/probe_uw_option_capability.py [--out PATH]

Why this exists. The options-alpha-v1 scope depends on questions that no document
can answer: whether a dated option chain is genuinely historical, how far back it
goes, whether per-contract NBBO history exists, and whether the contract screener
honours a date. A vendor doc describing a feature is not evidence the account can
reach it, and a 200 is not evidence the payload belongs to the date requested.

So this asks, and writes a machine-readable capability matrix. Every row carries
a verdict:

    VERIFIED    measured working, and the payload matched what was asked for
    PARTIAL     works, but with a limitation that changes how it may be used
    DENIED      the account is refused (HTTP 401/403)
    UNAVAILABLE the route does not exist (HTTP 404)
    UNVERIFIED  not probed in this run

Two traps this script is built to avoid, both hit during the first manual pass:

1. An envelope bug reads as a vendor outage. ``/historic`` returns its rows under
   ``chains``, not ``data``. Reading only ``data`` made a working endpoint look
   empty, and recording that as a provider blocker would have been a fabricated
   external dependency. :func:`rows_of` uses the same envelope rule the production
   parser uses (``webapp/board/outcome_job.py``).
2. An ignored parameter reads as historical data. ``date=`` is only believed when
   two different dates return different content, so :func:`_chain_is_historical`
   compares the same contract across two sessions instead of trusting row counts.

Read-only: GETs only, writes nothing but the output file. Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx

BASE = "https://api.unusualwhales.com"

# Two completed sessions a week apart. The pair is the whole point: one date alone
# cannot distinguish a historical endpoint from one that ignores its date param.
SESSION_A = "2026-09-22"
SESSION_B = "2026-09-15"

# Walked outward from the known-good side to find the oldest session that answers.
FLOOR_LADDER = ("2026-08-10", "2026-06-26", "2026-05-19", "2026-05-13", "2026-05-12", "2026-04-02")

TICKERS = ("SPY", "AAPL", "NVDA")


def rows_of(payload: Any) -> tuple[list[Any], str]:
    """Rows plus the envelope key they came from.

    ``chains`` before ``data`` — the order the production parser uses. Getting this
    wrong is what made a working endpoint look like an outage.
    """
    if isinstance(payload, dict):
        for key in ("chains", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, key
    if isinstance(payload, list):
        return payload, "root"
    return [], "none"


def dict_rows(payload: Any) -> list[dict[str, Any]]:
    rows, _ = rows_of(payload)
    return [r for r in rows if isinstance(r, dict)]


def tape_days(rows: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(r.get("last_tape_time"))[:10] for r in rows)


class Probe:
    """One HTTP client plus the quota headers the vendor returns on every call."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.requests = 0
        self.daily_count: str | None = None
        self.daily_limit: str | None = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        self.requests += 1
        try:
            response = await self._client.get(BASE + path, params=params or {})
        except Exception as error:  # a probe reports a failure, it does not raise
            return 0, f"ERR {type(error).__name__}"
        # The account's own budget, straight from the vendor rather than assumed.
        self.daily_count = response.headers.get("x-uw-daily-req-count", self.daily_count)
        self.daily_limit = response.headers.get("x-uw-token-req-limit", self.daily_limit)
        if response.status_code != 200:
            return response.status_code, response.text[:200]
        try:
            return 200, response.json()
        except ValueError:
            return 200, None


def verdict_for(status: int) -> str:
    if status in (401, 403):
        return "DENIED"
    if status == 404:
        return "UNAVAILABLE"
    if status != 200:
        return "PARTIAL"
    return "VERIFIED"


async def _chain_is_historical(probe: Probe, ticker: str) -> dict[str, Any]:
    """Does ``date=`` change the CONTENT, not just the row count?

    Row counts differ for boring reasons (expiries roll off), so the test is the
    same contract on two dates: if its bid and delta move, the endpoint is really
    serving that session.
    """
    chains: dict[str, dict[str, dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for day in (SESSION_A, SESSION_B):
        status, payload = await probe.get(
            f"/api/stock/{ticker}/option-chains", {"date": day, "greeks": "true"}
        )
        if status != 200:
            return {"verdict": verdict_for(status), "detail": f"{day} -> HTTP {status}"}
        rows = dict_rows(payload)
        counts[day] = len(rows)
        chains[day] = {r["option_symbol"]: r for r in rows if r.get("option_symbol")}
        days = tape_days(rows)
        if days and days.most_common(1)[0][0] != day:
            return {
                "verdict": "PARTIAL",
                "detail": f"{day} istendi, {days.most_common(1)[0][0]} geldi",
            }

    common = set(chains[SESSION_A]) & set(chains[SESSION_B])
    moved = sum(
        1
        for sym in common
        if (chains[SESSION_A][sym].get("nbbo_bid"), chains[SESSION_A][sym].get("delta"))
        != (chains[SESSION_B][sym].get("nbbo_bid"), chains[SESSION_B][sym].get("delta"))
    )
    return {
        "verdict": "VERIFIED" if moved > len(common) // 2 else "PARTIAL",
        "rows": counts,
        "common_contracts": len(common),
        "values_moved": moved,
        "values_identical": len(common) - moved,
        "detail": (
            f"{moved}/{len(common)} kontratin degeri iki tarih arasinda degisti"
            if moved
            else "degerler birebir ayni: date= kozmetik olabilir"
        ),
    }


async def _chain_floor(probe: Probe) -> dict[str, Any]:
    """Oldest session the chain answers for, and the first one it refuses."""
    reached: list[str] = []
    refused: list[str] = []
    for day in FLOOR_LADDER:
        status, payload = await probe.get(
            "/api/stock/SPY/option-chains", {"date": day, "greeks": "true"}
        )
        if status == 200 and dict_rows(payload):
            reached.append(day)
        else:
            refused.append(f"{day}:{status}")
    return {
        "verdict": "VERIFIED" if reached else "DENIED",
        "oldest_reached": min(reached) if reached else None,
        "refused": refused,
        "detail": "en eski cevaplanan seans" if reached else "hicbir tarih cevaplanmadi",
    }


async def _contract_history(probe: Probe) -> dict[str, Any]:
    """Per-contract daily NBBO series — the spine of any option exit pricing."""
    status, payload = await probe.get(
        "/api/screener/option-contracts",
        {
            "ticker_symbol": "SPY",
            "date": SESSION_A,
            "min_dte": 14,
            "max_dte": 60,
            "min_volume": 500,
            "order": "volume",
            "order_direction": "desc",
            "limit": 3,
        },
    )
    if status != 200:
        return {"verdict": verdict_for(status), "detail": f"aday bulunamadi: HTTP {status}"}
    candidates = [r.get("option_symbol") for r in dict_rows(payload) if r.get("option_symbol")]
    for symbol in candidates:
        status, payload = await probe.get(f"/api/option-contract/{symbol}/historic")
        rows = dict_rows(payload)
        if not rows:
            continue
        keys = sorted(rows[0])
        days = sorted(str(r["date"]) for r in rows if r.get("date"))
        has_nbbo = "nbbo_bid" in keys and "nbbo_ask" in keys
        return {
            "verdict": "VERIFIED" if has_nbbo else "PARTIAL",
            "sample_contract": symbol,
            "rows": len(rows),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "has_nbbo_bid_ask": has_nbbo,
            "fields": keys,
            "detail": "gunluk NBBO serisi; zarf 'chains'",
        }
    return {"verdict": "PARTIAL", "detail": f"{len(candidates)} adayda satir yok"}


async def _screener_date(probe: Probe) -> dict[str, Any]:
    """The screener carries a date param; believe it only if the results differ."""
    seen: dict[str, list[str]] = {}
    for day in (SESSION_A, "2026-06-26"):
        status, payload = await probe.get(
            "/api/screener/option-contracts",
            {"ticker_symbol": "SPY", "unusual": "true", "limit": 5, "date": day},
        )
        if status != 200:
            return {"verdict": verdict_for(status), "detail": f"{day} -> HTTP {status}"}
        seen[day] = [r.get("option_symbol") for r in dict_rows(payload)]
    differs = seen[SESSION_A] != seen["2026-06-26"]
    return {
        "verdict": "VERIFIED" if differs else "PARTIAL",
        "sample": seen,
        "detail": "date= farkli sonuc veriyor" if differs else "date= yok sayiliyor gorunuyor",
    }


async def _simple(probe: Probe, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    status, payload = await probe.get(path, params)
    rows = dict_rows(payload)
    out: dict[str, Any] = {"verdict": verdict_for(status), "rows": len(rows)}
    if status != 200:
        out["detail"] = str(payload)[:160]
    elif rows:
        out["fields"] = sorted(rows[0])
    return out


async def run() -> dict[str, Any]:
    key = os.environ.get("UNUSUAL_WHALES_API_KEY", "")
    if not key:
        raise SystemExit("UNUSUAL_WHALES_API_KEY tanimli degil.")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        probe = Probe(client)
        matrix: dict[str, Any] = {}

        matrix["option_chains_dated"] = await _chain_is_historical(probe, "SPY")
        matrix["option_chains_floor"] = await _chain_floor(probe)

        breadth: dict[str, Any] = {}
        for ticker in TICKERS:
            status, payload = await probe.get(
                f"/api/stock/{ticker}/option-chains", {"date": SESSION_A, "greeks": "true"}
            )
            rows = dict_rows(payload)
            days = tape_days(rows)
            breadth[ticker] = {
                "status": status,
                "rows": len(rows),
                "day": days.most_common(1)[0][0] if days else None,
            }
        matrix["option_chains_breadth"] = {
            "verdict": "VERIFIED"
            if all(v["status"] == 200 and v["day"] == SESSION_A for v in breadth.values())
            else "PARTIAL",
            "by_ticker": breadth,
        }

        matrix["contract_historic"] = await _contract_history(probe)
        matrix["contract_intraday"] = await _simple(
            probe, "/api/option-contract/SPY261016C00785000/intraday", {"date": SESSION_A}
        )
        matrix["screener_option_contracts"] = await _screener_date(probe)
        matrix["multi_leg"] = await _simple(
            probe, "/api/option-trades/multi-leg", {"ticker_symbol": "SPY", "limit": 5}
        )
        matrix["expiry_breakdown"] = await _simple(
            probe, "/api/stock/SPY/expiry-breakdown", {"date": SESSION_A}
        )
        matrix["volume_profile"] = await _simple(
            probe, "/api/option-contract/SPY261016C00785000/volume-profile"
        )

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "sessions_used": {"a": SESSION_A, "b": SESSION_B},
            "requests_spent": probe.requests,
            "quota": {"daily_count": probe.daily_count, "daily_limit": probe.daily_limit},
            "matrix": matrix,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/options-alpha-v1/capability_matrix.json")
    args = parser.parse_args()

    report = asyncio.run(run())
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"istek: {report['requests_spent']}  kota: {report['quota']}")
    for name, row in report["matrix"].items():
        detail = row.get("detail", "")
        print(f"  {row['verdict']:<11} {name:<28} {detail}")
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
