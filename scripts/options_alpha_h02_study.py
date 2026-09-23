"""H02 — does unusual volume that REPEATS in a contract beat a one-off spike?

    uv run python scripts/options_alpha_h02_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_H02.md` (commit 84192d1,
before this file existed).

Unusualness is H01's measure, unchanged: a contract's session volume as a percentile
of the distribution of eligible-contract volumes for that ticker over the previous 20
sessions. Contracts at or above the 90th percentile on D are the pool. The ARM is the
contract's own history:

    repetition(contract, D) = sessions in D-1..D-5 where it was also >= 90th percentile

  arm A (repeat):  repetition >= 2
  arm B (one-off): repetition == 0
  repetition == 1 is the dead band, deliberately unused

Same-session PAIRED, as section 4 freezes: a (ticker, session) produces only when it
carries a candidate in BOTH arms, so the comparison sits inside one name and one day
and the market-day move drops out of the difference. One anchor per arm -- the
highest-volume contract meeting that arm's condition, no fallback.

Split sweeps are not a problem here and the pre-registration says why: flow is read
from DAILY chain volume, which has already summed the intraday prints. The price is
that aggressor side, sweep/block and intraday timing do not exist in this study.

Section 7 makes two things mandatory and this file therefore computes them:

  * per-arm composition -- volume, open interest, spread, IV, DTE, delta and the
    ticker distribution;
  * the liquidity rule. If one name carries more than half of arm A, the result is
    written as "liquidity selection could not be separated", not as "repetition
    works". The power analysis measured 13%; the run confirms or refutes that.

Reads the local harvest only. Spends no quota.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
from collections import Counter, defaultdict
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

# Frozen in PREREG_H02 sections 4 and 4b.
TRAILING_SESSIONS = 20
HIGH_PERCENTILE = 90.0
LOOKBACK = 5
ARM_A_MIN_REPEATS = 2
ARM_B_REPEATS = 0

# Frozen in sections 6 and 7, identical to H01/H04/H10 so the numbers stay comparable.
HOLD_SESSIONS = 5
DTE_BUCKETS = ((14, 30), (31, 45), (46, 60))
DELTA_BUCKETS = ((0.25, 0.40), (0.40, 0.55), (0.55, 0.70))
SAMPLE_FLOOR = 30
BOOTSTRAP_SEED = 20260923
BOOTSTRAP_REPS = 10_000
LIQUIDITY_SHARE_LIMIT = 0.50

ARM_A, ARM_B = "A_repeat", "B_one_off"


def bucket_of(value: float, buckets: tuple[tuple[float, float], ...]) -> str | None:
    for low, high in buckets:
        if low <= value <= high:
            return f"{low}-{high}"
    return None


def percentile_of(value: float, sample: list[float]) -> float:
    """Share of the sample at or below ``value``, as a percentage (H01's measure)."""
    if not sample:
        return 0.0
    return 100.0 * sum(1 for x in sample if x <= value) / len(sample)


def spread_pct_of_mid(bid: Decimal | None, ask: Decimal | None) -> float | None:
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / Decimal(2)
    if mid <= 0:
        return None
    return float((ask - bid) / mid) * 100.0


def assign_arm(repetition: int) -> str | None:
    """Section 4's table. ``None`` is the dead band {1}."""
    if repetition >= ARM_A_MIN_REPEATS:
        return ARM_A
    if repetition == ARM_B_REPEATS:
        return ARM_B
    return None


def high_sets(
    eligible_by: dict[tuple[str, date], list[ContractQuote]],
    sessions: list[date],
    tickers: list[str],
) -> dict[tuple[str, date], set[str]]:
    """Symbols at or above the 90th percentile of the ticker's own trailing window.

    The window is strictly BEFORE the session, so a day never enters its own
    comparison. Repetition reads these sets for D-1..D-5 and D itself; nothing from
    D+1 reaches an arm decision.
    """
    out: dict[tuple[str, date], set[str]] = {}
    for index in range(TRAILING_SESSIONS, len(sessions)):
        day = sessions[index]
        trailing = sessions[index - TRAILING_SESSIONS : index]
        for ticker in tickers:
            today = [q for q in eligible_by.get((ticker, day), []) if q.volume is not None]
            if not today:
                continue
            sample = [
                float(q.volume)
                for past in trailing
                for q in eligible_by.get((ticker, past), [])
                if q.volume is not None
            ]
            if not sample:
                continue
            out[(ticker, day)] = {
                q.option_symbol
                for q in today
                if percentile_of(float(q.volume or 0), sample) >= HIGH_PERCENTILE
            }
    return out


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
    repetition: int
    volume: int
    open_interest: int | None
    iv: float | None
    spread_pct: float | None
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
    repetition: int,
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
    # itself, so the net figure would charge the fees twice. Same input H03, H01, H04
    # and H10 used, which is what keeps the families comparable.
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

    spread = spread_pct_of_mid(anchor_flow.bid, anchor_flow.ask)
    return Record(
        ticker=ticker,
        flow_session=flow_day,
        entry_session=entry_day,
        symbol=anchor_flow.option_symbol,
        arm=arm,
        repetition=repetition,
        volume=anchor_flow.volume or 0,
        open_interest=anchor_flow.open_interest,
        iv=anchor_flow.implied_volatility,
        spread_pct=None if spread is None else round(spread, 2),
        dte_bucket=dte_bucket,
        delta_bucket=delta_bucket,
        delta=abs(anchor_entry.delta),
        dte=dte,
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

    # Pass one: eligible contracts only (about seventy per file), held for the whole
    # harvest because both the percentile window and the repetition count read back.
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

    highs = high_sets(eligible_by, sessions, tickers)

    records: list[Record] = []
    dead_band = 0
    one_armed = 0
    paired_sessions = 0

    # First flow day needs 20 trailing sessions for its own percentile AND for each
    # of the five lookback days' percentiles.
    first = TRAILING_SESSIONS + LOOKBACK
    for index in range(first, len(sessions) - (HOLD_SESSIONS + 1)):
        flow_day = sessions[index]
        entry_day = sessions[index + 1]
        hold_days = sessions[index + 2 : index + 2 + HOLD_SESSIONS]
        past_days = sessions[index - LOOKBACK : index]

        for ticker in tickers:
            today_high = highs.get((ticker, flow_day), set())
            if not today_high:
                continue
            quotes = {q.option_symbol: q for q in eligible_by.get((ticker, flow_day), [])}

            pools: dict[str, list[tuple[ContractQuote, int]]] = defaultdict(list)
            for symbol in today_high:
                repetition = sum(
                    1 for past in past_days if symbol in highs.get((ticker, past), set())
                )
                arm = assign_arm(repetition)
                if arm is None:
                    dead_band += 1
                    continue
                pools[arm].append((quotes[symbol], repetition))

            # Paired: a one-armed session cannot carry the comparison, so it drops
            # from BOTH arms.
            if not pools[ARM_A] or not pools[ARM_B]:
                if pools[ARM_A] or pools[ARM_B]:
                    one_armed += 1
                continue
            paired_sessions += 1

            for arm in (ARM_A, ARM_B):
                anchor, repetition = max(pools[arm], key=lambda pair: pair[0].volume or 0)
                record = score(
                    anchor, arm, repetition, ticker, flow_day, entry_day, hold_days,
                    cache, settings, funnel,
                )
                if record is not None:
                    records.append(record)

    return summarise(records, settings, sessions, funnel, dead_band, one_armed, paired_sessions)


def block_bootstrap(records: list[Record], variant: str) -> dict[str, Any]:
    """Session-block resampling of the arm difference, as section 7 requires.

    The unit is the SESSION, not the card: every card entered on a day shares that
    day's market move, so same-session cards are not independent draws.
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


def mean_of(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def summarise(
    records: list[Record],
    settings: OptionsAlphaSettings,
    sessions: list[date],
    funnel: Funnel,
    dead_band: int,
    one_armed: int,
    paired_sessions: int,
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
        names = Counter(r.ticker for r in rows)
        arms[arm] = {
            "n": len(rows),
            "n_with_pnl": len(values),
            "mean_pnl_usd": round(statistics.fmean(values), 2) if values else None,
            "median_pnl_usd": round(statistics.median(values), 2) if values else None,
            "total_pnl_usd": round(sum(values), 2) if values else None,
            "win_rate": round(sum(1 for v in values if v > 0) / len(values), 3) if values else None,
            # Section 7's mandatory composition.
            "mean_repetition": mean_of([float(r.repetition) for r in rows]),
            "mean_volume": mean_of([float(r.volume) for r in rows]),
            "mean_open_interest": mean_of(
                [float(r.open_interest) for r in rows if r.open_interest is not None]
            ),
            "mean_spread_pct_of_mid": mean_of(
                [r.spread_pct for r in rows if r.spread_pct is not None]
            ),
            "mean_iv": mean_of([r.iv for r in rows if r.iv is not None]),
            "mean_dte": mean_of([float(r.dte) for r in rows]),
            "mean_delta": mean_of([r.delta for r in rows]),
            "ticker_counts": dict(names.most_common()),
            "top_ticker_share": round(names.most_common(1)[0][1] / len(rows), 3),
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

    # How many pairs survived with BOTH arms scored -- reported, not used to filter:
    # section 4 pairs at the candidate stage, as H01 did.
    arms_by_pair: dict[tuple[str, date], set[str]] = defaultdict(set)
    for record in records:
        arms_by_pair[(record.ticker, record.flow_session)].add(record.arm)
    complete_pairs = sum(1 for here in arms_by_pair.values() if len(here) == 2)

    arm_a, arm_b = arms.get(ARM_A, {}), arms.get(ARM_B, {})
    boot = block_bootstrap(records, primary)
    floor_met = (arm_a.get("n_with_pnl") or 0) >= SAMPLE_FLOOR and (
        arm_b.get("n_with_pnl") or 0
    ) >= SAMPLE_FLOOR
    mean_a, mean_b = arm_a.get("mean_pnl_usd"), arm_b.get("mean_pnl_usd")

    failed: list[str] = []
    if not (mean_a is not None and mean_b is not None and mean_a > mean_b):
        failed.append("A (tekrarlayan) kolu B (tek seferlik) kolunu gecmiyor")
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

    # Section 7's liquidity rule, frozen before any number existed.
    top_share_a = arm_a.get("top_ticker_share")
    liquidity_confounded = top_share_a is not None and top_share_a > LIQUIDITY_SHARE_LIMIT
    liquidity_note = (
        f"A kolunun en buyuk isim payi %{top_share_a * 100:.0f} > %50: sonuc "
        "'tekrar ise yariyor' DEGIL, 'likidite secimi ayristirilamadi' olarak okunur "
        "(on-kayit §7)."
        if liquidity_confounded
        else (
            f"A kolunun en buyuk isim payi %{(top_share_a or 0) * 100:.0f} <= %50: "
            "§7'nin likidite serhi bu kosumda tetiklenmedi."
        )
    )

    return {
        "hypothesis": "H02",
        "protocol": ["docs/options-alpha-v1/PREREG_H02.md"],
        "protocol_frozen_commit": "84192d1",
        "profile_sha256": settings.profile_sha256,
        "quality_tier": settings.quality.tier,
        "sessions_available": len(sessions),
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else [],
        "arm_definition": {
            ARM_A: f"repetition >= {ARM_A_MIN_REPEATS}",
            ARM_B: f"repetition == {ARM_B_REPEATS}",
            "unused_band": "repetition == 1",
            "repetition": f"sessions in D-1..D-{LOOKBACK} where the same contract was "
            f">= {HIGH_PERCENTILE:.0f}th percentile of its ticker's trailing "
            f"{TRAILING_SESSIONS}-session eligible-volume distribution",
            "pool": f"contracts >= {HIGH_PERCENTILE:.0f}th percentile on D",
            "level": "contract; same-session PAIRED, one anchor per arm = highest volume",
        },
        "candidate_rule": "engine's frozen gates only, unchanged",
        "primary_metric": f"net option P&L per structure, exit {primary}, "
        f"{settings.exit.primary_hold_trading_days} trading days",
        "dead_band_dropped": dead_band,
        "one_armed_sessions_dropped": one_armed,
        "paired_sessions": paired_sessions,
        "complete_pairs_scored": complete_pairs,
        "funnel": funnel.as_dict(),
        "arms": arms,
        "liquidity_confounded": liquidity_confounded,
        "liquidity_interpretation": liquidity_note,
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
        "cannot_say": (
            "Gun ici sira, fill, agresor tarafi ve sweep/blok ayrimi yok (B kalite, "
            "gunluk hacim). Bu aile 'kurumsal sweep tekrari' hakkinda degil, 'ayni "
            "kontrata birden fazla gun olagandisi hacim donmesi' hakkindadir."
        ),
        "multiple_testing": (
            "options_alpha_v1 kapsaminda besinci KOSAN aile; toplam ilan edilmis "
            "deneme 15 (H03 3 + H01 3 + H04 3 + H10 3 + H02 3). H06 kosulmadi, deneme "
            "tuketmedi. Tek bir ailenin esigi gecmesi 15 denemelik bir aramada tek "
            "basina kanit degildir."
        ),
        "records": [
            {
                "ticker": r.ticker,
                "flow_session": r.flow_session.isoformat(),
                "entry_session": r.entry_session.isoformat(),
                "symbol": r.symbol,
                "arm": r.arm,
                "repetition": r.repetition,
                "volume": r.volume,
                "open_interest": r.open_interest,
                "iv": r.iv,
                "spread_pct": r.spread_pct,
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
    parser.add_argument("--out", default="artifacts/options-alpha-v1/h02_result.json")
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    args = parser.parse_args()

    harvest = pathlib.Path(args.harvest)
    if not harvest.is_dir():
        print(f"hasat yok: {harvest}")
        return 2

    settings = load_settings()
    result = run(settings, harvest)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"seans {result['sessions_available']}  pencere {result['window']}")
    print(
        f"olu bantta (tekrar=1) dusen aday: {result['dead_band_dropped']}  "
        f"tek kollu seans-isim: {result['one_armed_sessions_dropped']}  "
        f"esli seans-isim: {result['paired_sessions']}  "
        f"iki kolu da skorlanan cift: {result['complete_pairs_scored']}"
    )
    print(f"\nhuni: {result['funnel']}")
    print("\n--- KOLLAR (birincil: time_only) ---")
    for arm, stats in result["arms"].items():
        print(
            f"  {arm:10s} n={stats['n']:<4} ortalama {stats['mean_pnl_usd']} $  "
            f"medyan {stats['median_pnl_usd']} $  kazanma {stats['win_rate']}"
        )
        print(
            f"             tekrar {stats['mean_repetition']}  hacim {stats['mean_volume']}  "
            f"OI {stats['mean_open_interest']}  makas %{stats['mean_spread_pct_of_mid']}  "
            f"IV {stats['mean_iv']}  DTE {stats['mean_dte']}  delta {stats['mean_delta']}"
        )
        print(
            f"             isimler {stats['ticker_counts']}  "
            f"en buyuk pay %{stats['top_ticker_share'] * 100:.0f}"
        )
    print(f"\nLIKIDITE SERHI: {result['liquidity_interpretation']}")
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
