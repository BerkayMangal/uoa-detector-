"""H04's power analysis: can the two arms reach the sample floor at all?

    uv run python scripts/options_alpha_h04_power.py

This ran BEFORE `docs/options-alpha-v1/PREREG_H04.md` was frozen, and the table in
that document's section 4b is this script's output. A frozen document must not cite
numbers nobody can reproduce, which is why the script is committed rather than left
in a scratch directory.

**It computes counts only.** No P&L, no exit, no outcome is evaluated anywhere in
this file. That is the line that makes it legitimate: choosing band thresholds so
both arms can clear the floor is a power decision, and it is disclosed in section 4b;
choosing them because one split pays better would be forbidden, and nothing here can
see which split pays better.

What it measures. Among the contracts H01's rule would pick -- session volume at or
above the 90th percentile of the ticker's own trailing 20 sessions -- it computes the
underlying's move that day against its own trailing sigma:

    z = |close(D) / close(D-1) - 1| / sigma20,  sigma20 from the 20 returns BEFORE D

then reports how many anchors fall in each candidate band pair, and projects records
by the risk-gate survival rate H01 actually measured (202 of 879 structures).

The first draft of the pre-registration used z <= 0.5 and z >= 1.5. That left arm B
with roughly 15 projected records against a floor of 30, so the thresholds moved --
before freezing, and on these counts alone.

Reads the local harvest and a committed CSV. Spends no quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import statistics
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.selection import eligible_contracts, parse_chain_row
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings

DEFAULT_HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")
DEFAULT_BARS = pathlib.Path("data/study_f/bars.csv")

TRAILING_SESSIONS = 20
HIGH_PERCENTILE = 90.0

# Measured in H01's funnel: 202 of 879 built structures cleared the 100 USD risk
# gate. Used only to project records from anchors, never to judge anything.
RISK_GATE_SURVIVAL = 202 / 879
SAMPLE_FLOOR = 30
MIN_DEAD_BAND = 0.50

# The pairs the analysis considered. Kept in the file so the published table is the
# whole search, not a flattering row lifted out of a wider one.
CANDIDATE_PAIRS: tuple[tuple[float, float], ...] = (
    (0.45, 0.95),
    (0.50, 1.00),
    (0.50, 1.50),
    (0.40, 0.90),
    (0.55, 1.05),
    (0.60, 1.10),
    (0.50, 1.20),
    (0.45, 1.10),
)


def load_closes(path: pathlib.Path, tickers: list[str]) -> dict[str, dict[date, float]]:
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["ticker"] in tickers and row["close"]:
                closes[row["ticker"]][date.fromisoformat(row["day"])] = float(row["close"])
    return closes


def z_score(closes: dict[date, float], day: date) -> float | None:
    """The day's absolute return over the sigma of the 20 returns BEFORE it.

    Sigma never sees the move it is scaling; a sigma that included the day would
    shrink exactly when the move was large.
    """
    days = sorted(d for d in closes if d <= day)
    if len(days) < TRAILING_SESSIONS + 2 or days[-1] != day:
        return None
    series = [closes[d] for d in days[-(TRAILING_SESSIONS + 2) :]]
    returns = [series[i] / series[i - 1] - 1 for i in range(1, len(series))]
    sigma = statistics.stdev(returns[:-1])
    return None if sigma == 0 else abs(returns[-1]) / sigma


def collect_anchors(
    harvest: pathlib.Path,
    closes: dict[str, dict[date, float]],
    tickers: list[str],
    settings: OptionsAlphaSettings,
) -> list[dict[str, object]]:
    sessions = sorted(date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir())
    volumes: dict[tuple[str, date], list[int]] = {}
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
            volumes[(ticker, session)] = [q.volume for q in kept if q.volume is not None]

    anchors: list[dict[str, object]] = []
    # The tail is trimmed by the hold window the study needs after the entry session.
    for index in range(TRAILING_SESSIONS, len(sessions) - 6):
        flow_day = sessions[index]
        trailing = sessions[index - TRAILING_SESSIONS : index]
        for ticker in tickers:
            today = volumes.get((ticker, flow_day), [])
            if not today:
                continue
            sample = [v for day in trailing for v in volumes.get((ticker, day), [])]
            if not sample:
                continue
            best = max(today)
            percentile = 100.0 * sum(1 for v in sample if v <= best) / len(sample)
            if percentile < HIGH_PERCENTILE:
                continue
            z = z_score(closes[ticker], flow_day)
            if z is None:
                continue
            anchors.append(
                {"ticker": ticker, "session": flow_day.isoformat(), "z": round(z, 4)}
            )
    return anchors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    parser.add_argument("--bars", default=str(DEFAULT_BARS))
    parser.add_argument("--out", default=None, help="ankraj listesini JSON olarak yaz")
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
    closes = load_closes(bars, tickers)
    missing = [t for t in tickers if not closes.get(t)]
    if missing:
        print(f"cubuk verisi eksik olan isimler: {missing}")
        return 2

    anchors = collect_anchors(harvest, closes, tickers, settings)
    zs = sorted(float(a["z"]) for a in anchors)
    if not zs:
        print("aday yok")
        return 1

    print(f"aday seans-isim (>=%{HIGH_PERCENTILE:.0f} yuzdelik): {len(anchors)}")
    quantiles = "  ".join(
        f"%{p}={zs[int(p / 100 * (len(zs) - 1))]:.2f}" for p in (10, 25, 40, 50, 60, 75, 90)
    )
    print(f"z ceyrekleri: {quantiles}\n")

    header = (
        f"{'A esigi':>9} {'B esigi':>9} | {'A aday':>7} {'B aday':>7} | "
        f"{'~A kayit':>9} {'~B kayit':>9} | {'olu bant':>9} | taban {SAMPLE_FLOOR}"
    )
    print(header)
    print("-" * len(header))

    best: tuple[float, float] | None = None
    best_margin = 0.0
    for low, high in CANDIDATE_PAIRS:
        arm_a = sum(1 for z in zs if z <= low)
        arm_b = sum(1 for z in zs if z >= high)
        dead = len(zs) - arm_a - arm_b
        projected_a = arm_a * RISK_GATE_SURVIVAL
        projected_b = arm_b * RISK_GATE_SURVIVAL
        holds = projected_a >= SAMPLE_FLOOR and projected_b >= SAMPLE_FLOOR
        wide_enough = (high - low) >= MIN_DEAD_BAND
        margin = min(projected_a, projected_b)
        print(
            f"{low:>9.2f} {high:>9.2f} | {arm_a:>7} {arm_b:>7} | "
            f"{projected_a:>9.1f} {projected_b:>9.1f} | {dead:>9} | "
            f"{'TUTAR' if holds else 'tutmaz'}"
        )
        if holds and wide_enough and margin > best_margin:
            best, best_margin = (low, high), margin

    print()
    if best is None:
        print("HICBIR CIFT TABANI TUTMUYOR — degisecek olan tasarim, esik degil")
    else:
        print(
            f"secim kurali: olu bant >= {MIN_DEAD_BAND} sarti altinda zayif kolun "
            f"projeksiyonunu en buyuk yapan cift"
        )
        print(f"secilen: A z<={best[0]}  B z>={best[1]}  (zayif kol ~{best_margin:.1f} kayit)")

    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(anchors, indent=2) + "\n", encoding="utf-8")
        print(f"\nankrajlar -> {out} ({len(anchors)} satir)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
