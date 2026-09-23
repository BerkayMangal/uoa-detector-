"""H01 — is volume that is unusual FOR ITS OWN TICKER worth anything?

    uv run python scripts/options_alpha_h01_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_H01.md`.

Absolute premium measures a ticker's scale, not an event: five thousand lots is
ordinary in SPY and extraordinary in a small name. So a contract's session volume
is ranked against the distribution of eligible-contract volumes for THAT ticker
over the previous 20 sessions. The high arm sits at or above the 90th percentile,
the control between the 40th and 60th, and the 60-90 band is deliberately unused so
the arms cannot blur into each other.

The design exists in this shape because of how H03 failed. There, candidates were
selected on volume above open interest while the arms split on the open-interest
increase -- two faces of one variable -- and the control arm collapsed to seven.
Here the eligibility gates decide what is tradeable and a separate variable, the
volume percentile, decides the arm.

Information time. A session's volume is only complete once that session closes, so
acting on volume(D) at D's close would be look-ahead. Entry is the D+1 CLOSE, and
the trailing distribution is built strictly from sessions BEFORE D -- D never enters
its own comparison.

Two passes, for a reason. Loading twenty sessions of chains per ticker-session would
be seventeen thousand file reads; instead pass one walks the harvest once and keeps
only the eligible contracts (about seventy per file), and pass two loads full chains
on demand behind a small rolling cache, because the short leg of a spread is chosen
from every listed strike rather than from the eligible subset.

Reads only the local harvest; spends no quota.
"""

from __future__ import annotations

import argparse
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

# Frozen in the pre-registration, section 4.
TRAILING_SESSIONS = 20
HIGH_PERCENTILE = 90.0
CONTROL_LOW, CONTROL_HIGH = 40.0, 60.0

# Frozen in section 5 and 6.
HOLD_SESSIONS = 5
DTE_BUCKETS = ((14, 30), (31, 45), (46, 60))
DELTA_BUCKETS = ((0.25, 0.40), (0.40, 0.55), (0.55, 0.70))


def bucket_of(value: float, buckets: tuple[tuple[float, float], ...]) -> str | None:
    for low, high in buckets:
        if low <= value <= high:
            return f"{low}-{high}"
    return None


def percentile_of(value: float, sample: list[float]) -> float:
    """Share of the sample at or below ``value``, as a percentage."""
    if not sample:
        return 0.0
    return 100.0 * sum(1 for x in sample if x <= value) / len(sample)


