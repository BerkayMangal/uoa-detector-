"""H10 — does long premium pay when implied vol looks cheap against realised?

    uv run python scripts/options_alpha_h10_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_H10.md` (commit 456dbcc,
before this file existed).

    cheapness = IV(contract, D) / realised_vol20(underlying, D)
    realised_vol20 = stdev of the 20 daily returns BEFORE D, annualised

  arm A (cheap): cheapness <= 0.90
  arm B (rich):  cheapness >= 1.10
  the 0.90-1.10 band is unused, 0.20 wide

Cheapness is assigned at SESSION level, not contract level, and the pre-registration
says why: the ratio's denominator is the underlying's realised vol, identical for
every contract of that ticker-session, so skew and term move it only slightly while
session-to-session variation is large. The draft split arms inside a session and
collapsed to 68 pairs against a floor of 30. Each (ticker, session) therefore lands
in exactly ONE arm, represented by its highest-volume eligible contract -- the anchor
the study would trade anyway.

Unlike H01 and H04 there is no volume-percentile filter: this family is about the
price of premium, not about flow. That makes comparison with them looser and the
report says so.

Two things section 7 makes mandatory and this file therefore computes:

  * per-arm composition -- mean IV, spread as a percent of mid, DTE, delta;
  * the interpretation rule frozen in section 4b.1. The cheap arm sits on
    systematically wider spreads (3.5% of mid against 2.8%), a confound that runs
    AGAINST the hypothesised arm because the cost model charges the spread inside the
    entry and exit prices. So if the cheap arm wins the finding is conservative, and
    if it loses the artifact must say the cost disadvantage could not be separated --
    not that cheapness fails.

Nothing here says anything about SELLING vol. `docs/edge_to_money.md`'s verdict is
untouched by whatever this returns.

Reads the local harvest plus the committed data/study_f/bars.csv. Spends no quota.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
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

# Frozen in PREREG_H10 sections 3, 4 and 4b.
TRAILING_SESSIONS = 20
TRADING_DAYS = 252
ARM_A_MAX = 0.90
ARM_B_MIN = 1.10

# Frozen in sections 6 and 7, identical to H01/H04 so the numbers stay comparable.
HOLD_SESSIONS = 5
DTE_BUCKETS = ((14, 30), (31, 45), (46, 60))
DELTA_BUCKETS = ((0.25, 0.40), (0.40, 0.55), (0.55, 0.70))
SAMPLE_FLOOR = 30
BOOTSTRAP_SEED = 20260923
BOOTSTRAP_REPS = 10_000

ARM_A, ARM_B = "A_cheap", "B_rich"


def bucket_of(value: float, buckets: tuple[tuple[float, float], ...]) -> str | None:
    for low, high in buckets:
        if low <= value <= high:
            return f"{low}-{high}"
    return None


def load_closes(path: pathlib.Path, tickers: list[str]) -> dict[str, dict[date, float]]:
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["ticker"] in tickers and row["close"]:
                closes[row["ticker"]][date.fromisoformat(row["day"])] = float(row["close"])
    return closes


def realised_vol(closes: dict[date, float], day: date) -> float | None:
    """Annualised stdev of the 20 returns strictly BEFORE `day`.

    The day's own return is excluded: a window containing the move it scales shrinks
    exactly when the move is large, which flatters whichever arm the move landed in.
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


