"""S01 — where does the shared setup's loss come from?

    uv run python scripts/options_alpha_s01_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_S01.md` before this file
existed. Not a hypothesis family: no pass verdict, no trial spent.

Every priced `time_only` record of the five families that ran on the v2 engine is
split into five parts that telescope exactly to the engine's net P&L:

    market          = (M5 - M0)   * 100 * q      mid-to-mid package move
    entry_spread    = (RAW - M0)  * 100 * q      paid by opening at ask/bid
    entry_latency   = (DEB - RAW) * 100 * q      the 2% haircut on the entry
    exit_spread     = (M5 - EXIT) * 100 * q      paid by closing at bid/ask
    commission      = round-trip fees

    net = market - entry_spread - entry_latency - exit_spread - commission

The identity is algebraic, so the reconciliation gate is really a check that this
file rebuilds the engine's legs, entry debit, exit value and fees; if more than 2%
of records disagree the study stops and interprets nothing (section 4).

A seeded random-anchor baseline runs the same gates with no flow filter at all, to
ask whether the families' anchors differ from picking any eligible contract.

Reads the local harvest and the committed result artifacts. Spends no quota.
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
ARTIFACTS = pathlib.Path("artifacts/options-alpha-v1")
SIGNAL_FILES = (
    "h03_result_v2.json",
    "h01_result_v2.json",
    "h04_result_v2.json",
    "h10_result_v2.json",
    "h02_result.json",
)

# Frozen in PREREG_S01 sections 4-7, and identical to the families where shared.
TRAILING_SESSIONS = 20
HOLD_SESSIONS = 5
DTE_BUCKETS = ((14, 30), (31, 45), (46, 60))
DELTA_BUCKETS = ((0.25, 0.40), (0.40, 0.55), (0.55, 0.70))
RECONCILE_TOLERANCE_USD = Decimal("0.02")
RECONCILE_MAX_FAIL_SHARE = 0.02
COST_BOUND_SHARE = 0.75
BOOTSTRAP_SEED = 20260923
BOOTSTRAP_REPS = 10_000
RANDOM_SEED = 20260923
ETF_NAMES = ("QQQ", "SPY")

COMPONENTS = ("market", "entry_spread", "entry_latency", "exit_spread", "commission")


def bucket_of(value: float, buckets: tuple[tuple[float, float], ...]) -> str | None:
    for low, high in buckets:
        if low <= value <= high:
            return f"{low}-{high}"
    return None


class ChainCache:
    """Full chains behind a rolling window, keyed by (ticker, session)."""

    def __init__(self, harvest: pathlib.Path, limit: int = 120) -> None:
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


def raw_mid(quote: ContractQuote) -> Decimal | None:
    if quote.bid is None or quote.ask is None:
        return None
    return (quote.bid + quote.ask) / Decimal(2)


def package(
    quotes: list[ContractQuote], legs: tuple[Any, ...], how: str
) -> Decimal | None:
    """Package value on one session: 'mid', or 'exit' (long bid, short ask)."""
    by_symbol = {q.option_symbol: q for q in quotes}
    total = Decimal(0)
    for leg in legs:
        quote = by_symbol.get(leg.quote.option_symbol)
        if quote is None:
            return None
        if how == "mid":
            price = raw_mid(quote)
        else:
            price = quote.bid if leg.is_long else quote.ask
            if price is not None and price <= 0:
                price = None
        if price is None:
            return None
        total += price if leg.is_long else -price
    return total


@dataclass
class Trade:
    source: str  # "signal" or "random"
    families: tuple[str, ...]
    ticker: str
    entry_session: date
    symbol: str
    structure: str
    quantity: int
    engine_pnl: Decimal
    parts: dict[str, Decimal] | None
    net: Decimal | None
    entry_mid_usd: Decimal | None
    note: str | None


def decompose(
    ticker: str,
    symbol: str,
    entry_day: date,
    exit_day: date,
    quantity: int,
    cache: ChainCache,
    settings: OptionsAlphaSettings,
) -> tuple[dict[str, Decimal] | None, Decimal | None, Decimal | None, str | None, str | None]:
    """Section 4's identity for one trade. Returns parts, net, M0 in USD, kind, note."""
    chain_entry = cache.get(ticker, entry_day)
    anchor = next((q for q in chain_entry if q.option_symbol == symbol), None)
    if anchor is None:
        return None, None, None, None, "ankraj giris zincirinde yok"
    candidate = build_candidate(anchor, chain_entry, settings, Funnel())
    if candidate is None:
        return None, None, None, None, "motor aday uretmedi"
    if candidate.sizing.structures != quantity:
        return None, None, None, candidate.kind.value, (
            f"adet uyusmuyor: motor {candidate.sizing.structures}, kayit {quantity}"
        )

    legs = candidate.legs
    m0 = package(chain_entry, legs, "mid")
    # RAW: long at the ask, short at the bid, before the latency haircut.
    raw = Decimal(0)
    for leg in legs:
        price = leg.quote.ask if leg.is_long else leg.quote.bid
        if price is None:
            return None, None, None, candidate.kind.value, "giris kotasyonu eksik"
        raw += price if leg.is_long else -price
    chain_exit = cache.get(ticker, exit_day)
    m5 = package(chain_exit, legs, "mid")
    exit_value = package(chain_exit, legs, "exit")
    if m0 is None or m5 is None or exit_value is None:
        return None, None, None, candidate.kind.value, "mid veya cikis degeri hesaplanamadi"

    deb = candidate.price.entry_debit
    scale = Decimal(100) * Decimal(quantity)
    commission = candidate.price.commission_usd * Decimal(quantity)
    parts = {
        "market": (m5 - m0) * scale,
        "entry_spread": (raw - m0) * scale,
        "entry_latency": (deb - raw) * scale,
        "exit_spread": (m5 - exit_value) * scale,
        "commission": commission,
    }
    net = (
        parts["market"]
        - parts["entry_spread"]
        - parts["entry_latency"]
        - parts["exit_spread"]
        - parts["commission"]
    )
    return parts, net, m0 * scale, candidate.kind.value, None


