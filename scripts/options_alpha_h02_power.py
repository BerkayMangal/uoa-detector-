"""H02's power analysis: is repeating unusual volume testable, and is it just liquidity?

    uv run python scripts/options_alpha_h02_power.py

Runs BEFORE `docs/options-alpha-v1/PREREG_H02.md` is frozen. Four families have been
shaped by this step already, and this script carries all four lessons:

  * H04 -- the draft's bands left one arm at ~15 projected records, so bands are chosen
    on counts and the choice is disclosed.
  * H06 -- the measure could not be separated from the calendar, so the family was
    refused a freeze. Here the analogous risk is LIQUIDITY, and the per-arm ticker
    distribution is printed for exactly that reason.
  * H10 -- the arm variable was assumed contract-level and turned out session-level.
    So both structures are measured here rather than assumed.
  * H10's RUN -- the projection multiplied arm-assigned sessions by the risk-gate rate
    alone and ignored the attrition between anchor and record. End to end that was 13%,
    not 23%, and an arm missed the floor by two. **This script checks the entry-day
    buckets for real** instead of assuming they pass.

**Counts only.** No P&L, no exit, no outcome is evaluated anywhere in this file.

Unusualness is H01's measure, unchanged: a contract's session volume as a percentile of
the distribution of eligible-contract volumes for that ticker over the previous 20
sessions. The ARM is the contract's own history:

    repetition(contract, D) = sessions in D-1..D-k where it was also >= 90th percentile
    arm A: repetition >= A_MIN     arm B: repetition == 0

Reads the local harvest only. Spends no quota.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from typing import NamedTuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.selection import eligible_contracts, parse_chain_row
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings
from uoa_detector.options_alpha.structures import ContractQuote

DEFAULT_HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")

TRAILING_SESSIONS = 20
HIGH_PERCENTILE = 90.0
RISK_GATE_SURVIVAL = 202 / 879  # measured in H01's funnel
SAMPLE_FLOOR = 30

# The engine's frozen entry gates, checked here rather than assumed. This is the step
# H10's power analysis skipped.
MIN_DTE, MAX_DTE = 14, 60
MIN_ABS_DELTA, MAX_ABS_DELTA = 0.25, 0.70

LOOKBACKS = (3, 5, 10)
A_MINIMUMS = (1, 2, 3)


class Anchor(NamedTuple):
    ticker: str
    session: date
    symbol: str
    arm: str
    repetition: int
    volume: int
    survives_entry: bool


def percentile_of(value: float, sample: list[float]) -> float:
    if not sample:
        return 0.0
    return 100.0 * sum(1 for x in sample if x <= value) / len(sample)


class Harvest:
    """Eligible contracts per (ticker, session), plus full chains on demand."""

    def __init__(self, harvest: pathlib.Path, settings: OptionsAlphaSettings) -> None:
        self._harvest = harvest
        self._settings = settings
        self.sessions = sorted(
            date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir()
        )
        # symbol -> volume, for eligible contracts only (~70 per file).
        self.eligible: dict[tuple[str, date], dict[str, int]] = {}
        # (ticker, entry day) -> symbol -> (delta, dte), built lazily for the entry check.
        self._entry: dict[tuple[str, date], dict[str, tuple[float | None, int]]] = {}

    def load_eligible(self, tickers: list[str]) -> None:
        for session in self.sessions:
            for ticker in tickers:
                chain = self._chain(ticker, session)
                if not chain:
                    continue
                kept, _ = eligible_contracts(chain, session, self._settings)
                self.eligible[(ticker, session)] = {
                    q.option_symbol: q.volume for q in kept if q.volume is not None
                }

    def _chain(self, ticker: str, session: date) -> list[ContractQuote]:
        path = self._harvest / session.isoformat() / f"{ticker}.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [
            q
            for q in (parse_chain_row(r, session, ticker) for r in payload["rows"])
            if q is not None
        ]

    def survives_entry(self, ticker: str, entry_day: date, symbol: str) -> bool:
        """Would this anchor still produce a record at the D+1 close?

        The anchor has to be listed, carry a delta, and sit inside BOTH the DTE and
        the |delta| bucket. H10 assumed this and lost half its projected sample.

        Cached per (ticker, entry day): nine (k, A_MIN) combinations ask about the same
        entry days thousands of times over, and re-parsing a 650 KB chain for one symbol
        each time would make this script slower than the study it is sizing.
        """
        key = (ticker, entry_day)
        table = self._entry.get(key)
        if table is None:
            table = {
                q.option_symbol: (q.delta, q.dte(entry_day))
                for q in self._chain(ticker, entry_day)
            }
            self._entry[key] = table
        found = table.get(symbol)
        if found is None:
            return False
        delta, dte = found
        if delta is None:
            return False
        return MIN_DTE <= dte <= MAX_DTE and MIN_ABS_DELTA <= abs(delta) <= MAX_ABS_DELTA


def high_sets(harvest: Harvest, tickers: list[str]) -> dict[tuple[str, date], set[str]]:
    """Symbols at or above the 90th percentile of the ticker's own trailing window."""
    out: dict[tuple[str, date], set[str]] = {}
    for index in range(TRAILING_SESSIONS, len(harvest.sessions)):
        day = harvest.sessions[index]
        trailing = harvest.sessions[index - TRAILING_SESSIONS : index]
        for ticker in tickers:
            today = harvest.eligible.get((ticker, day), {})
            if not today:
                continue
            sample = [
                float(v)
                for past in trailing
                for v in harvest.eligible.get((ticker, past), {}).values()
            ]
            if not sample:
                continue
            out[(ticker, day)] = {
                symbol
                for symbol, volume in today.items()
                if percentile_of(float(volume), sample) >= HIGH_PERCENTILE
            }
    return out


