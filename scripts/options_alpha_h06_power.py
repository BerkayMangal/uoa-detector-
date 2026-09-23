"""H06's power analysis: can the concentration arms reach the sample floor?

    uv run python scripts/options_alpha_h06_power.py

Runs BEFORE `docs/options-alpha-v1/PREREG_H06.md` is frozen, for the reason H04
made concrete: that family's draft bands would have left one arm at roughly fifteen
records against a floor of thirty, and the study would have closed as
INSUFFICIENT_DATA without ever testing its hypothesis. Thresholds get chosen here,
on counts, and the choice is disclosed in the pre-registration.

**It computes counts only.** No P&L, no exit, no outcome is evaluated anywhere in
this file. That is what separates a power decision from a result-fitted one.

What it measures. For each (ticker, session D), eligible contracts are grouped by
expiry and each expiry's share of that session's eligible volume is compared with
its own trailing baseline:

    share(E, D)    = E's eligible volume / all eligible volume that session
    baseline(E, D) = mean share(E, .) over the 20 sessions BEFORE D
    rise(E, D)     = share(E, D) - baseline(E, D)

Only expiries listed on EVERY one of the 20 trailing sessions qualify. The first
version counted an absent expiry as share 0, on the reasoning that "absent" really is
"no share" -- and the counts refuted it: the median rise came out at +0.061,
systematically positive, which a genuine concentration measure cannot be. A newly
listed expiry always shows a large rise against a baseline of zeros, so the measure
was reading listing age and the natural drift of share as an expiry approaches, not
interest moving toward it. That is the confound this filter removes, found before the
protocol was frozen rather than after.

It also prints the calendar diagnostic the pre-registration demands: how much of the
concentrating arm is simply the monthly expiry, which would mean the study measured
the calendar rather than information.

Reads the local harvest only. Spends no quota.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import date
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.selection import eligible_contracts, parse_chain_row
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings

DEFAULT_HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")

TRAILING_SESSIONS = 20
RISK_GATE_SURVIVAL = 202 / 879  # measured in H01's funnel
SAMPLE_FLOOR = 30

# Draft bands; the whole point of this script is to find out whether they hold.
CANDIDATE_PAIRS: tuple[tuple[float, float], ...] = (
    (0.05, 0.00),
    (0.04, 0.00),
    (0.03, 0.00),
    (0.03, -0.01),
    (0.02, 0.00),
    (0.02, -0.01),
    (0.06, -0.01),
    (0.08, -0.02),
)


class Anchor(NamedTuple):
    ticker: str
    session: date
    expiry: date
    rise: float
    volume: int
    is_monthly: bool


def is_third_friday(day: date) -> bool:
    """Monthly expiry. The calendar lifts concentration on its own, so the report
    has to say how much of an arm is just this."""
    return day.weekday() == 4 and 15 <= day.day <= 21


def session_shares(
    harvest: pathlib.Path, tickers: list[str], settings: OptionsAlphaSettings
) -> tuple[
    list[date],
    dict[tuple[str, date], dict[date, float]],
    dict[tuple[str, date], dict[date, int]],
]:
    """Per (ticker, session): each expiry's share of eligible volume, and its best
    contract volume."""
    sessions = sorted(date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir())
    shares: dict[tuple[str, date], dict[date, float]] = {}
    best: dict[tuple[str, date], dict[date, int]] = {}

    for session in sessions:
        for ticker in tickers:
            path = harvest / session.isoformat() / f"{ticker}.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            quotes = [
                q
                for q in (parse_chain_row(r, session, ticker) for r in payload["rows"])
                if q is not None
            ]
            kept, _ = eligible_contracts(quotes, session, settings)
            by_expiry_volume: dict[date, int] = defaultdict(int)
            by_expiry_best: dict[date, int] = defaultdict(int)
            for quote in kept:
                if quote.volume is None:
                    continue
                by_expiry_volume[quote.expiry] += quote.volume
                by_expiry_best[quote.expiry] = max(by_expiry_best[quote.expiry], quote.volume)
            total = sum(by_expiry_volume.values())
            if total <= 0:
                continue
            shares[(ticker, session)] = {
                expiry: volume / total for expiry, volume in by_expiry_volume.items()
            }
            best[(ticker, session)] = dict(by_expiry_best)
    return sessions, shares, best


def collect(
    sessions: list[date],
    shares: dict[tuple[str, date], dict[date, float]],
    best: dict[tuple[str, date], dict[date, int]],
    tickers: list[str],
) -> list[Anchor]:
    anchors: list[Anchor] = []
    for index in range(TRAILING_SESSIONS, len(sessions) - 6):
        flow_day = sessions[index]
        trailing = sessions[index - TRAILING_SESSIONS : index]
        for ticker in tickers:
            today = shares.get((ticker, flow_day))
            if not today:
                continue
            for expiry, share in today.items():
                # Only expiries LISTED on every trailing session qualify.
                #
                # The first version of this counted an absent expiry as share 0,
                # and the counts exposed it: the median rise came out at +0.061,
                # systematically positive, which a genuine concentration measure
                # cannot be. A newly listed expiry always shows a large "rise"
                # against a baseline of zeros, so the measure was reading listing
                # age and the drift of share as an expiry approaches, not interest
                # moving toward it.
                past = [p for p in (shares.get((ticker, day), {}) for day in trailing) if p]
                if len(past) < TRAILING_SESSIONS or any(expiry not in p for p in past):
                    continue
                history = [p[expiry] for p in past]
                rise = share - statistics.fmean(history)
                volume = best.get((ticker, flow_day), {}).get(expiry, 0)
                anchors.append(
                    Anchor(ticker, flow_day, expiry, rise, volume, is_third_friday(expiry))
                )
    return anchors


def sweep(anchors: list[Anchor], label: str) -> None:
    """Counts per band pair. A ticker-session counts only when BOTH arms appear in
    it, which is what makes the comparison same-name and same-day."""
    if not anchors:
        print(f"\n=== {label} === gozlem yok")
        return
    rises = sorted(a.rise for a in anchors)
    quantiles = "  ".join(
        f"%{p}={rises[int(p / 100 * (len(rises) - 1))]:+.3f}" for p in (10, 25, 50, 75, 90)
    )
    print(f"\n=== {label} === gozlem {len(anchors)} | artis ceyrekleri: {quantiles}")
    # Two comparison structures are reported side by side, because the choice between
    # them is a design decision and it should be made on visible counts rather than
    # discovered later.
    #
    #   PAIRED   a ticker-session counts only when BOTH arms appear in it, so every
    #            comparison is same-name and same-day. Strongest control.
    #   UNPAIRED each arm counts its own ticker-sessions independently. Loses the
    #            same-day control and lets market-day effects in.
    #
    # The same-day control was an addition of mine, not something the hypothesis
    # requires, so dropping it is a legitimate design choice -- but it is a real
    # weakening and is named as one.
    header = (
        f"{'A esigi':>9} {'B esigi':>9} | {'A vade':>7} {'B vade':>7} | "
        f"{'esli':>5} {'~esli kyt':>9} | {'A c-s':>6} {'B c-s':>6} | "
        f"{'~A kyt':>7} {'~B kyt':>7} | {'A aylik':>8} | taban {SAMPLE_FLOOR}"
    )
    print(header)
    print("-" * len(header))
    for low, high in CANDIDATE_PAIRS:
        arm_a = [a for a in anchors if a.rise >= low]
        arm_b = [a for a in anchors if a.rise <= high]
        sessions_a = {(a.ticker, a.session) for a in arm_a}
        sessions_b = {(a.ticker, a.session) for a in arm_b}
        paired = sessions_a & sessions_b
        # One anchor per (ticker, session, arm): the highest-volume expiry in that arm.
        paired_projected = len(paired) * RISK_GATE_SURVIVAL
        unpaired_a = len(sessions_a) * RISK_GATE_SURVIVAL
        unpaired_b = len(sessions_b) * RISK_GATE_SURVIVAL
        monthly_share = (
            sum(1 for a in arm_a if a.is_monthly) / len(arm_a) if arm_a else 0.0
        )
        paired_holds = paired_projected >= SAMPLE_FLOOR
        unpaired_holds = unpaired_a >= SAMPLE_FLOOR and unpaired_b >= SAMPLE_FLOOR
        verdict = (
            "ESLI TUTAR" if paired_holds
            else ("yalniz ESLEMESIZ tutar" if unpaired_holds else "hicbiri tutmaz")
        )
        print(
            f"{low:>+9.2f} {high:>+9.2f} | {len(arm_a):>7} {len(arm_b):>7} | "
            f"{len(paired):>5} {paired_projected:>9.1f} | "
            f"{len(sessions_a):>6} {len(sessions_b):>6} | "
            f"{unpaired_a:>7.1f} {unpaired_b:>7.1f} | "
            f"{monthly_share:>7.0%} | {verdict}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    parser.add_argument(
        "--out", default=None, help="ankrajlari JSON yaz; sonraki analizler 850 dosyayi yurumez"
    )
    args = parser.parse_args()

    harvest = pathlib.Path(args.harvest)
    if not harvest.is_dir():
        print(f"hasat yok: {harvest}")
        return 2

    settings = load_settings()
    tickers = list(settings.universe.tickers)
    sessions, shares, best = session_shares(harvest, tickers, settings)
    anchors = collect(sessions, shares, best, tickers)
    if not anchors:
        print("aday yok")
        return 1

    rises = sorted(a.rise for a in anchors)
    print(f"vade-seans-isim gozlemi: {len(anchors)}")
    quantiles = "  ".join(
        f"%{p}={rises[int(p / 100 * (len(rises) - 1))]:+.3f}"
        for p in (10, 25, 50, 75, 90)
    )
    print(f"artis ceyrekleri: {quantiles}\n")

    sweep(anchors, "TUM VADELER")
    sweep([a for a in anchors if not a.is_monthly], "AYLIK VADE HARIC")

    print()
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                [
                    {
                        "ticker": a.ticker,
                        "session": a.session.isoformat(),
                        "expiry": a.expiry.isoformat(),
                        "rise": round(a.rise, 5),
                        "volume": a.volume,
                        "is_monthly": a.is_monthly,
                    }
                    for a in anchors
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"ankrajlar -> {out} ({len(anchors)} satir)")

    print("A aylik: A kolundaki vadelerin yuzde kaci ucuncu cuma (aylik vade).")
    print("Yuksekse olculen sey bilgi degil TAKVIM olabilir. Ikinci tablo aylik")
    print("vadeleri tamamen disliyor: takvim etkisini tasarimdan atmanin maliyeti")
    print("orada gorulur ve taban orada da tutmali.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