def load_signal_trades(
    sessions: list[date], cache: ChainCache, settings: OptionsAlphaSettings
) -> tuple[list[Trade], int]:
    """Unique (ticker, entry, anchor) trades across the five families."""
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    families: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    total = 0
    for name in SIGNAL_FILES:
        payload = json.loads((ARTIFACTS / name).read_text(encoding="utf-8"))
        for record in payload["records"]:
            if record["exits"]["time_only"]["pnl_usd"] is None:
                continue
            total += 1
            key = (record["ticker"], record["entry_session"], record["symbol"])
            families[key].append(payload["hypothesis"])
            seen.setdefault(key, record)

    trades: list[Trade] = []
    for key, record in seen.items():
        entry_day = date.fromisoformat(record["entry_session"])
        exit_day = sessions[sessions.index(entry_day) + HOLD_SESSIONS]
        parts, net, m0_usd, kind, note = decompose(
            record["ticker"], record["symbol"], entry_day, exit_day,
            int(record["quantity"]), cache, settings,
        )
        trades.append(
            Trade(
                source="signal",
                families=tuple(sorted(set(families[key]))),
                ticker=record["ticker"],
                entry_session=entry_day,
                symbol=record["symbol"],
                structure=kind or record["structure"],
                quantity=int(record["quantity"]),
                engine_pnl=Decimal(record["exits"]["time_only"]["pnl_usd"]),
                parts=parts,
                net=net,
                entry_mid_usd=m0_usd,
                note=note,
            )
        )
    return trades, total