def collect(
    harvest: Harvest,
    highs: dict[tuple[str, date], set[str]],
    tickers: list[str],
    lookback: int,
    a_min: int,
) -> list[Anchor]:
    anchors: list[Anchor] = []
    sessions = harvest.sessions
    first = TRAILING_SESSIONS + lookback
    for index in range(first, len(sessions) - 6):
        flow_day, entry_day = sessions[index], sessions[index + 1]
        past_days = sessions[index - lookback : index]
        for ticker in tickers:
            today_high = highs.get((ticker, flow_day), set())
            if not today_high:
                continue
            volumes = harvest.eligible.get((ticker, flow_day), {})
            per_arm: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
            for symbol in today_high:
                repetition = sum(
                    1 for past in past_days if symbol in highs.get((ticker, past), set())
                )
                if repetition >= a_min:
                    arm = "A_repeat"
                elif repetition == 0:
                    arm = "B_one_off"
                else:
                    continue  # dead band
                per_arm[arm].append((symbol, volumes.get(symbol, 0), repetition))
            for arm, rows in per_arm.items():
                symbol, volume, repetition = max(rows, key=lambda r: r[1])
                anchors.append(
                    Anchor(
                        ticker=ticker,
                        session=flow_day,
                        symbol=symbol,
                        arm=arm,
                        repetition=repetition,
                        volume=volume,
                        survives_entry=harvest.survives_entry(ticker, entry_day, symbol),
                    )
                )
    return anchors


def report(anchors: list[Anchor], lookback: int, a_min: int) -> None:
    arm_a = [a for a in anchors if a.arm == "A_repeat"]
    arm_b = [a for a in anchors if a.arm == "B_one_off"]
    sessions_a = {(a.ticker, a.session) for a in arm_a}
    sessions_b = {(a.ticker, a.session) for a in arm_b}
    paired = sessions_a & sessions_b

    # Bucket attrition measured, not assumed -- the H10 lesson.
    survive_a = [a for a in arm_a if a.survives_entry]
    survive_b = [a for a in arm_b if a.survives_entry]
    paired_survivors = {(a.ticker, a.session) for a in survive_a} & {
        (a.ticker, a.session) for a in survive_b
    }

    projected_paired = len(paired_survivors) * RISK_GATE_SURVIVAL
    projected_a = len(survive_a) * RISK_GATE_SURVIVAL
    projected_b = len(survive_b) * RISK_GATE_SURVIVAL
    paired_holds = projected_paired >= SAMPLE_FLOOR
    unpaired_holds = projected_a >= SAMPLE_FLOOR and projected_b >= SAMPLE_FLOOR
    verdict = (
        "ESLI TUTAR" if paired_holds
        else ("yalniz ESLEMESIZ tutar" if unpaired_holds else "hicbiri tutmaz")
    )

    print(
        f"k={lookback:<3} A>={a_min} | A {len(arm_a):>5} B {len(arm_b):>5} | "
        f"kova sonrasi A {len(survive_a):>5} B {len(survive_b):>5} | "
        f"esli {len(paired):>4}->{len(paired_survivors):<4} | "
        f"~esli {projected_paired:>6.1f} | ~A {projected_a:>6.1f} ~B {projected_b:>6.1f} | "
        f"{verdict}"
    )

    if survive_a and survive_b:
        top_a = Counter(a.ticker for a in survive_a).most_common(3)
        top_b = Counter(a.ticker for a in survive_b).most_common(3)
        share_a = top_a[0][1] / len(survive_a)
        print(
            f"        isim dagilimi  A: {top_a}  (en buyuk pay %{share_a * 100:.0f})"
            f"  B: {top_b}"
        )
        if share_a >= 0.5:
            print("        UYARI: A kolu tek isme yigiliyor — likidite secimi "
                  "ayristirilamaz, H06 gibi dondurulmamali")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    args = parser.parse_args()

    root = pathlib.Path(args.harvest)
    if not root.is_dir():
        print(f"hasat yok: {root}")
        return 2

    settings = load_settings()
    tickers = list(settings.universe.tickers)
    harvest = Harvest(root, settings)
    harvest.load_eligible(tickers)
    highs = high_sets(harvest, tickers)

    counts = [len(v) for v in highs.values()]
    print(f"seans-isim (>=%{HIGH_PERCENTILE:.0f} kumesi olan): {len(highs)}")
    print(f"kume buyuklugu: ortalama {statistics.fmean(counts):.1f}  medyan "
          f"{statistics.median(counts):.0f}\n")
    print("Kova elemesi VARSAYILMIYOR, girişte fiilen kontrol ediliyor (H10 dersi).")
    print(f"Taban {SAMPLE_FLOOR}, risk kapisi orani %{RISK_GATE_SURVIVAL * 100:.0f}.\n")

    for lookback in LOOKBACKS:
        for a_min in A_MINIMUMS:
            report(collect(harvest, highs, tickers, lookback, a_min), lookback, a_min)
        print()

    print("Yigilma uyarisi yoksa ve bir yapi tabani tutuyorsa esik secimi yapilir;")
    print("aksi halde aile dondurulmaz ve olcum INFEASIBLE_H02.md olarak kaydedilir.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
