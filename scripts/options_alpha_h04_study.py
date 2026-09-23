"""H04 — does flow LEAD the underlying, or merely follow it?

    uv run python scripts/options_alpha_h04_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_H04.md` (commit 17677a5,
before this file existed).

Unusual volume while the underlying has barely moved might precede the move. Unusual
volume after the underlying has already moved is probably a reaction to it. So the
candidate rule is H01's -- session volume at or above the 90th percentile of the
ticker's own trailing 20 sessions -- and the ARM is decided by a separate variable,
the underlying's own move that day measured against its own trailing sigma:

    z = |close(D) / close(D-1) - 1| / sigma20,  sigma20 from the 20 returns BEFORE D

  arm A (has not moved):  z <= 0.40
  arm B (already moved):  z >= 0.90
  the 0.40-0.90 band is deliberately unused, 0.50 sigma wide

Those thresholds are not the ones the first draft carried. A power analysis
(`scripts/options_alpha_h04_power.py`) showed the draft's 0.5 / 1.5 left arm B with
about 15 projected records against a floor of 30, so they moved BEFORE the freeze and
on counts alone -- disclosed in the pre-registration's section 4b.

One structural difference from H01 worth stating plainly: z is a property of the
(ticker, session), not of a contract, so a ticker-session lands in exactly ONE arm.
H01's ticker-sessions produced a candidate for each arm; here they cannot.

Information time is H01's rule for H01's reason: a session's volume is complete only
once that session closes, so entry is the D+1 CLOSE, and both the volume distribution
and sigma20 read strictly from sessions BEFORE D.

Reads the local harvest plus the committed data/study_f/bars.csv. Spends no quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.exits import (
    DailyObservation,
    ExitVariantId,
    evaluate_exit,
)
from uoa_detector.options_alpha.selection import (
    Funnel,
    build_candidate,
    eligible_contracts,
    parse_chain_row,
)
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings
from uoa_detector.options_alpha.structures import ContractQuote

DEFAULT_HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")
DEFAULT_BARS = pathlib.Path("data/study_f/bars.csv")

# Frozen in PREREG_H04 sections 4 and 4b.
TRAILING_SESSIONS = 20
HIGH_PERCENTILE = 90.0
ARM_A_MAX_Z = 0.40
ARM_B_MIN_Z = 0.90

# Frozen in sections 5 and 7. Identical to H01 so the two families compare.
HOLD_SESSIONS = 5
DTE_BUCKETS = ((14, 30), (31, 45), (46, 60))
DELTA_BUCKETS = ((0.25, 0.40), (0.40, 0.55), (0.55, 0.70))
SAMPLE_FLOOR = 30
BOOTSTRAP_SEED = 20260923
BOOTSTRAP_REPS = 10_000


def bucket_of(value: float, buckets: tuple[tuple[float, float], ...]) -> str | None:
    for low, high in buckets:
        if low <= value <= high:
            return f"{low}-{high}"
    return None


def percentile_of(value: float, sample: list[float]) -> float:
    if not sample:
        return 0.0
    return 100.0 * sum(1 for x in sample if x <= value) / len(sample)


def load_closes(path: pathlib.Path, tickers: list[str]) -> dict[str, dict[date, float]]:
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["ticker"] in tickers and row["close"]:
                closes[row["ticker"]][date.fromisoformat(row["day"])] = float(row["close"])
    return closes


def z_score(closes: dict[date, float], day: date) -> float | None:
    """Sigma never sees the move it scales; including the day would shrink it
    exactly when the move was large."""
    days = sorted(d for d in closes if d <= day)
    if len(days) < TRAILING_SESSIONS + 2 or days[-1] != day:
        return None
    series = [closes[d] for d in days[-(TRAILING_SESSIONS + 2) :]]
    returns = [series[i] / series[i - 1] - 1 for i in range(1, len(series))]
    sigma = statistics.stdev(returns[:-1])
    return None if sigma == 0 else abs(returns[-1]) / sigma


class ChainCache:
    """Full chains behind a rolling window, keyed by (ticker, session).

    Full chains are needed to pick a spread's short leg and to value the package on
    hold days; the walk moves forward in time, so a handful of recent sessions is
    enough and all 850 parsed at once would not fit comfortably.
    """

    def __init__(self, harvest: pathlib.Path, limit: int = 90) -> None:
        self._harvest = harvest
        self._limit = limit
        self._cache: dict[tuple[str, date], list[ContractQuote]] = {}

    def get(self, ticker: str, session: date) -> list[ContractQuote]:
        key = (ticker, session)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        path = self._harvest / session.isoformat() / f"{ticker}.json"
        quotes: list[ContractQuote] = []
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            quotes = [
                q
                for q in (parse_chain_row(row, session, ticker) for row in payload["rows"])
                if q is not None
            ]
        if len(self._cache) >= self._limit:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = quotes
        return quotes


@dataclass
class Record:
    ticker: str
    flow_session: date
    entry_session: date
    symbol: str
    arm: str
    z: float
    percentile: float
    volume: int
    dte_bucket: str
    delta_bucket: str
    structure: str
    entry_debit: str
    quantity: int
    exits: dict[str, Any]


def structure_value(quotes: list[ContractQuote], legs: tuple[Any, ...]) -> Decimal | None:
    """Closable package value: long leg at the bid, short leg at the ask."""
    by_symbol = {q.option_symbol: q for q in quotes}
    total = Decimal(0)
    for leg in legs:
        quote = by_symbol.get(leg.quote.option_symbol)
        if quote is None:
            return None
        price = quote.bid if leg.is_long else quote.ask
        if price is None or price <= 0:
            return None
        total += price if leg.is_long else -price
    return total.quantize(Decimal("0.01"))


def score(
    anchor_flow: ContractQuote,
    arm: str,
    z: float,
    pct: float,
    ticker: str,
    flow_day: date,
    entry_day: date,
    hold_days: list[date],
    cache: ChainCache,
    settings: OptionsAlphaSettings,
    funnel: Funnel,
) -> Record | None:
    chain_entry = cache.get(ticker, entry_day)
    anchor_entry = next(
        (q for q in chain_entry if q.option_symbol == anchor_flow.option_symbol), None
    )
    if anchor_entry is None or anchor_entry.delta is None:
        return None

    dte_bucket = bucket_of(float(anchor_entry.dte(entry_day)), DTE_BUCKETS)
    delta_bucket = bucket_of(abs(anchor_entry.delta), DELTA_BUCKETS)
    if dte_bucket is None or delta_bucket is None:
        return None

    entry_funnel = Funnel()
    candidate = build_candidate(anchor_entry, chain_entry, settings, entry_funnel)
    funnel.structures_built += entry_funnel.structures_built
    funnel.priced += entry_funnel.priced
    funnel.passed_cost_gate += entry_funnel.passed_cost_gate
    funnel.passed_risk_gate += entry_funnel.passed_risk_gate
    funnel.dropped.update(entry_funnel.dropped)
    if candidate is None:
        return None

    observations = []
    for day in hold_days:
        chain_day = cache.get(ticker, day)
        value = structure_value(chain_day, candidate.legs) if chain_day else None
        observations.append(
            DailyObservation(
                day=day,
                exit_value=value,
                dte=(candidate.legs[0].quote.expiry - day).days,
                is_complete=value is not None,
            )
        )

    # GROSS per-share ceiling, deliberately not price.max_profit_usd / multiplier:
    # that field already has commission taken out and evaluate_exit subtracts
    # commission_usd itself, so the net figure would charge the fees twice. Same
    # input H03 and H01 used, which is what makes the three families comparable.
    max_profit = None
    if len(candidate.legs) == 2:
        width = abs(candidate.legs[1].quote.strike - candidate.legs[0].quote.strike)
        max_profit = (width - candidate.price.entry_debit).quantize(Decimal("0.01"))

    exits: dict[str, Any] = {}
    for variant in ExitVariantId:
        outcome = evaluate_exit(
            variant=variant,
            entry_debit=candidate.price.entry_debit,
            observations=tuple(observations),
            settings=settings,
            structures=candidate.sizing.structures,
            max_profit_per_share=max_profit,
            commission_usd=candidate.price.commission_usd * Decimal(candidate.sizing.structures),
        )
        exits[variant.value] = {
            "reason": outcome.reason.value,
            "pnl_usd": None if outcome.pnl_usd is None else str(outcome.pnl_usd),
            "return_on_risk": outcome.return_on_risk,
            "held": outcome.held_trading_days,
            "unpriced_days": outcome.unpriced_days,
        }

    return Record(
        ticker=ticker,
        flow_session=flow_day,
        entry_session=entry_day,
        symbol=anchor_flow.option_symbol,
        arm=arm,
        z=round(z, 4),
        percentile=round(pct, 1),
        volume=anchor_flow.volume or 0,
        dte_bucket=dte_bucket,
        delta_bucket=delta_bucket,
        structure=candidate.kind.value,
        entry_debit=str(candidate.price.entry_debit),
        quantity=candidate.sizing.structures,
        exits=exits,
    )


def run(
    settings: OptionsAlphaSettings, harvest: pathlib.Path, bars: pathlib.Path
) -> dict[str, Any]:
    sessions = sorted(date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir())
    tickers = list(settings.universe.tickers)
    closes = load_closes(bars, tickers)
    cache = ChainCache(harvest)
    funnel = Funnel()

    # Pass one: eligible contracts only. H01's funnel showed roughly seventy per
    # file, small enough to hold for the whole harvest, which turns a 17,000-file
    # walk into 850.
    eligible_by: dict[tuple[str, date], list[ContractQuote]] = {}
    for session in sessions:
        for ticker in tickers:
            chain = cache.get(ticker, session)
            if not chain:
                continue
            kept, one = eligible_contracts(chain, session, settings)
            funnel.scanned += one.scanned
            funnel.eligible += one.eligible
            funnel.dropped.update(one.dropped)
            eligible_by[(ticker, session)] = kept

    records: list[Record] = []
    dead_band = 0
    no_sigma = 0

    for index in range(TRAILING_SESSIONS, len(sessions) - (HOLD_SESSIONS + 1)):
        flow_day = sessions[index]
        entry_day = sessions[index + 1]
        hold_days = sessions[index + 2 : index + 2 + HOLD_SESSIONS]
        trailing = sessions[index - TRAILING_SESSIONS : index]

        for ticker in tickers:
            today = eligible_by.get((ticker, flow_day), [])
            if not today:
                continue

            # Strictly BEFORE the flow session: D never enters its own comparison.
            sample = [
                float(q.volume)
                for day in trailing
                for q in eligible_by.get((ticker, day), [])
                if q.volume is not None
            ]
            if not sample:
                continue

            ranked = [
                (q, percentile_of(float(q.volume), sample))
                for q in today
                if q.volume is not None
            ]
            qualifying = [(q, pct) for q, pct in ranked if pct >= HIGH_PERCENTILE]
            if not qualifying:
                continue
            anchor, pct = max(qualifying, key=lambda pair: pair[0].volume or 0)

            z = z_score(closes.get(ticker, {}), flow_day)
            if z is None:
                no_sigma += 1
                continue

            # z belongs to the ticker-session, so this lands in exactly one arm.
            if z <= ARM_A_MAX_Z:
                arm = "A_not_moved"
            elif z >= ARM_B_MIN_Z:
                arm = "B_already_moved"
            else:
                dead_band += 1
                continue

            record = score(
                anchor, arm, z, pct, ticker, flow_day, entry_day, hold_days,
                cache, settings, funnel,
            )
            if record is not None:
                records.append(record)

    return summarise(records, settings, sessions, funnel, dead_band, no_sigma, bars)


def block_bootstrap(records: list[Record], variant: str) -> dict[str, Any]:
    """Session-block resampling of the arm difference, as section 7 requires.

    The unit is the SESSION, not the card: one market move drives every card entered
    that day, so treating same-session cards as independent draws would shrink the
    interval to something the data does not support.
    """
    by_session: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for record in records:
        raw = record.exits[variant]["pnl_usd"]
        if raw is not None:
            by_session[record.entry_session].append((record.arm, float(raw)))
    blocks = sorted(by_session)
    if not blocks:
        return {"blocks": 0, "excludes_zero": False, "note": "blok yok"}

    def difference(chosen: list[list[tuple[str, float]]]) -> float | None:
        a = [v for block in chosen for arm, v in block if arm == "A_not_moved"]
        b = [v for block in chosen for arm, v in block if arm == "B_already_moved"]
        if not a or not b:
            return None
        return statistics.fmean(a) - statistics.fmean(b)

    observed = difference([by_session[s] for s in blocks])
    rng = random.Random(BOOTSTRAP_SEED)
    draws = [
        d
        for _ in range(BOOTSTRAP_REPS)
        if (d := difference([by_session[rng.choice(blocks)] for _ in blocks])) is not None
    ]
    draws.sort()
    low, high = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))]
    return {
        "blocks": len(blocks),
        "reps": len(draws),
        "seed": BOOTSTRAP_SEED,
        "observed_difference_usd": None if observed is None else round(observed, 2),
        "ci95": [round(low, 2), round(high, 2)],
        "share_at_or_below_zero": round(sum(1 for d in draws if d <= 0) / len(draws), 3),
        "excludes_zero": low > 0 or high < 0,
    }


def summarise(
    records: list[Record],
    settings: OptionsAlphaSettings,
    sessions: list[date],
    funnel: Funnel,
    dead_band: int,
    no_sigma: int,
    bars: pathlib.Path,
) -> dict[str, Any]:
    primary = ExitVariantId.TIME_ONLY.value

    def pnls(rows: list[Record], variant: str) -> list[float]:
        return [
            float(r.exits[variant]["pnl_usd"])
            for r in rows
            if r.exits[variant]["pnl_usd"] is not None
        ]

    by_arm: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        by_arm[record.arm].append(record)

    arms: dict[str, Any] = {}
    for arm, rows in by_arm.items():
        values = pnls(rows, primary)
        zs = [r.z for r in rows]
        arms[arm] = {
            "n": len(rows),
            "n_with_pnl": len(values),
            "mean_pnl_usd": round(statistics.fmean(values), 2) if values else None,
            "median_pnl_usd": round(statistics.median(values), 2) if values else None,
            "total_pnl_usd": round(sum(values), 2) if values else None,
            "win_rate": round(sum(1 for v in values if v > 0) / len(values), 3) if values else None,
            "mean_z": round(statistics.fmean(zs), 3) if zs else None,
        }

    grouped: dict[tuple[str, str], dict[str, list[Record]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in records:
        grouped[(record.dte_bucket, record.delta_bucket)][record.arm].append(record)

    buckets: dict[str, Any] = {}
    dropped_buckets: list[str] = []
    for (dte, delta), here in sorted(grouped.items()):
        key = f"dte {dte} / delta {delta}"
        if len(here) < 2:
            dropped_buckets.append(f"{key} (yalniz {next(iter(here))})")
            continue
        buckets[key] = {
            arm: {
                "n": len(rows),
                "mean_pnl_usd": (
                    round(statistics.fmean(pnls(rows, primary)), 2)
                    if pnls(rows, primary)
                    else None
                ),
            }
            for arm, rows in here.items()
        }

    arm_a = arms.get("A_not_moved", {})
    arm_b = arms.get("B_already_moved", {})
    boot = block_bootstrap(records, primary)
    floor_met = (arm_a.get("n_with_pnl") or 0) >= SAMPLE_FLOOR and (
        arm_b.get("n_with_pnl") or 0
    ) >= SAMPLE_FLOOR
    mean_a, mean_b = arm_a.get("mean_pnl_usd"), arm_b.get("mean_pnl_usd")

    # Section 7's ladder, all THREE clauses. H01's runner checked one, invented a
    # label absent from its own ladder and never computed the bootstrap its protocol
    # demanded; that defect is why this is written out clause by clause.
    failed: list[str] = []
    if not (mean_a is not None and mean_b is not None and mean_a > mean_b):
        failed.append("A kolu B kolunu gecmiyor")
    if not (mean_a is not None and mean_a > 0):
        failed.append(f"A kolu maliyet sonrasi pozitif degil ({mean_a} $)")
    if not boot["excludes_zero"]:
        failed.append("blok bootstrap araligi sifiri iceriyor")

    if not floor_met or mean_a is None or mean_b is None:
        verdict = "INSUFFICIENT_DATA"
        failed = [f"her iki kolda {SAMPLE_FLOOR} tamamlanmis yapi yok"]
    elif not failed:
        verdict = "EXPLORATORY_PASS"
    else:
        verdict = "REJECTED"

    return {
        "hypothesis": "H04",
        "protocol": ["docs/options-alpha-v1/PREREG_H04.md"],
        "protocol_frozen_commit": "17677a5",
        "profile_sha256": settings.profile_sha256,
        "quality_tier": settings.quality.tier,
        "underlying_bars": str(bars),
        "sessions_available": len(sessions),
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else [],
        "arm_definition": {
            "A_not_moved": f"z <= {ARM_A_MAX_Z}",
            "B_already_moved": f"z >= {ARM_B_MIN_Z}",
            "unused_band": f"{ARM_A_MAX_Z}-{ARM_B_MIN_Z}",
            "z": "|close(D)/close(D-1)-1| / stdev of the 20 returns strictly before D",
        },
        "candidate_rule": f"volume percentile >= {HIGH_PERCENTILE} of the ticker's own "
        f"trailing {TRAILING_SESSIONS} sessions, highest-volume qualifying contract",
        "primary_metric": f"net option P&L per structure, exit {primary}, "
        f"{settings.exit.primary_hold_trading_days} trading days",
        "dead_band_dropped": dead_band,
        "no_sigma_dropped": no_sigma,
        "funnel": funnel.as_dict(),
        "arms": arms,
        "buckets": buckets,
        "dropped_buckets": dropped_buckets,
        "secondary_variants": {
            variant.value: {
                arm: (
                    round(statistics.fmean(pnls(rows, variant.value)), 2)
                    if pnls(rows, variant.value)
                    else None
                )
                for arm, rows in by_arm.items()
            }
            for variant in ExitVariantId
            if variant.value != primary
        },
        "sample_floor_met": floor_met,
        "block_bootstrap": boot,
        "verdict": verdict,
        "verdict_failed_clauses": failed,
        "multiple_testing": (
            "options_alpha_v1 kapsaminda ucuncu aile; toplam ilan edilmis deneme 9 "
            "(H03 3 + H01 3 + H04 3). Tek bir ailenin esigi gecmesi dokuz denemelik "
            "bir aramada tek basina kanit degildir."
        ),
        "records": [
            {
                "ticker": r.ticker,
                "flow_session": r.flow_session.isoformat(),
                "entry_session": r.entry_session.isoformat(),
                "symbol": r.symbol,
                "arm": r.arm,
                "z": r.z,
                "percentile": r.percentile,
                "volume": r.volume,
                "dte_bucket": r.dte_bucket,
                "delta_bucket": r.delta_bucket,
                "structure": r.structure,
                "entry_debit": r.entry_debit,
                "quantity": r.quantity,
                "exits": r.exits,
            }
            for r in records
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/options-alpha-v1/h04_result.json")
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
    result = run(settings, harvest, bars)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"seans {result['sessions_available']}  pencere {result['window']}")
    print(f"olu bantta dusen: {result['dead_band_dropped']}  sigma yok: {result['no_sigma_dropped']}")
    print(f"\nhuni: {result['funnel']}")
    print("\n--- KOLLAR (birincil: time_only) ---")
    for arm, stats in result["arms"].items():
        print(
            f"  {arm:16s} n={stats['n']:<4} ortalama {stats['mean_pnl_usd']} $  "
            f"medyan {stats['median_pnl_usd']} $  kazanma {stats['win_rate']}  "
            f"ort z {stats['mean_z']}"
        )
    print("\n--- KOVALAR ---")
    for key, here in result["buckets"].items():
        parts = " | ".join(f"{a}: n={v['n']} ort {v['mean_pnl_usd']}" for a, v in here.items())
        print(f"  {key}  {parts}")
    for dropped in result["dropped_buckets"]:
        print(f"  DUSTU {dropped}")
    print(f"\nikincil: {result['secondary_variants']}")
    boot = result["block_bootstrap"]
    print(
        f"\nseans-blok bootstrap: {boot['blocks']} blok, gozlenen "
        f"{boot.get('observed_difference_usd')} $, %95 {boot.get('ci95')}, "
        f"<=0 orani {boot.get('share_at_or_below_zero')}, "
        f"sifiri disliyor: {boot['excludes_zero']}"
    )
    print(f"ornek tabani (her iki kolda >={SAMPLE_FLOOR}): {result['sample_floor_met']}")
    for clause in result["verdict_failed_clauses"]:
        print(f"  DUSEN SART: {clause}")
    print(f"HUKUM: {result['verdict']}")
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