def random_trades(
    sessions: list[date], cache: ChainCache, settings: OptionsAlphaSettings
) -> tuple[list[Trade], int]:
    """Section 6: one seeded random eligible anchor per (ticker, flow session)."""
    rng = random.Random(RANDOM_SEED)
    tickers = list(settings.universe.tickers)
    trades: list[Trade] = []
    drawn = 0
    for index in range(TRAILING_SESSIONS, len(sessions) - (HOLD_SESSIONS + 1)):
        flow_day, entry_day = sessions[index], sessions[index + 1]
        hold_days = sessions[index + 2 : index + 2 + HOLD_SESSIONS]
        for ticker in tickers:
            chain = cache.get(ticker, flow_day)
            if not chain:
                continue
            kept, _ = eligible_contracts(chain, flow_day, settings)
            if not kept:
                continue
            pool = sorted(kept, key=lambda q: q.option_symbol)
            anchor_flow = rng.choice(pool)
            drawn += 1

            chain_entry = cache.get(ticker, entry_day)
            anchor = next(
                (q for q in chain_entry if q.option_symbol == anchor_flow.option_symbol), None
            )
            if anchor is None or anchor.delta is None:
                continue
            if bucket_of(float(anchor.dte(entry_day)), DTE_BUCKETS) is None:
                continue
            if bucket_of(abs(anchor.delta), DELTA_BUCKETS) is None:
                continue
            candidate = build_candidate(anchor, chain_entry, settings, Funnel())
            if candidate is None:
                continue

            observations = []
            for day in hold_days:
                chain_day = cache.get(ticker, day)
                value = package(chain_day, candidate.legs, "exit") if chain_day else None
                value = None if value is None else value.quantize(Decimal("0.01"))
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
            outcome = evaluate_exit(
                variant=ExitVariantId.TIME_ONLY,
                entry_debit=candidate.price.entry_debit,
                observations=tuple(observations),
                settings=settings,
                structures=candidate.sizing.structures,
                max_profit_per_share=max_profit,
                commission_usd=candidate.price.commission_usd
                * Decimal(candidate.sizing.structures),
            )
            if outcome.pnl_usd is None:
                continue
            parts, net, m0_usd, kind, note = decompose(
                ticker, anchor.option_symbol, entry_day, hold_days[-1],
                candidate.sizing.structures, cache, settings,
            )
            trades.append(
                Trade(
                    source="random",
                    families=(),
                    ticker=ticker,
                    entry_session=entry_day,
                    symbol=anchor.option_symbol,
                    structure=kind or candidate.kind.value,
                    quantity=candidate.sizing.structures,
                    engine_pnl=outcome.pnl_usd,
                    parts=parts,
                    net=net,
                    entry_mid_usd=m0_usd,
                    note=note,
                )
            )
    return trades, drawn


def reconcile(trades: list[Trade]) -> dict[str, Any]:
    failures = []
    for t in trades:
        if t.net is None:
            failures.append({"symbol": t.symbol, "entry": t.entry_session.isoformat(),
                             "why": t.note or "ayristirilamadi"})
        elif abs(t.net - t.engine_pnl) > RECONCILE_TOLERANCE_USD:
            failures.append({"symbol": t.symbol, "entry": t.entry_session.isoformat(),
                             "why": f"net {t.net:.2f} vs motor {t.engine_pnl}"})
    share = len(failures) / len(trades) if trades else 1.0
    return {
        "trades": len(trades),
        "failed": len(failures),
        "fail_share": round(share, 4),
        "gate_passed": share <= RECONCILE_MAX_FAIL_SHARE,
        "failures": failures,
    }


def block_ci(
    rows: list[tuple[date, str, float]], stat: Any
) -> dict[str, Any]:
    """Session-block bootstrap of ``stat`` over (entry session, source, value) rows."""
    by_session: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for day, source, value in rows:
        by_session[day].append((source, value))
    blocks = sorted(by_session)
    observed = stat([by_session[s] for s in blocks])
    rng = random.Random(BOOTSTRAP_SEED)
    draws = sorted(
        d
        for _ in range(BOOTSTRAP_REPS)
        if (d := stat([by_session[rng.choice(blocks)] for _ in blocks])) is not None
    )
    low, high = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))]
    return {
        "blocks": len(blocks),
        "observed": None if observed is None else round(observed, 2),
        "ci95": [round(low, 2), round(high, 2)],
        "excludes_zero": low > 0 or high < 0,
    }


