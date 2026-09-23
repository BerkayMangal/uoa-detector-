"""H10's power analysis: are the cheap/rich premium arms testable, and separable?

    uv run python scripts/options_alpha_h10_power.py

Runs BEFORE `docs/options-alpha-v1/PREREG_H10.md` is frozen. Two families have now
been shaped by this step: H04's draft bands would have left one arm at roughly
fifteen records against a floor of thirty, and H06 was refused a freeze outright
because its measure could not be separated from the calendar. The same question is
asked here before anything is frozen.

**It computes counts only.** No P&L, no exit, no outcome is evaluated anywhere in
this file.

What it measures. For each (ticker, session D), eligible contracts are ranked by how
cheap their implied vol looks against the underlying's own recent realised vol:

    cheapness = IV(contract, D) / realised_vol20(underlying, D)
    realised_vol20 = stdev of the 20 daily returns BEFORE D, annualised

An arm needs both a population and a defensible one. So alongside the counts this
prints the composition of each arm -- mean IV, mean spread as a percent of mid, mean
DTE, mean delta -- because section 7 of the draft requires that diagnostic and
requires seeing it BEFORE the freeze. If the cheap arm is systematically the
wider-spread arm, the family cannot separate cheapness from transaction cost, and the
honest move is to refuse the freeze the way H06 was refused rather than to run it and
report a number nobody can interpret.

**Known gap, found by the run rather than before it (2026-09-23).** The projections
below multiply arm-assigned ticker-sessions by the risk-gate survival rate alone, and
that rate was right -- 73 of 309 structures cleared it, 23.6% against the 23% assumed.
What is missing is the attrition BETWEEN the anchor and the record: at entry the anchor
must still be listed, still carry a delta, and fall inside both the DTE 14-60 and the
|delta| 0.25-0.70 buckets. H10 projected 77.7 and 51.2 records and got 45 and 28, so
the end-to-end rate was about 13%, not 23%, and arm B missed the floor of 30 by two.

The computation here is deliberately NOT changed: `PREREG_H10.md` section 4b cites this
script's table and it has to stay reproducible. The next family's power analysis should
model the bucket attrition as well. See `RESULT_H10.md` section 3.

Reads the local harvest plus the committed data/study_f/bars.csv. Spends no quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.selection import eligible_contracts, parse_chain_row
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings

DEFAULT_HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")
DEFAULT_BARS = pathlib.Path("data/study_f/bars.csv")

TRAILING_SESSIONS = 20
TRADING_DAYS = 252
RISK_GATE_SURVIVAL = 202 / 879  # measured in H01's funnel
SAMPLE_FLOOR = 30

# Declared before the run so the published table is the whole search.
CANDIDATE_PAIRS: tuple[tuple[float, float], ...] = (
    (0.90, 1.20),
    (0.95, 1.15),
    (0.90, 1.10),
    (1.00, 1.30),
    (0.85, 1.25),
    (1.00, 1.20),
    (0.95, 1.25),
    (0.80, 1.20),
)


class Candidate(NamedTuple):
    ticker: str
    session: date
    symbol: str
    cheapness: float
    iv: float
    spread_pct: float
    dte: int
    delta: float
    volume: int


def load_closes(path: pathlib.Path, tickers: list[str]) -> dict[str, dict[date, float]]:
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["ticker"] in tickers and row["close"]:
                closes[row["ticker"]][date.fromisoformat(row["day"])] = float(row["close"])
    return closes


def realised_vol(closes: dict[date, float], day: date) -> float | None:
    """Annualised stdev of the 20 returns strictly BEFORE `day`.

    The day's own return is excluded for the same reason H04 excluded it from sigma:
    a window that contains the move it is scaling shrinks exactly when the move is
    large, which quietly flatters whichever arm the move landed in.
    """
    days = sorted(d for d in closes if d <= day)
    if len(days) < TRAILING_SESSIONS + 2 or days[-1] != day:
        return None
    series = [closes[d] for d in days[-(TRAILING_SESSIONS + 2) :]]
    returns = [series[i] / series[i - 1] - 1 for i in range(1, len(series))]
    sigma = statistics.stdev(returns[:-1])
    return None if sigma == 0 else sigma * math.sqrt(TRADING_DAYS)


def spread_pct_of_mid(bid: Decimal | None, ask: Decimal | None) -> float | None:
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / Decimal(2)
    if mid <= 0:
        return None
    return float((ask - bid) / mid) * 100.0


def collect(
    harvest: pathlib.Path,
    closes: dict[str, dict[date, float]],
    tickers: list[str],
    settings: OptionsAlphaSettings,
) -> list[Candidate]:
    sessions = sorted(date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir())
    out: list[Candidate] = []
    # The tail is trimmed by the hold window the study would need after entry.
    for session in sessions[: len(sessions) - 6]:
        for ticker in tickers:
            path = harvest / session.isoformat() / f"{ticker}.json"
            if not path.exists():
                continue
            vol = realised_vol(closes.get(ticker, {}), session)
            if vol is None:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            quotes = [
                q
                for q in (parse_chain_row(r, session, ticker) for r in payload["rows"])
                if q is not None
            ]
            kept, _ = eligible_contracts(quotes, session, settings)
            for quote in kept:
                if quote.implied_volatility is None or quote.delta is None:
                    continue
                if quote.volume is None:
                    continue
                spread = spread_pct_of_mid(quote.bid, quote.ask)
                if spread is None:
                    continue
                out.append(
                    Candidate(
                        ticker=ticker,
                        session=session,
                        symbol=quote.option_symbol,
                        cheapness=quote.implied_volatility / vol,
                        iv=quote.implied_volatility,
                        spread_pct=spread,
                        dte=quote.dte(session),
                        delta=abs(quote.delta),
                        volume=quote.volume,
                    )
                )
    return out


def describe(rows: list[Candidate]) -> str:
    if not rows:
        return "-"
    return (
        f"IV {statistics.fmean(r.iv for r in rows):.2f} "
        f"makas %{statistics.fmean(r.spread_pct for r in rows):.1f} "
        f"DTE {statistics.fmean(r.dte for r in rows):.0f} "
        f"delta {statistics.fmean(r.delta for r in rows):.2f}"
    )


def sweep_session_level(candidates: list[Candidate]) -> list[tuple[float, float, int, int]]:
    """Arms assigned at (ticker, session) level, not contract level.

    The paired table below showed why this exists. Cheapness divides IV by the
    UNDERLYING's realised vol, and that denominator is identical for every contract
    of a ticker-session, so skew and term move the ratio only a little while the
    session-to-session variation is large. A ticker-session therefore lands almost
    entirely on one side of any cheap/rich line, and requiring both sides inside one
    session left 68 pairs at best against a floor of thirty.

    So cheapness is effectively a session-level variable, exactly as H04's z was, and
    the honest fix is to assign the arm at that level. The cost is real and named: the
    same-name same-day control is gone, and market-day effects are no longer
    differenced out. The session's representative is its highest-volume eligible
    contract, which is the anchor the study would trade anyway.
    """
    by_session: dict[tuple[str, date], Candidate] = {}
    for candidate in candidates:
        key = (candidate.ticker, candidate.session)
        held = by_session.get(key)
        if held is None or candidate.volume > held.volume:
            by_session[key] = candidate
    representatives = list(by_session.values())

    ratios = sorted(c.cheapness for c in representatives)
    quantiles = "  ".join(
        f"%{p}={ratios[int(p / 100 * (len(ratios) - 1))]:.2f}"
        for p in (10, 25, 50, 75, 90)
    )
    print()
    print("=== SEANS DUZEYINDE KOL ATAMASI (eslemesiz) ===")
    print(f"temsilci seans-isim: {len(representatives)} | ucuzluk ceyrekleri: {quantiles}")
    header = (
        f"{'A<=':>6} {'B>=':>6} | {'A c-s':>6} {'B c-s':>6} | "
        f"{'~A kyt':>7} {'~B kyt':>7} | {'makas A-B':>10} | taban {SAMPLE_FLOOR}"
    )
    print(header)
    print("-" * len(header))

    viable: list[tuple[float, float, int, int]] = []
    for low, high in CANDIDATE_PAIRS:
        arm_a = [c for c in representatives if c.cheapness <= low]
        arm_b = [c for c in representatives if c.cheapness >= high]
        projected_a = len(arm_a) * RISK_GATE_SURVIVAL
        projected_b = len(arm_b) * RISK_GATE_SURVIVAL
        gap = (
            statistics.fmean(c.spread_pct for c in arm_a)
            - statistics.fmean(c.spread_pct for c in arm_b)
            if arm_a and arm_b
            else float("nan")
        )
        holds = projected_a >= SAMPLE_FLOOR and projected_b >= SAMPLE_FLOOR
        if holds:
            viable.append((low, high, len(arm_a), len(arm_b)))
        print(
            f"{low:>6.2f} {high:>6.2f} | {len(arm_a):>6} {len(arm_b):>6} | "
            f"{projected_a:>7.1f} {projected_b:>7.1f} | {gap:>+10.1f} | "
            f"{'TUTAR' if holds else 'tutmaz'}"
        )

    print()
    for low, high in CANDIDATE_PAIRS:
        arm_a = [c for c in representatives if c.cheapness <= low]
        arm_b = [c for c in representatives if c.cheapness >= high]
        print(f"  A<={low:.2f} B>={high:.2f}")
        print(f"    A (ucuz)   n={len(arm_a):<5} {describe(arm_a)}")
        print(f"    B (pahali) n={len(arm_b):<5} {describe(arm_b)}")
    return viable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    parser.add_argument("--bars", default=str(DEFAULT_BARS))
    args = parser.parse_args()

    harvest, bars = pathlib.Path(args.harvest), pathlib.Path(args.bars)
    if not harvest.is_dir():
        print(f"hasat yok: {harvest}")
        return 2
    if not bars.exists():
        print(f"cubuk dosyasi yok: {bars}")
        return 2

    settings = load_settings()
    tickers = list(settings.universe.tickers)
    candidates = collect(harvest, load_closes(bars, tickers), tickers, settings)
    if not candidates:
        print("aday yok")
        return 1

    ratios = sorted(c.cheapness for c in candidates)
    quantiles = "  ".join(
        f"%{p}={ratios[int(p / 100 * (len(ratios) - 1))]:.2f}"
        for p in (5, 10, 25, 50, 75, 90, 95)
    )
    print(f"uygun kontrat gozlemi: {len(candidates)}")
    print(f"ucuzluk (IV / gerceklesen_vol20) ceyrekleri: {quantiles}\n")

    header = (
        f"{'A<=':>6} {'B>=':>6} | {'A kont':>7} {'B kont':>7} | {'esli c-s':>9} | "
        f"{'~kayit':>7} | taban {SAMPLE_FLOOR}"
    )
    print(header)
    print("-" * len(header))

    viable: list[tuple[float, float, int]] = []
    for low, high in CANDIDATE_PAIRS:
        arm_a = [c for c in candidates if c.cheapness <= low]
        arm_b = [c for c in candidates if c.cheapness >= high]
        paired = {(c.ticker, c.session) for c in arm_a} & {
            (c.ticker, c.session) for c in arm_b
        }
        projected = len(paired) * RISK_GATE_SURVIVAL
        holds = projected >= SAMPLE_FLOOR
        if holds:
            viable.append((low, high, len(paired)))
        print(
            f"{low:>6.2f} {high:>6.2f} | {len(arm_a):>7} {len(arm_b):>7} | "
            f"{len(paired):>9} | {projected:>7.1f} | {'TUTAR' if holds else 'tutmaz'}"
        )

    print()
    print("=== KOL BILESIMI (§7'nin zorunlu teshisi, dondurmadan ONCE) ===")
    print("Ucuz kol sistematik olarak daha GENIS makasa dusuyorsa, aile ucuzlugu")
    print("islem maliyetinden ayiramaz ve H06 gibi dondurulmamalidir.")
    print()
    for low, high in CANDIDATE_PAIRS:
        arm_a = [c for c in candidates if c.cheapness <= low]
        arm_b = [c for c in candidates if c.cheapness >= high]
        paired = {(c.ticker, c.session) for c in arm_a} & {
            (c.ticker, c.session) for c in arm_b
        }
        in_a = [c for c in arm_a if (c.ticker, c.session) in paired]
        in_b = [c for c in arm_b if (c.ticker, c.session) in paired]
        gap = (
            statistics.fmean(c.spread_pct for c in in_a)
            - statistics.fmean(c.spread_pct for c in in_b)
            if in_a and in_b
            else float("nan")
        )
        print(f"  A<={low:.2f} B>={high:.2f}")
        print(f"    A (ucuz)  n={len(in_a):<5} {describe(in_a)}")
        print(f"    B (pahali) n={len(in_b):<5} {describe(in_b)}")
        print(f"    makas farki (A - B): {gap:+.1f} puan")

    session_viable = sweep_session_level(candidates)

    print()
    if not viable and not session_viable:
        print("HICBIR YAPIDA HICBIR CIFT TABANI TUTMUYOR — aile dondurulmaz")
    elif not viable and session_viable:
        best = max(session_viable, key=lambda item: min(item[2], item[3]))
        print("ESLI yapi tabani tutmuyor; SEANS DUZEYINDE yapi tutuyor.")
        print(f"  en dengeli tutan cift: A<={best[0]:.2f} B>={best[1]:.2f} "
              f"(A {best[2]} c-s, B {best[3]} c-s)")
        print("  Bedeli: ayni-gun kontrolu kayboldu. Dondurulacaksa bu, secim kurali")
        print("  ve makas teshisiyle birlikte on-kayitta BEYAN edilir.")
    else:
        best = max(viable, key=lambda item: item[2])
        print(f"tabani tutan {len(viable)} cift; en cok esli seans-isim: "
              f"A<={best[0]:.2f} B>={best[1]:.2f} ({best[2]} c-s)")
        print("Esik secimi ancak yukaridaki makas farki kucukse yapilir; buyukse")
        print("aile dondurulmaz ve bu olcum INFEASIBLE_H10.md olarak kaydedilir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