class ChainCache:
    """Full chains behind a small rolling window, keyed by (ticker, session).

    Full chains are needed only to pick a spread's short leg and to value the
    package on hold days, and the walk moves forward in time, so a handful of
    recent sessions is enough. Keeping all 850 parsed would not fit comfortably.
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
    # that field already has the commission taken out, and evaluate_exit subtracts
    # commission_usd itself, so the net figure would charge the fees twice. Same
    # input H03 used, which is what makes the two families' numbers comparable.
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
        percentile=round(pct, 1),
        volume=anchor_flow.volume or 0,
        dte_bucket=dte_bucket,
        delta_bucket=delta_bucket,
        structure=candidate.kind.value,
        entry_debit=str(candidate.price.entry_debit),
        quantity=candidate.sizing.structures,
        exits=exits,
    )


def run(settings: OptionsAlphaSettings, harvest: pathlib.Path) -> dict[str, Any]:
    sessions = sorted(date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir())
    tickers = list(settings.universe.tickers)
    cache = ChainCache(harvest)
    funnel = Funnel()

    # Pass one: eligible contracts only, which the funnel showed to be about
    # seventy per file -- small enough to hold for the whole harvest.
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
    dropped_one_armed = 0

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

            high: list[tuple[ContractQuote, float]] = []
            control: list[tuple[ContractQuote, float]] = []
            for quote in today:
                if quote.volume is None:
                    continue
                pct = percentile_of(float(quote.volume), sample)
                if pct >= HIGH_PERCENTILE:
                    high.append((quote, pct))
                elif CONTROL_LOW <= pct <= CONTROL_HIGH:
                    control.append((quote, pct))

            # A one-armed session cannot carry the comparison, so it drops from BOTH.
            if not high or not control:
                if high or control:
                    dropped_one_armed += 1
                continue

            for arm, pool in (("high", high), ("control", control)):
                anchor, pct = max(pool, key=lambda pair: pair[0].volume or 0)
                record = score(
                    anchor, arm, pct, ticker, flow_day, entry_day, hold_days,
                    cache, settings, funnel,
                )
                if record is not None:
                    records.append(record)

    return summarise(records, settings, sessions, funnel, dropped_one_armed)


def block_bootstrap(
    records: list[Record], variant: str, reps: int = 10_000, seed: int = 20260923
) -> dict[str, Any]:
    """Session-block resampling of the arm difference, as section 7 requires.

    The resampling unit is the SESSION, not the card: one market move drives every
    card entered that day, so treating same-session cards as independent draws
    would shrink the interval to something the data does not support. Seeded, so
    the published interval is reproducible rather than merely plausible.
    """
    by_session: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for record in records:
        raw = record.exits[variant]["pnl_usd"]
        if raw is not None:
            by_session[record.entry_session].append((record.arm, float(raw)))
    sessions = sorted(by_session)
    if not sessions:
        return {"blocks": 0, "excludes_zero": False, "note": "blok yok"}

    def difference(blocks: list[list[tuple[str, float]]]) -> float | None:
        high = [v for block in blocks for arm, v in block if arm == "high"]
        control = [v for block in blocks for arm, v in block if arm == "control"]
        if not high or not control:
            return None
        return statistics.fmean(high) - statistics.fmean(control)

    observed = difference([by_session[s] for s in sessions])
    rng = random.Random(seed)
    draws = [
        d
        for _ in range(reps)
        if (d := difference([by_session[rng.choice(sessions)] for _ in sessions])) is not None
    ]
    draws.sort()
    low = draws[int(0.025 * len(draws))]
    high_end = draws[int(0.975 * len(draws))]
    return {
        "blocks": len(sessions),
        "reps": len(draws),
        "seed": seed,
        "observed_difference_usd": None if observed is None else round(observed, 2),
        "ci95": [round(low, 2), round(high_end, 2)],
        "share_at_or_below_zero": round(sum(1 for d in draws if d <= 0) / len(draws), 3),
        "excludes_zero": low > 0 or high_end < 0,
    }


def summarise(
    records: list[Record],
    settings: OptionsAlphaSettings,
    sessions: list[date],
    funnel: Funnel,
    dropped_one_armed: int,
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
        arms[arm] = {
            "n": len(rows),
            "n_with_pnl": len(values),
            "mean_pnl_usd": round(statistics.fmean(values), 2) if values else None,
            "median_pnl_usd": round(statistics.median(values), 2) if values else None,
            "total_pnl_usd": round(sum(values), 2) if values else None,
            "win_rate": round(sum(1 for v in values if v > 0) / len(values), 3) if values else None,
        }

    buckets: dict[str, Any] = {}
    dropped_buckets: list[str] = []
    grouped: dict[tuple[str, str], dict[str, list[Record]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        grouped[(record.dte_bucket, record.delta_bucket)][record.arm].append(record)
    for (dte, delta), here in sorted(grouped.items()):
        key = f"dte {dte} / delta {delta}"
        if len(here) < 2:
            dropped_buckets.append(f"{key} (yalniz {next(iter(here))})")
            continue
        buckets[key] = {
            arm: {
                "n": len(rows),
                "mean_pnl_usd": (
                    round(statistics.fmean(pnls(rows, primary)), 2) if pnls(rows, primary) else None
                ),
            }
            for arm, rows in here.items()
        }

    high = arms.get("high", {})
    control = arms.get("control", {})
    floor_met = (high.get("n_with_pnl") or 0) >= 30 and (control.get("n_with_pnl") or 0) >= 30
    high_mean, control_mean = high.get("mean_pnl_usd"), control.get("mean_pnl_usd")
    boot = block_bootstrap(records, primary)

    # The ladder frozen in PREREG_H01 section 7. EXPLORATORY_PASS requires THREE
    # things at once -- beats the control, POSITIVE after costs, and a session-block
    # interval that clears zero -- and the first draft of this function checked only
    # the first, then reported a label ("EXPLORATORY_PASS_CANDIDATE") that appears
    # nowhere in the frozen ladder. Beating a control that loses more money is not a
    # pass; see PREREG_H01 addendum 1, which settles the branch using the
    # cost-positive clause that was frozen before any number existed.
    beats = high_mean is not None and control_mean is not None and high_mean > control_mean
    cost_positive = high_mean is not None and high_mean > 0
    clears_zero = bool(boot["excludes_zero"])
    failed: list[str] = []
    if not beats:
        failed.append("yuksek kol kontrolu gecmiyor")
    if not cost_positive:
        failed.append(f"maliyet sonrasi pozitif degil ({high_mean} $)")
    if not clears_zero:
        failed.append("blok bootstrap araligi sifiri iceriyor")

    if not floor_met or high_mean is None or control_mean is None:
        verdict = "INSUFFICIENT_DATA"
        failed = ["her iki kolda 30 tamamlanmis yapi yok"]
    elif not failed:
        verdict = "EXPLORATORY_PASS"
    else:
        verdict = "REJECTED"

    return {
        "hypothesis": "H01",
        "protocol": ["docs/options-alpha-v1/PREREG_H01.md"],
        "profile_sha256": settings.profile_sha256,
        "quality_tier": settings.quality.tier,
        "sessions_available": len(sessions),
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else [],
        "trailing_sessions": TRAILING_SESSIONS,
        "arm_definition": {
            "high": f"percentile >= {HIGH_PERCENTILE}",
            "control": f"{CONTROL_LOW} <= percentile <= {CONTROL_HIGH}",
            "unused_band": f"{CONTROL_HIGH}-{HIGH_PERCENTILE}",
        },
        "primary_metric": f"net option P&L per structure, exit {primary}, "
        f"{settings.exit.primary_hold_trading_days} trading days",
        "ticker_sessions_dropped_one_armed": dropped_one_armed,
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
            "options_alpha_v1 kapsaminda ikinci aile; toplam ilan edilmis deneme 6 "
            "(H03'un 3'u + H01'in 3'u). Tek bir ailenin esigi gecmesi alti denemelik "
            "bir aramada tek basina kanit degildir."
        ),
        "records": [
            {
                "ticker": r.ticker,
                "flow_session": r.flow_session.isoformat(),
                "entry_session": r.entry_session.isoformat(),
                "symbol": r.symbol,
                "arm": r.arm,
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
    parser.add_argument("--out", default="artifacts/options-alpha-v1/h01_result.json")
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    args = parser.parse_args()

    harvest = pathlib.Path(args.harvest)
    if not harvest.exists():
        print(f"hasat yok: {harvest}")
        return 2

    settings = load_settings()
    result = run(settings, harvest)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"seans {result['sessions_available']}  pencere {result['window']}")
    print(f"tek kollu dustu: {result['ticker_sessions_dropped_one_armed']}")
    print(f"\nhuni: {result['funnel']}")
    print("\n--- KOLLAR (birincil: time_only) ---")
    for arm, stats in result["arms"].items():
        print(
            f"  {arm:8s} n={stats['n']:<4} ortalama {stats['mean_pnl_usd']} $  "
            f"medyan {stats['median_pnl_usd']} $  kazanma {stats['win_rate']}"
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
        f"{boot['observed_difference_usd']} $, %95 {boot['ci95']}, "
        f"<=0 orani {boot['share_at_or_below_zero']}, sifiri disliyor: {boot['excludes_zero']}"
    )
    print(f"ornek tabani (her iki kolda >=30): {result['sample_floor_met']}")
    for clause in result["verdict_failed_clauses"]:
        print(f"  DUSEN SART: {clause}")
    print(f"HUKUM: {result['verdict']}")
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
