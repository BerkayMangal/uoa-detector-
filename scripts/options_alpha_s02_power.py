"""S02's feasibility count: can a defined-risk short ATM iron butterfly be built and
closed on the harvest, often enough to test?

    uv run python scripts/options_alpha_s02_power.py

Runs BEFORE `docs/options-alpha-v1/PREREG_S02.md` is frozen. **Counts only**: no
credit, debit, P&L or outcome is computed anywhere in this file. It checks the
construction rules the pre-registration will freeze -- expiry pick, ATM strike,
wings at 1.5x the expected move -- and whether every leg is quoted on the entry
day and the short legs are quoted on the exit day.

Reads the local harvest and data/study_f/bars.csv. Spends no quota.
"""

from __future__ import annotations

import csv
import json
import math
import pathlib
from collections import Counter
from datetime import date

HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")
BARS = pathlib.Path("data/study_f/bars.csv")
TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "AMD", "GOOGL"]
HOLD = 5
DTE_MIN, DTE_MAX, DTE_TARGET = 21, 45, 30
WING_EM = 1.5


def rows(ticker: str, day: date) -> list[dict]:
    p = HARVEST / day.isoformat() / f"{ticker}.json"
    return json.loads(p.read_text())["rows"] if p.exists() else []


def main() -> None:
    sessions = sorted(date.fromisoformat(p.name) for p in HARVEST.iterdir() if p.is_dir())
    closes: dict[tuple[str, date], float] = {}
    for r in csv.DictReader(BARS.open()):
        closes[(r["ticker"], date.fromisoformat(r["day"]))] = float(r["close"])
    entries = list(range(0, len(sessions) - HOLD, HOLD))
    print(f"seans {len(sessions)}, cakismasiz giris tarihi {len(entries)} (her {HOLD} seansta bir)")
    why: Counter[str] = Counter()
    ok_by_date: Counter[date] = Counter()
    ok = 0
    for i in entries:
        d, x = sessions[i], sessions[i + HOLD]
        for t in TICKERS:
            spot = closes.get((t, d))
            chain = rows(t, d)
            if spot is None or not chain:
                why["zincir/spot yok"] += 1
                continue
            exps = sorted({r["expires"] for r in chain})
            dtes = [(abs((date.fromisoformat(e) - d).days - DTE_TARGET), e) for e in exps
                    if DTE_MIN <= (date.fromisoformat(e) - d).days <= DTE_MAX]
            if not dtes:
                why["21-45 DTE vade yok"] += 1
                continue
            exp = min(dtes)[1]
            leg = {(r["option_type"], float(r["strike"])): r for r in chain if r["expires"] == exp}
            both = sorted({k for (_, k) in leg if ("call", k) in leg and ("put", k) in leg},
                          key=lambda k: (abs(k - spot), k))
            if not both:
                why["ATM cift yok"] += 1
                continue
            k = both[0]
            ivs = [float(leg[(s, k)]["implied_volatility"] or 0) for s in ("call", "put")]
            iv = sum(ivs) / 2
            if iv <= 0:
                why["ATM IV yok"] += 1
                continue
            dte = (date.fromisoformat(exp) - d).days
            em = spot * iv * math.sqrt(dte / 365)
            calls = sorted(s for (o, s) in leg if o == "call" and s >= k + WING_EM * em)
            puts = sorted(s for (o, s) in leg if o == "put" and s <= k - WING_EM * em)
            if not calls or not puts:
                why["kanat strike'i yok"] += 1
                continue
            need = [("call", k), ("put", k), ("call", calls[0]), ("put", puts[-1])]
            if any(leg[n].get("nbbo_ask") in (None, "") for n in need):
                why["giriste ask eksik"] += 1
                continue
            xrows = {(r["option_type"], float(r["strike"])): r for r in rows(t, x) if r["expires"] == exp}
            if ("call", k) not in xrows or ("put", k) not in xrows:
                why["cikista kisa bacak kotasyonu yok"] += 1
                continue
            ok += 1
            ok_by_date[d] += 1
    print(f"kurulabilir + cikisi fiyatlanabilir: {ok}  / denenen {len(entries) * len(TICKERS)}")
    print(f"giris tarihi basina: min {min(ok_by_date.values())} maks {max(ok_by_date.values())} "
          f"tarih sayisi {len(ok_by_date)}")
    print("dusme nedenleri:", dict(why))


if __name__ == "__main__":
    main()
