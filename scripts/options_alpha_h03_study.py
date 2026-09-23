"""H03 — does a next-publication open-interest increase precede better option P&L?

    uv run python scripts/options_alpha_h03_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_H03.md`, as operationalised
by `PREREG_H03_ADDENDUM.md` (what "unusual flow" means) and corrected by
`PREREG_H03_ADDENDUM_2.md` (which observation splits the arms).

The shape, in one paragraph. On session D a contract shows unusual flow when it
clears the engine's frozen gates and its volume exceeds its open interest. Open
interest is start-of-day, so the row dated D+1 is the first publication that can
confirm whether that flow OPENED a position or closed one -- and it is published
before D+1 opens, which makes the D+1 CLOSE the earliest honest entry. The one
candidate per (ticker, session) then falls into the confirmed arm if its open
interest rose, and the control arm if it did not. Both arms are priced, sized and
exited by exactly the same frozen code the live card path uses.

No fallback anywhere. If no contract qualifies, that ticker-session produces
nothing rather than a weaker signal dressed as a strong one.

Reads only the local harvest; spends no quota. Writes one result artifact.
"""

from __future__ import annotations

import argparse
import json
import pathlib
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

# Frozen in addendum 2 section 3. Buckets exist so the two arms are compared like
# with like; a bucket that ends up with only one arm is dropped and reported.
DTE_BUCKETS = ((14, 30), (31, 45), (46, 60))
DELTA_BUCKETS = ((0.25, 0.40), (0.40, 0.55), (0.55, 0.70))

HOLD_SESSIONS = 5


def bucket_of(value: float, buckets: tuple[tuple[float, float], ...]) -> str | None:
    for low, high in buckets:
        if low <= value <= high:
            return f"{low}-{high}"
    return None


def load_session(harvest: pathlib.Path, session: date, ticker: str) -> list[ContractQuote]:
    path = harvest / session.isoformat() / f"{ticker}.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        q
        for q in (parse_chain_row(row, session, ticker) for row in payload["rows"])
        if q is not None
    ]


def open_interest_of(quotes: list[ContractQuote], symbol: str) -> int | None:
    for quote in quotes:
        if quote.option_symbol == symbol:
            return quote.open_interest
    return None


@dataclass
class Record:
    ticker: str
    flow_session: date
    entry_session: date
    symbol: str
    arm: str
    dte_bucket: str
    delta_bucket: str
    oi_before: int
    oi_after: int
    structure: str
    entry_debit: str
    quantity: int
    exits: dict[str, Any]


def structure_value(
    quotes: list[ContractQuote], legs: tuple[Any, ...]
) -> Decimal | None:
    """Closable package value that session: long leg at the bid, short at the ask."""
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


def run(settings: OptionsAlphaSettings, harvest: pathlib.Path) -> dict[str, Any]:
    sessions = sorted(
        date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir()
    )
    tickers = list(settings.universe.tickers)
    records: list[Record] = []
    funnel_totals = Funnel()
    skipped_no_candidate = 0

    # Need D, D+1 (entry) and five more sessions to hold through.
    for index in range(len(sessions) - (HOLD_SESSIONS + 1)):
        flow_day = sessions[index]
        entry_day = sessions[index + 1]
        hold_days = sessions[index + 2 : index + 2 + HOLD_SESSIONS]

        for ticker in tickers:
            chain_flow = load_session(harvest, flow_day, ticker)
            chain_entry = load_session(harvest, entry_day, ticker)
            if not chain_flow or not chain_entry:
                continue

            eligible, funnel = eligible_contracts(chain_flow, flow_day, settings)
            funnel_totals.scanned += funnel.scanned
            funnel_totals.eligible += funnel.eligible
            funnel_totals.dropped.update(funnel.dropped)

            # Addendum 1 section 2: unusual flow is volume above open interest.
            unusual = [
                q
                for q in eligible
                if q.volume is not None
                and q.open_interest is not None
                and q.volume > q.open_interest
            ]
            if not unusual:
                skipped_no_candidate += 1
                continue

            # Addendum 1 section 3: one candidate per ticker-session, no fallback.
            anchor_flow = max(unusual, key=lambda q: q.volume or 0)

            oi_before = anchor_flow.open_interest
            oi_after = open_interest_of(chain_entry, anchor_flow.option_symbol)
            if oi_before is None or oi_after is None:
                continue

            # Addendum 2 section 3: the confirmation splits the arms.
            arm = "confirmed" if oi_after > oi_before else "control"

            anchor_entry = next(
                (q for q in chain_entry if q.option_symbol == anchor_flow.option_symbol),
                None,
            )
            if anchor_entry is None or anchor_entry.delta is None:
                continue

            dte_bucket = bucket_of(float(anchor_entry.dte(entry_day)), DTE_BUCKETS)
            delta_bucket = bucket_of(abs(anchor_entry.delta), DELTA_BUCKETS)
            if dte_bucket is None or delta_bucket is None:
                continue

            entry_funnel = Funnel()
            candidate = build_candidate(anchor_entry, chain_entry, settings, entry_funnel)
            # Merge EVERY counter, not only the drops. Merging on failure alone and
            # hand-incrementing one counter on success made the printed elimination
            # chain contradict itself: ten structures past the risk gate while
            # structures_built, priced and passed_cost_gate all read zero. A funnel
            # that cannot be trusted is worse than no funnel, because it is read as
            # evidence of what the rules rejected.
            funnel_totals.structures_built += entry_funnel.structures_built
            funnel_totals.priced += entry_funnel.priced
            funnel_totals.passed_cost_gate += entry_funnel.passed_cost_gate
            funnel_totals.passed_risk_gate += entry_funnel.passed_risk_gate
            funnel_totals.dropped.update(entry_funnel.dropped)
            if candidate is None:
                continue

            observations: list[DailyObservation] = []
            for day in hold_days:
                chain_day = load_session(harvest, day, ticker)
                value = structure_value(chain_day, candidate.legs) if chain_day else None
                observations.append(
                    DailyObservation(
                        day=day,
                        exit_value=value,
                        dte=(candidate.legs[0].quote.expiry - day).days,
                        is_complete=value is not None,
                    )
                )

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
                    commission_usd=candidate.price.commission_usd
                    * Decimal(candidate.sizing.structures),
                )
                exits[variant.value] = {
                    "reason": outcome.reason.value,
                    "pnl_usd": None if outcome.pnl_usd is None else str(outcome.pnl_usd),
                    "return_on_risk": outcome.return_on_risk,
                    "held": outcome.held_trading_days,
                    "unpriced_days": outcome.unpriced_days,
                }

            records.append(
                Record(
                    ticker=ticker,
                    flow_session=flow_day,
                    entry_session=entry_day,
                    symbol=anchor_flow.option_symbol,
                    arm=arm,
                    dte_bucket=dte_bucket,
                    delta_bucket=delta_bucket,
                    oi_before=oi_before,
                    oi_after=oi_after,
                    structure=candidate.kind.value,
                    entry_debit=str(candidate.price.entry_debit),
                    quantity=candidate.sizing.structures,
                    exits=exits,
                )
            )

    return summarise(records, settings, sessions, funnel_totals, skipped_no_candidate)