class ChainCache:
    """Full chains behind a rolling window, keyed by (ticker, session)."""

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
    cheapness: float
    iv: float
    spread_pct: float
    volume: int
    dte_bucket: str
    delta_bucket: str
    delta: float
    dte: int
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
    cheapness: float,
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

    dte = anchor_entry.dte(entry_day)
    dte_bucket = bucket_of(float(dte), DTE_BUCKETS)
    delta_bucket = bucket_of(abs(anchor_entry.delta), DELTA_BUCKETS)
    if dte_bucket is None or delta_bucket is None:
        return None

    spread = spread_pct_of_mid(anchor_flow.bid, anchor_flow.ask)
    if spread is None or anchor_flow.implied_volatility is None:
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
    # that field already nets commission and evaluate_exit subtracts commission_usd
    # itself, so the net figure would charge the fees twice. Same input H03, H01 and
    # H04 used, which is what keeps the families comparable.
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
        cheapness=round(cheapness, 4),
        iv=anchor_flow.implied_volatility,
        spread_pct=round(spread, 2),
        volume=anchor_flow.volume or 0,
        dte_bucket=dte_bucket,
        delta_bucket=delta_bucket,
        delta=abs(anchor_entry.delta),
        dte=dte,
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

    records: list[Record] = []
    dead_band = 0
    no_vol = 0

    for index in range(TRAILING_SESSIONS, len(sessions) - (HOLD_SESSIONS + 1)):
        flow_day = sessions[index]
        entry_day = sessions[index + 1]
        hold_days = sessions[index + 2 : index + 2 + HOLD_SESSIONS]

        for ticker in tickers:
            chain = cache.get(ticker, flow_day)
            if not chain:
                continue
            kept, one = eligible_contracts(chain, flow_day, settings)
            funnel.scanned += one.scanned
            funnel.eligible += one.eligible
            funnel.dropped.update(one.dropped)
            if not kept:
                continue

            vol = realised_vol(closes.get(ticker, {}), flow_day)
            if vol is None:
                no_vol += 1
                continue

            # The session's representative: its highest-volume eligible contract.
            usable = [
                q
                for q in kept
                if q.volume is not None and q.implied_volatility is not None and q.delta is not None
            ]
            if not usable:
                continue
            anchor = max(usable, key=lambda q: q.volume or 0)
            if anchor.implied_volatility is None:
                continue
            cheapness = anchor.implied_volatility / vol

            # Session-level: this ticker-session lands in exactly one arm.
            if cheapness <= ARM_A_MAX:
                arm = ARM_A
            elif cheapness >= ARM_B_MIN:
                arm = ARM_B
            else:
                dead_band += 1
                continue

            record = score(
                anchor, arm, cheapness, ticker, flow_day, entry_day, hold_days,
                cache, settings, funnel,
            )
            if record is not None:
                records.append(record)

    return summarise(records, settings, sessions, funnel, dead_band, no_vol, bars)


