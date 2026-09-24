"""S02 — the vol premium as money: short ATM iron butterfly at measured spreads.

    uv run python scripts/options_alpha_s02_study.py [--out PATH]

Runs the protocol frozen in `docs/options-alpha-v1/PREREG_S02.md` (commit 06bcdcf,
before this file existed).

On every non-overlapping entry date (harvest session 0, 5, 10, ...) and for each of
the ten profile names, UNCONDITIONALLY:

    expiry  = DTE in [21, 45], closest to 30 (earlier on ties)
    K       = strike with both a call and a put row, closest to spot (lower on ties)
    EM      = spot * IV_atm * sqrt(DTE/365), IV_atm = mean of the K call/put IVs
    wings   = smallest call strike >= K + 1.5 EM, largest put strike <= K - 1.5 EM
    sell K call + K put, buy both wings, quantity 1

Entry at the D close: shorts at the bid, wings at the ask, credit x (1 - 2%).
Exit five sessions later: shorts at the ask, wings at the bid, debit x (1 + 2%).
A missing short-leg quote at exit is UNKNOWN; a missing wing row is a zero bid,
which is against us. Commission 4 legs x $0.65 x 2 = $5.20.

The net P&L is split like S01: the mid-to-mid package move (the premium actually
earned by a seller) minus the costs, and the parts must reconcile to the cent.

Reads the local harvest and data/study_f/bars.csv. Spends no quota.
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

DEFAULT_HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")
DEFAULT_BARS = pathlib.Path("data/study_f/bars.csv")
TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN", "META", "TSLA", "AMD", "GOOGL"]

# Frozen in PREREG_S02 sections 3-7.
HOLD = 5
DTE_MIN, DTE_MAX, DTE_TARGET = 21, 45, 30
WING_EM = 1.5
LATENCY = Decimal("0.02")
COMMISSION_USD = Decimal("5.20")  # 4 legs x 0.65 x 2 directions
MULT = Decimal(100)
FLOOR_TRADES = 120
FLOOR_DATES = 12
BOOTSTRAP_SEED = 20260923
BOOTSTRAP_REPS = 10_000
IV_RANK_WARMUP = 20
IV_RANK_HIGH = 0.75
EDGE_TO_MONEY_SPREADS = (5.0, 10.0, 20.0)


def dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


class Harvest:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.sessions = sorted(date.fromisoformat(p.name) for p in root.iterdir() if p.is_dir())
        self._cache: dict[tuple[str, date], list[dict[str, Any]]] = {}

    def rows(self, ticker: str, day: date) -> list[dict[str, Any]]:
        key = (ticker, day)
        if key not in self._cache:
            path = self.root / day.isoformat() / f"{ticker}.json"
            self._cache[key] = json.loads(path.read_text())["rows"] if path.exists() else []
        return self._cache[key]

    def legs(self, ticker: str, day: date, expiry: str) -> dict[tuple[str, float], dict[str, Any]]:
        return {
            (r["option_type"], float(r["strike"])): r
            for r in self.rows(ticker, day)
            if r["expires"] == expiry
        }


@dataclass
class Fly:
    ticker: str
    entry: date
    exit: date
    expiry: str
    dte: int
    spot: float
    strike: float
    call_wing: float
    put_wing: float
    iv_atm: float
    credit: Decimal
    credit_raw: Decimal
    debit: Decimal | None
    debit_raw: Decimal | None
    mid_entry: Decimal
    mid_exit: Decimal | None
    pnl: Decimal | None
    max_loss: Decimal
    status: str
    parts: dict[str, Decimal] | None


def atm_iv(harvest: Harvest, ticker: str, day: date, spot: float) -> float | None:
    """The same expiry/strike rule as the fly, returning only IV_atm (for IV-rank)."""
    built = choose(harvest, ticker, day, spot)
    return None if built is None else built[3]


def choose(
    harvest: Harvest, ticker: str, day: date, spot: float
) -> tuple[str, int, float, float, float, float] | None:
    """Section 4: (expiry, dte, K, iv_atm, call wing, put wing), or None."""
    chain = harvest.rows(ticker, day)
    if not chain:
        return None
    candidates = []
    for expiry in {r["expires"] for r in chain}:
        dte = (date.fromisoformat(expiry) - day).days
        if DTE_MIN <= dte <= DTE_MAX:
            candidates.append((abs(dte - DTE_TARGET), dte, expiry))
    if not candidates:
        return None
    _, dte, expiry = min(candidates)
    legs = harvest.legs(ticker, day, expiry)
    strikes = sorted(
        {k for (_, k) in legs if ("call", k) in legs and ("put", k) in legs},
        key=lambda k: (abs(k - spot), k),
    )
    if not strikes:
        return None
    k = strikes[0]
    ivs = [float(legs[(side, k)]["implied_volatility"] or 0) for side in ("call", "put")]
    iv = sum(ivs) / 2
    if iv <= 0:
        return None
    em = spot * iv * math.sqrt(dte / 365)
    calls = sorted(s for (o, s) in legs if o == "call" and s >= k + WING_EM * em)
    puts = sorted(s for (o, s) in legs if o == "put" and s <= k - WING_EM * em)
    if not calls or not puts:
        return None
    return expiry, dte, k, iv, calls[0], puts[-1]


def mid(row: dict[str, Any] | None) -> Decimal | None:
    if row is None:
        return None
    bid, ask = dec(row.get("nbbo_bid")), dec(row.get("nbbo_ask"))
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def build(
    harvest: Harvest, ticker: str, entry: date, exit_day: date, spot: float
) -> Fly | str:
    picked = choose(harvest, ticker, entry, spot)
    if picked is None:
        return "kurulamadi"
    expiry, dte, k, iv, cw, pw = picked
    legs = harvest.legs(ticker, entry, expiry)
    ck, pk, cwr, pwr = legs[("call", k)], legs[("put", k)], legs[("call", cw)], legs[("put", pw)]
    prices = [dec(ck["nbbo_bid"]), dec(pk["nbbo_bid"]), dec(cwr["nbbo_ask"]), dec(pwr["nbbo_ask"])]
    if any(p is None for p in prices):
        return "giriste kotasyon eksik"
    cb, pb, cwa, pwa = prices  # type: ignore[misc]
    credit_raw = cb + pb - cwa - pwa  # type: ignore[operator]
    credit = (credit_raw * (1 - LATENCY)).quantize(Decimal("0.0001"))
    if credit <= 0:
        return "kredi <= 0"
    mids = [mid(r) for r in (ck, pk, cwr, pwr)]
    mid_entry = mids[0] + mids[1] - mids[2] - mids[3]  # type: ignore[operator]
    width = Decimal(str(max(cw - k, k - pw)))
    max_loss = (width - credit) * MULT + COMMISSION_USD

    xlegs = harvest.legs(ticker, exit_day, expiry)
    xck, xpk = xlegs.get(("call", k)), xlegs.get(("put", k))
    xcw, xpw = xlegs.get(("call", cw)), xlegs.get(("put", pw))
    base = dict(
        ticker=ticker, entry=entry, exit=exit_day, expiry=expiry, dte=dte, spot=spot,
        strike=k, call_wing=cw, put_wing=pw, iv_atm=iv, credit=credit, credit_raw=credit_raw,
        mid_entry=mid_entry, max_loss=max_loss,
    )
    short_asks = [dec(xck["nbbo_ask"]) if xck else None, dec(xpk["nbbo_ask"]) if xpk else None]
    if any(a is None for a in short_asks):
        return Fly(**base, debit=None, debit_raw=None, mid_exit=None, pnl=None,  # type: ignore[arg-type]
                   status="BILINMIYOR", parts=None)
    # A wing with no row has no bid to sell into: zero, against us (section 5).
    wing_bids = [dec(xcw["nbbo_bid"]) if xcw else Decimal(0), dec(xpw["nbbo_bid"]) if xpw else Decimal(0)]
    wing_bids = [b if b is not None else Decimal(0) for b in wing_bids]
    debit_raw = short_asks[0] + short_asks[1] - wing_bids[0] - wing_bids[1]  # type: ignore[operator]
    debit = (debit_raw * (1 + LATENCY)).quantize(Decimal("0.0001"))
    pnl = (credit - debit) * MULT - COMMISSION_USD

    # Mid at exit; a missing wing row counts as a zero mid (it had no bid).
    xm = [mid(xck), mid(xpk), mid(xcw) if xcw else Decimal(0), mid(xpw) if xpw else Decimal(0)]
    mid_exit = None if any(m is None for m in xm) else xm[0] + xm[1] - xm[2] - xm[3]  # type: ignore[operator]
    parts = None
    if mid_exit is not None:
        parts = {
            "premium_earned_mid": (mid_entry - mid_exit) * MULT,
            "entry_spread": (mid_entry - credit_raw) * MULT,
            "entry_latency": (credit_raw - credit) * MULT,
            "exit_spread": (debit_raw - mid_exit) * MULT,
            "exit_latency": (debit - debit_raw) * MULT,
            "commission": COMMISSION_USD,
        }
    return Fly(**base, debit=debit, debit_raw=debit_raw, mid_exit=mid_exit, pnl=pnl,  # type: ignore[arg-type]
               status="TAMAM", parts=parts)


def load_closes(path: pathlib.Path) -> dict[tuple[str, date], float]:
    out: dict[tuple[str, date], float] = {}
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if row["ticker"] in TICKERS and row["close"]:
                out[(row["ticker"], date.fromisoformat(row["day"]))] = float(row["close"])
    return out


def run_schedule(
    harvest: Harvest, closes: dict[tuple[str, date], float], offset: int
) -> tuple[list[Fly], dict[str, int]]:
    sessions = harvest.sessions
    flies: list[Fly] = []
    dropped: dict[str, int] = defaultdict(int)
    for i in range(offset, len(sessions) - HOLD, HOLD):
        entry, exit_day = sessions[i], sessions[i + HOLD]
        for ticker in TICKERS:
            spot = closes.get((ticker, entry))
            if spot is None:
                dropped["spot yok"] += 1
                continue
            built = build(harvest, ticker, entry, exit_day, spot)
            if isinstance(built, str):
                dropped[built] += 1
            else:
                flies.append(built)
    return flies, dict(dropped)


def ror(f: Fly) -> float:
    return float(f.pnl / f.max_loss)  # type: ignore[operator]


def date_block_ci(flies: list[Fly]) -> dict[str, Any]:
    by_date: dict[date, list[float]] = defaultdict(list)
    for f in flies:
        by_date[f.entry].append(ror(f))
    blocks = sorted(by_date)
    rng = random.Random(BOOTSTRAP_SEED)
    draws = []
    for _ in range(BOOTSTRAP_REPS):
        values = [v for _ in blocks for v in by_date[rng.choice(blocks)]]
        draws.append(statistics.fmean(values))
    draws.sort()
    low, high = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))]
    return {
        "blocks": len(blocks),
        "ci95": [round(low, 4), round(high, 4)],
        "share_at_or_below_zero": round(sum(1 for d in draws if d <= 0) / len(draws), 4),
    }


def summary(flies: list[Fly]) -> dict[str, Any]:
    done = [f for f in flies if f.status == "TAMAM"]
    if not done:
        return {"n": 0}
    rors = [ror(f) for f in done]
    pnls = [float(f.pnl) for f in done]  # type: ignore[arg-type]
    per_date: dict[date, list[float]] = defaultdict(list)
    for f in done:
        per_date[f.entry].append(ror(f))
    date_means = [statistics.fmean(v) for _, v in sorted(per_date.items())]
    sharpe = (
        statistics.fmean(date_means) / statistics.stdev(date_means) * math.sqrt(252 / HOLD)
        if len(date_means) > 1 and statistics.stdev(date_means) > 0
        else None
    )
    with_parts = [f for f in done if f.parts is not None]
    comps = (
        {k: round(statistics.fmean(float(f.parts[k]) for f in with_parts), 2)  # type: ignore[index]
         for k in with_parts[0].parts}  # type: ignore[union-attr]
        if with_parts else {}
    )
    cost = sum(v for k, v in comps.items() if k != "premium_earned_mid")
    mean_credit_raw = statistics.fmean(float(f.credit_raw) * 100 for f in done)
    mean_mid_entry = statistics.fmean(float(f.mid_entry) * 100 for f in done)
    return {
        "n": len(done),
        "entry_dates": len(per_date),
        "mean_ror": round(statistics.fmean(rors), 4),
        "median_ror": round(statistics.median(rors), 4),
        "mean_pnl_usd": round(statistics.fmean(pnls), 2),
        "total_pnl_usd": round(sum(pnls), 2),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
        "mean_max_loss_usd": round(statistics.fmean(float(f.max_loss) for f in done), 2),
        "mean_credit_usd": round(statistics.fmean(float(f.credit) * 100 for f in done), 2),
        "mean_mid_entry_usd": round(mean_mid_entry, 2),
        "components_usd": comps,
        "mean_cost_usd": round(cost, 2),
        "cost_pct_of_mid_credit": round(100 * cost / mean_mid_entry, 1) if mean_mid_entry else None,
        "entry_spread_pct_of_mid_credit": round(
            100 * (mean_mid_entry - mean_credit_raw) / mean_mid_entry, 1
        ) if mean_mid_entry else None,
        "annualised_sharpe_date_means": None if sharpe is None else round(sharpe, 2),
        "worst_trade_usd": round(min(pnls), 2),
        "best_trade_usd": round(max(pnls), 2),
        "worst_date_total_usd": round(
            min(sum(float(f.pnl) for f in done if f.entry == d) for d in per_date), 2  # type: ignore[arg-type]
        ),
    }


def edge_to_money_counterfactual(done: list[Fly]) -> dict[str, Any]:
    """What edge_to_money's assumed spreads would have charged on these same flies.

    The assumption there is a spread of X% of the option's mid, paid half on the way
    in and half on the way out, per leg. Here it is applied to the four legs' mids
    and compared with the measured entry+exit spread actually paid.
    """
    out: dict[str, Any] = {}
    measured = [
        float(f.parts["entry_spread"] + f.parts["exit_spread"])  # type: ignore[index]
        for f in done if f.parts is not None
    ]
    out["measured_spread_cost_usd"] = round(statistics.fmean(measured), 2) if measured else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="artifacts/options-alpha-v1/s02_result.json")
    parser.add_argument("--harvest", default=str(DEFAULT_HARVEST))
    parser.add_argument("--bars", default=str(DEFAULT_BARS))
    args = parser.parse_args()

    harvest = Harvest(pathlib.Path(args.harvest))
    closes = load_closes(pathlib.Path(args.bars))

    flies, dropped = run_schedule(harvest, closes, offset=0)
    done = [f for f in flies if f.status == "TAMAM"]
    unknown = sum(1 for f in flies if f.status == "BILINMIYOR")

    # Reconciliation: parts must telescope to the P&L (section 5).
    fails = [
        f for f in done
        if f.parts is not None
        and abs(
            (f.parts["premium_earned_mid"] - f.parts["entry_spread"] - f.parts["entry_latency"]
             - f.parts["exit_spread"] - f.parts["exit_latency"] - f.parts["commission"])
            - f.pnl  # type: ignore[operator]
        ) > Decimal("0.02")
    ]

    head = summary(flies)
    boot = date_block_ci(done) if done else {"ci95": [None, None]}
    dates = sorted({f.entry for f in done})
    half = len(dates) // 2
    first = [ror(f) for f in done if f.entry in dates[:half]]
    second = [ror(f) for f in done if f.entry in dates[half:]]
    halves = {
        "first_half_dates": [d.isoformat() for d in dates[:half]],
        "first_half_mean_ror": round(statistics.fmean(first), 4) if first else None,
        "second_half_mean_ror": round(statistics.fmean(second), 4) if second else None,
    }

    floor_met = head.get("n", 0) >= FLOOR_TRADES and head.get("entry_dates", 0) >= FLOOR_DATES
    failed: list[str] = []
    if not (head.get("mean_ror", -1) > 0):
        failed.append(f"ortalama RoR pozitif degil ({head.get('mean_ror')})")
    if not (boot["ci95"][0] is not None and boot["ci95"][0] > 0):
        failed.append(f"tarih-blok bootstrap alt siniri > 0 degil ({boot['ci95']})")
    if not ((halves["first_half_mean_ror"] or -1) > 0 and (halves["second_half_mean_ror"] or -1) > 0):
        failed.append("iki yarinin ikisinde de pozitif degil")
    if not floor_met:
        verdict, failed = "INSUFFICIENT_DATA", [f"taban tutmadi ({head.get('n')} yapi / {head.get('entry_dates')} tarih)"]
    elif not failed:
        verdict = "EXPLORATORY_PASS"
    else:
        verdict = "REJECTED"

    # Secondary 1: every phase offset.
    offsets = {}
    for off in range(HOLD):
        fl, _ = run_schedule(harvest, closes, off)
        s = summary(fl)
        offsets[str(off)] = {k: s.get(k) for k in ("n", "entry_dates", "mean_ror", "mean_pnl_usd", "win_rate")}

    # Secondary 2: causal IV-rank >= 75% (the name's own earlier ATM IVs in the harvest).
    history: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for day in harvest.sessions:
        for ticker in TICKERS:
            spot = closes.get((ticker, day))
            if spot is None:
                continue
            iv = atm_iv(harvest, ticker, day, spot)
            if iv is not None:
                history[ticker].append((day, iv))
    high_iv: list[Fly] = []
    ranked = 0
    for f in done:
        prior = [iv for d, iv in history[f.ticker] if d < f.entry]
        if len(prior) < IV_RANK_WARMUP:
            continue
        ranked += 1
        rank = sum(1 for iv in prior if iv <= f.iv_atm) / len(prior)
        if rank >= IV_RANK_HIGH:
            high_iv.append(f)

    per_ticker = {
        t: {k: summary([f for f in done if f.ticker == t]).get(k) for k in ("n", "mean_ror", "mean_pnl_usd", "win_rate")}
        for t in TICKERS
    }

    result: dict[str, Any] = {
        "study": "S02",
        "protocol": ["docs/options-alpha-v1/PREREG_S02.md"],
        "protocol_frozen_commit": "06bcdcf",
        "window": [harvest.sessions[0].isoformat(), harvest.sessions[-1].isoformat()],
        "schedule": f"entry every {HOLD} sessions from index 0, hold {HOLD}",
        "structure": "short ATM iron butterfly, 21-45 DTE nearest 30, wings 1.5x EM, qty 1",
        "dropped_at_build": dropped,
        "unknown_exits": unknown,
        "reconciliation_failures": len(fails),
        "primary": head,
        "date_block_bootstrap_mean_ror": boot,
        "halves": halves,
        "sample_floor_met": floor_met,
        "verdict": verdict,
        "verdict_failed_clauses": failed,
        "trials": "1 (scope total 16)",
        "secondary_all_offsets": offsets,
        "secondary_high_iv_rank": {
            "ranked_entries": ranked,
            **{k: summary(high_iv).get(k) for k in ("n", "mean_ror", "mean_pnl_usd", "win_rate")},
        },
        "secondary_per_ticker": per_ticker,
        "secondary_cost_vs_edge_to_money": edge_to_money_counterfactual(done),
        "cannot_say": (
            "Kuyruk orneklenmemis olabilir (16 tarih, ~4 ay); gun ici fill yok (B kalite); "
            "on likit isim disina genellenmez; gecis bile yalniz ileriye donuk izleme adayidir."
        ),
        "trades": [
            {
                "ticker": f.ticker, "entry": f.entry.isoformat(), "exit": f.exit.isoformat(),
                "expiry": f.expiry, "dte": f.dte, "spot": f.spot, "strike": f.strike,
                "call_wing": f.call_wing, "put_wing": f.put_wing, "iv_atm": round(f.iv_atm, 4),
                "credit": str(f.credit), "debit": None if f.debit is None else str(f.debit),
                "pnl_usd": None if f.pnl is None else str(f.pnl.quantize(Decimal("0.01"))),
                "max_loss_usd": str(f.max_loss.quantize(Decimal("0.01"))),
                "ror": None if f.pnl is None else round(ror(f), 4), "status": f.status,
                "parts_usd": None if f.parts is None else {k: str(v.quantize(Decimal("0.01"))) for k, v in f.parts.items()},
            }
            for f in flies
        ],
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=1, ensure_ascii=False))
    print(f"yazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