def mean_of(blocks: list[list[tuple[str, float]]], source: str) -> float | None:
    values = [v for block in blocks for s, v in block if s == source]
    return statistics.fmean(values) if values else None


def describe(trades: list[Trade]) -> dict[str, Any]:
    ok = [t for t in trades if t.parts is not None and t.net is not None]
    if not ok:
        return {"n": 0}
    comp = {c: statistics.fmean(float(t.parts[c]) for t in ok) for c in COMPONENTS}  # type: ignore[index]
    cost = comp["entry_spread"] + comp["entry_latency"] + comp["exit_spread"] + comp["commission"]
    net = statistics.fmean(float(t.net) for t in ok)  # type: ignore[arg-type]
    return {
        "n": len(ok),
        "mean_net_usd": round(net, 2),
        "components_usd": {c: round(v, 2) for c, v in comp.items()},
        "mean_cost_total_usd": round(cost, 2),
        "cost_share_of_net_loss": round(cost / -net, 3) if net < 0 else None,
        "win_rate_net": round(sum(1 for t in ok if t.net > 0) / len(ok), 3),  # type: ignore[operator]
        "win_rate_mid": round(sum(1 for t in ok if t.parts["market"] > 0) / len(ok), 3),  # type: ignore[index]
        "mean_entry_mid_usd": round(statistics.fmean(float(t.entry_mid_usd) for t in ok), 2),  # type: ignore[arg-type]
        "round_trip_cost_pct_of_entry_mid": round(
            100 * cost / statistics.fmean(float(t.entry_mid_usd) for t in ok), 1  # type: ignore[arg-type]
        ),
    }