def block_bootstrap(records: list[Record], variant: str) -> dict[str, Any]:
    """Session-block resampling of the arm difference, as section 7 requires.

    The unit is the SESSION even though arms are already assigned per session: two
    tickers entered on the same day still share that day's market move, so cards are
    not independent draws.
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
        a = [v for block in chosen for arm, v in block if arm == ARM_A]
        b = [v for block in chosen for arm, v in block if arm == ARM_B]
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
    no_vol: int,
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
        arms[arm] = {
            "n": len(rows),
            "n_with_pnl": len(values),
            "mean_pnl_usd": round(statistics.fmean(values), 2) if values else None,
            "median_pnl_usd": round(statistics.median(values), 2) if values else None,
            "total_pnl_usd": round(sum(values), 2) if values else None,
            "win_rate": round(sum(1 for v in values if v > 0) / len(values), 3) if values else None,
            # Section 7 makes this composition mandatory: 4b.1's interpretation rule
            # rests on it, so it is computed here rather than left to a reader.
            "mean_cheapness": round(statistics.fmean(r.cheapness for r in rows), 3),
            "mean_iv": round(statistics.fmean(r.iv for r in rows), 3),
            "mean_spread_pct_of_mid": round(statistics.fmean(r.spread_pct for r in rows), 2),
            "mean_dte": round(statistics.fmean(r.dte for r in rows), 1),
            "mean_delta": round(statistics.fmean(r.delta for r in rows), 3),
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

    arm_a, arm_b = arms.get(ARM_A, {}), arms.get(ARM_B, {})
    boot = block_bootstrap(records, primary)
    floor_met = (arm_a.get("n_with_pnl") or 0) >= SAMPLE_FLOOR and (
        arm_b.get("n_with_pnl") or 0
    ) >= SAMPLE_FLOOR
    mean_a, mean_b = arm_a.get("mean_pnl_usd"), arm_b.get("mean_pnl_usd")

    failed: list[str] = []
    if not (mean_a is not None and mean_b is not None and mean_a > mean_b):
        failed.append("A (ucuz) kolu B (pahali) kolunu gecmiyor")
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

    # Section 4b.1, frozen before any number existed. The cheap arm was measured to
    # sit on wider spreads, so the confound runs against it and the direction of the
    # result decides which sentence is honest.
    spread_gap = (
        round(
            (arm_a.get("mean_spread_pct_of_mid") or 0.0)
            - (arm_b.get("mean_spread_pct_of_mid") or 0.0),
            2,
        )
        if arm_a and arm_b
        else None
    )
    if mean_a is not None and mean_b is not None and mean_a > mean_b:
        cost_note = (
            "A kolu daha GENIS makasa ragmen B'yi gecti; bulgu bu yonde "
            "MUHAFAZAKARDIR (on-kayit 4b.1)."
        )
    elif spread_gap is not None and spread_gap > 0:
        cost_note = (
            "A kolu daha GENIS makasa dusuyor ve kaybetti; kaybin bir kismi maliyettir. "
            "On-kayit 4b.1 geregi bu sonuc 'ucuzluk ise yaramiyor' DEGIL, "
            "'maliyet dezavantaji ayristirilamadi' olarak okunur."
        )
    else:
        cost_note = (
            "A kolu daha genis makasa dusmedi; 4b.1'in maliyet serhi bu kosumda "
            "uygulanmiyor."
        )

    return {
        "hypothesis": "H10",
        "protocol": ["docs/options-alpha-v1/PREREG_H10.md"],
        "protocol_frozen_commit": "456dbcc",
        "profile_sha256": settings.profile_sha256,
        "quality_tier": settings.quality.tier,
        "underlying_bars": str(bars),
        "sessions_available": len(sessions),
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else [],
        "arm_definition": {
            ARM_A: f"cheapness <= {ARM_A_MAX}",
            ARM_B: f"cheapness >= {ARM_B_MIN}",
            "unused_band": f"{ARM_A_MAX}-{ARM_B_MIN}",
            "cheapness": "IV(contract, D) / annualised stdev of the 20 returns before D",
            "level": "session: one arm per (ticker, session), representative = "
            "highest-volume eligible contract",
        },
        "candidate_rule": "engine's frozen gates only; NO volume-percentile filter, "
        "because this family is about the price of premium and not about flow",
        "primary_metric": f"net option P&L per structure, exit {primary}, "
        f"{settings.exit.primary_hold_trading_days} trading days",
        "dead_band_dropped": dead_band,
        "no_realised_vol_dropped": no_vol,
        "funnel": funnel.as_dict(),
        "arms": arms,
        "spread_gap_a_minus_b": spread_gap,
        "cost_interpretation": cost_note,
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
        "does_not_touch": (
            "Bu aile vol SATMA hakkinda hicbir sey soylemez; docs/edge_to_money.md'nin "
            "hukmu degismez."
        ),
        "multiple_testing": (
            "options_alpha_v1 kapsaminda dorduncu KOSAN aile; toplam ilan edilmis "
            "deneme 12 (H03 3 + H01 3 + H04 3 + H10 3). H06 kosulmadi, deneme "
            "tuketmedi. Tek bir ailenin esigi gecmesi 12 denemelik bir aramada tek "
            "basina kanit degildir."
        ),
        "records": [
            {
                "ticker": r.ticker,
                "flow_session": r.flow_session.isoformat(),
                "entry_session": r.entry_session.isoformat(),
                "symbol": r.symbol,
                "arm": r.arm,
                "cheapness": r.cheapness,
                "iv": r.iv,
                "spread_pct": r.spread_pct,
                "volume": r.volume,
                "dte_bucket": r.dte_bucket,
                "delta_bucket": r.delta_bucket,
                "dte": r.dte,
                "delta": round(r.delta, 3),
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
    parser.add_argument("--out", default="artifacts/options-alpha-v1/h10_result.json")
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
    print(
        f"olu bantta dusen: {result['dead_band_dropped']}  "
        f"gerceklesen vol yok: {result['no_realised_vol_dropped']}"
    )
    print(f"\nhuni: {result['funnel']}")
    print("\n--- KOLLAR (birincil: time_only) ---")
    for arm, stats in result["arms"].items():
        print(
            f"  {arm:8s} n={stats['n']:<4} ortalama {stats['mean_pnl_usd']} $  "
            f"medyan {stats['median_pnl_usd']} $  kazanma {stats['win_rate']}"
        )
        print(
            f"           ucuzluk {stats['mean_cheapness']}  IV {stats['mean_iv']}  "
            f"makas %{stats['mean_spread_pct_of_mid']}  DTE {stats['mean_dte']}  "
            f"delta {stats['mean_delta']}"
        )
    print(f"\nmakas farki (A - B): {result['spread_gap_a_minus_b']} puan")
    print(f"MALIYET SERHI: {result['cost_interpretation']}")
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