def summarise(
    records: list[Record],
    settings: OptionsAlphaSettings,
    sessions: list[date],
    funnel: Funnel,
    skipped: int,
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
            "win_rate": (
                round(sum(1 for v in values if v > 0) / len(values), 3) if values else None
            ),
        }

    # Buckets with only one arm cannot support a comparison and are reported as
    # dropped rather than quietly averaged into the headline.
    buckets: dict[str, Any] = {}
    dropped_buckets: list[str] = []
    grouped: dict[tuple[str, str], dict[str, list[Record]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in records:
        grouped[(record.dte_bucket, record.delta_bucket)][record.arm].append(record)
    for (dte, delta), arms_here in sorted(grouped.items()):
        key = f"dte {dte} / delta {delta}"
        if len(arms_here) < 2:
            dropped_buckets.append(f"{key} (yalniz {next(iter(arms_here))})")
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
            for arm, rows in arms_here.items()
        }

    confirmed = arms.get("confirmed", {})
    floor_met = (confirmed.get("n_with_pnl") or 0) >= 30
    verdict = "INSUFFICIENT_DATA"
    if floor_met:
        control_mean = (arms.get("control") or {}).get("mean_pnl_usd")
        confirmed_mean = confirmed.get("mean_pnl_usd")
        if confirmed_mean is None or control_mean is None:
            verdict = "INSUFFICIENT_DATA"
        elif confirmed_mean <= control_mean:
            verdict = "REJECTED"
        else:
            verdict = "EXPLORATORY_PASS_CANDIDATE"

    return {
        "hypothesis": "H03",
        "protocol": [
            "docs/options-alpha-v1/PREREG_H03.md",
            "docs/options-alpha-v1/PREREG_H03_ADDENDUM.md",
            "docs/options-alpha-v1/PREREG_H03_ADDENDUM_2.md",
        ],
        "profile_sha256": settings.profile_sha256,
        "quality_tier": settings.quality.tier,
        "sessions_available": len(sessions),
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else [],
        "primary_metric": f"net option P&L per structure, exit {primary}, "
        f"{settings.exit.primary_hold_trading_days} trading days",
        "ticker_sessions_without_candidate": skipped,
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
        "verdict": verdict,
        "verdict_note": (
            "EXPLORATORY_PASS_CANDIDATE yalnizca on-kosullari gosterir; kesin hukum "
            "blok bootstrap belirsizligi ile birlikte verilir. B kalite veride "
            "FORWARD_PASS hicbir kosulda cikamaz."
        ),
        "records": [
            {
                "ticker": r.ticker,
                "flow_session": r.flow_session.isoformat(),
                "entry_session": r.entry_session.isoformat(),
                "symbol": r.symbol,
                "arm": r.arm,
                "dte_bucket": r.dte_bucket,
                "delta_bucket": r.delta_bucket,
                "oi_before": r.oi_before,
                "oi_after": r.oi_after,
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
    parser.add_argument("--out", default="artifacts/options-alpha-v1/h03_result.json")
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
    print(f"aday cikmayan isim-seans: {result['ticker_sessions_without_candidate']}")
    print(f"\nhuni: {result['funnel']}")
    print("\n--- KOLLAR (birincil: time_only) ---")
    for arm, stats in result["arms"].items():
        print(
            f"  {arm:10s} n={stats['n']:<4} ortalama {stats['mean_pnl_usd']} $  "
            f"medyan {stats['median_pnl_usd']} $  kazanma {stats['win_rate']}"
        )
    print("\n--- KOVALAR ---")
    for key, arms_here in result["buckets"].items():
        parts = " | ".join(f"{a}: n={v['n']} ort {v['mean_pnl_usd']}" for a, v in arms_here.items())
        print(f"  {key}  {parts}")
    for dropped in result["dropped_buckets"]:
        print(f"  DUSTU {dropped}")
    print(f"\nornek tabani saglandi: {result['sample_floor_met']}")
    print(f"HUKUM: {result['verdict']}")
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