def breakdowns(trades: list[Trade]) -> dict[str, Any]:
    ok = [t for t in trades if t.parts is not None and t.entry_mid_usd]
    out: dict[str, Any] = {}
    by_structure: dict[str, list[Trade]] = defaultdict(list)
    by_name: dict[str, list[Trade]] = defaultdict(list)
    for t in ok:
        by_structure[t.structure].append(t)
        by_name["QQQ+SPY" if t.ticker in ETF_NAMES else "diger"].append(t)
    out["structure"] = {k: describe(v) for k, v in sorted(by_structure.items())}
    out["name_group"] = {k: describe(v) for k, v in sorted(by_name.items())}

    # Entry half-spread as a share of the entry mid, in four equal-count groups.
    ranked = sorted(ok, key=lambda t: float(t.parts["entry_spread"] / t.entry_mid_usd))  # type: ignore[index,operator]
    quartiles: dict[str, Any] = {}
    size = len(ranked)
    for i in range(4):
        chunk = ranked[i * size // 4 : (i + 1) * size // 4]
        if not chunk:
            continue
        lo = float(chunk[0].parts["entry_spread"] / chunk[0].entry_mid_usd) * 100  # type: ignore[index,operator]
        hi = float(chunk[-1].parts["entry_spread"] / chunk[-1].entry_mid_usd) * 100  # type: ignore[index,operator]
        quartiles[f"Q{i + 1} (giris makasi %{lo:.1f}-%{hi:.1f} of mid)"] = describe(chunk)
    out["entry_spread_quartile"] = quartiles
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/options-alpha-v1/s01_result.json")
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    args = parser.parse_args()

    harvest = pathlib.Path(args.harvest)
    if not harvest.is_dir():
        print(f"hasat yok: {harvest}")
        return 2

    settings = load_settings()
    sessions = sorted(date.fromisoformat(p.name) for p in harvest.iterdir() if p.is_dir())
    cache = ChainCache(harvest)

    signal, signal_total = load_signal_trades(sessions, cache, settings)
    rand, drawn = random_trades(sessions, cache, settings)

    rec_signal = reconcile(signal)
    rec_random = reconcile(rand)
    result: dict[str, Any] = {
        "study": "S01",
        "protocol": ["docs/options-alpha-v1/PREREG_S01.md"],
        "profile_sha256": settings.profile_sha256,
        "quality_tier": settings.quality.tier,
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()],
        "signal_records_total": signal_total,
        "signal_unique_trades": len(signal),
        "random_anchors_drawn": drawn,
        "random_trades_priced": len(rand),
        "reconciliation": {"signal": rec_signal, "random": rec_random},
        "is_family": False,
        "trials_spent": 0,
    }

    if not (rec_signal["gate_passed"] and rec_random["gate_passed"]):
        result["label"] = "DURDU_MUTABAKAT"
        result["note"] = "mutabakat kapisi gecilmedi (on-kayit §4); yorum yazilmaz"
    else:
        ok_signal = [t for t in signal if t.parts is not None]
        ok_random = [t for t in rand if t.parts is not None]
        sig = describe(ok_signal)
        rnd = describe(ok_random)
        market_ci = block_ci(
            [(t.entry_session, "s", float(t.parts["market"])) for t in ok_signal],  # type: ignore[index]
            lambda blocks: mean_of(blocks, "s"),
        )

        def diff(blocks: list[list[tuple[str, float]]]) -> float | None:
            a, b = mean_of(blocks, "signal"), mean_of(blocks, "random")
            return None if a is None or b is None else a - b

        vs_random = block_ci(
            [(t.entry_session, "signal", float(t.net)) for t in ok_signal]  # type: ignore[arg-type]
            + [(t.entry_session, "random", float(t.net)) for t in ok_random],  # type: ignore[arg-type]
            diff,
        )
        random_market_ci = block_ci(
            [(t.entry_session, "r", float(t.parts["market"])) for t in ok_random],  # type: ignore[index]
            lambda blocks: mean_of(blocks, "r"),
        )

        share = sig["cost_share_of_net_loss"]
        low, high = market_ci["ci95"]
        if high < 0:
            label = "PIYASA_SURUKLEMESI"
        elif share is not None and share >= COST_BOUND_SHARE and low <= 0 <= high:
            label = "MALIYET_BAGLI"
        else:
            label = "KARMA"

        result.update(
            {
                "signal": sig,
                "signal_market_block_bootstrap": market_ci,
                "random": rnd,
                "random_market_block_bootstrap": random_market_ci,
                "signal_minus_random_net": vs_random,
                "breakdowns_signal": breakdowns(ok_signal),
                "breakdowns_random": breakdowns(ok_random),
                "label": label,
                "signals_distinguishable_from_random": vs_random["excludes_zero"],
            }
        )

    result["trades"] = [
        {
            "source": t.source,
            "families": list(t.families),
            "ticker": t.ticker,
            "entry_session": t.entry_session.isoformat(),
            "symbol": t.symbol,
            "structure": t.structure,
            "quantity": t.quantity,
            "engine_pnl_usd": str(t.engine_pnl),
            "net_usd": None if t.net is None else str(t.net.quantize(Decimal("0.01"))),
            "parts_usd": None
            if t.parts is None
            else {k: str(v.quantize(Decimal("0.01"))) for k, v in t.parts.items()},
            "entry_mid_usd": None
            if t.entry_mid_usd is None
            else str(t.entry_mid_usd.quantize(Decimal("0.01"))),
            "note": t.note,
        }
        for t in signal + rand
    ]

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {k: v for k, v in result.items() if k != "trades"}
    for side in ("signal", "random"):
        summary["reconciliation"][side] = {
            k: v for k, v in summary["reconciliation"][side].items() if k != "failures"
        } | {"first_failures": result["reconciliation"][side]["failures"][:5]}
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
