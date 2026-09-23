"""H10 — trade-level audit of ten structures, recomputed independently.

    uv run python scripts/options_alpha_h10_trade_audit.py [--out PATH]

Reads the committed ``artifacts/options-alpha-v1/h10_result.json`` and the local
harvest. Spends no quota. Changes no threshold and no verdict.

Selection is fixed BEFORE any P&L is looked at and does not depend on outcomes:
within each arm, records are ordered by ``sha256(symbol|entry_session)`` and the
first five are taken. Anyone can reproduce the same ten.

What is independent and what is not, stated plainly:

* The LEG IDENTITY (which strikes make the spread) comes from the engine's own
  ``build_candidate``, because the result artifact records only the anchor. The
  audit reconstructs it; it does not re-decide it.
* Everything after that is recomputed from the raw harvest JSON with plain
  ``json`` + ``Decimal``: no ``parse_chain_row``, no ``price_structure``, no
  ``evaluate_exit``. Entry sides, slippage, commission, daily closable value,
  time exit, net P&L.

A day on which a leg has no row (the harvest keeps only rows with a bid) is
UNKNOWN, never zero. The audit keeps unknown outcomes and realised losses apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from datetime import date
from decimal import Decimal
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.selection import Funnel, build_candidate, parse_chain_row
from uoa_detector.options_alpha.settings import load_settings

RESULT = pathlib.Path("artifacts/options-alpha-v1/h10_result.json")
HARVEST = pathlib.Path("artifacts/options-alpha-v1/harvest")
PER_ARM = 5
HOLD = 5
C = Decimal("0.01")


def pick_key(record: dict[str, Any]) -> str:
    return hashlib.sha256(f"{record['symbol']}|{record['entry_session']}".encode()).hexdigest()


def raw_rows(ticker: str, session: str) -> tuple[pathlib.Path, dict[str, dict[str, Any]]]:
    path = HARVEST / session / f"{ticker}.json"
    if not path.exists():
        return path, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return path, {row["option_symbol"]: row for row in payload["rows"]}


def dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def audit_one(record: dict[str, Any], sessions: list[str], settings: Any) -> dict[str, Any]:
    ticker, entry = record["ticker"], record["entry_session"]
    entry_path, entry_raw = raw_rows(ticker, entry)

    # Leg identity from the engine (see module docstring).
    entry_payload = json.loads(entry_path.read_text(encoding="utf-8"))
    chain = [
        q for q in (parse_chain_row(r, date.fromisoformat(entry), ticker) for r in entry_payload["rows"])
        if q is not None
    ]
    anchor = next(q for q in chain if q.option_symbol == record["symbol"])
    candidate = build_candidate(anchor, chain, settings, Funnel())
    if candidate is None:
        return {"record": record, "error": "motor bu kayit icin aday uretmedi"}
    legs = [(leg.quote.option_symbol, leg.is_long) for leg in candidate.legs]
    qty = int(record["quantity"])
    mult = 100

    # --- Independent entry pricing from raw rows.
    costs = settings.costs
    debit = Decimal(0)
    entry_legs = []
    for sym, is_long in legs:
        row = entry_raw[sym]
        bid, ask = dec(row["nbbo_bid"]), dec(row["nbbo_ask"])
        side_name = costs.entry_long_side if is_long else costs.entry_short_side
        px = {"ask": ask, "bid": bid}[side_name]
        assert px is not None
        debit += px if is_long else -px
        entry_legs.append({"leg": sym, "side": "long" if is_long else "short", "bid": str(bid),
                           "ask": str(ask), "used": f"{side_name} {px}", "strike": row["strike"],
                           "expires": row["expires"]})
    raw_debit = debit
    debit = (debit * (Decimal(1) + Decimal(str(costs.extra_slippage_pct)) / Decimal(100))).quantize(C)
    commission = (Decimal(str(costs.commission_per_contract_per_leg_usd)) * len(legs) * 2 * qty).quantize(C)
    max_loss = (debit * mult * qty + commission).quantize(C)

    # --- Hold days: the five sessions after entry.
    i = sessions.index(entry)
    hold = sessions[i + 1 : i + 1 + HOLD]
    days = []
    for day in hold:
        path, rows = raw_rows(ticker, day)
        value = Decimal(0)
        missing = []
        quotes = {}
        for sym, is_long in legs:
            row = rows.get(sym)
            if row is None:
                missing.append(sym)
                continue
            px = dec(row["nbbo_bid"]) if is_long else dec(row["nbbo_ask"])
            quotes[sym] = {"bid": row["nbbo_bid"], "ask": row["nbbo_ask"]}
            if px is None or px <= 0:
                missing.append(sym)
                continue
            value += px if is_long else -px
        days.append({"day": day, "source": str(path), "quotes": quotes,
                     "closable_value": None if missing else str(value.quantize(C)),
                     "missing_legs": missing})

    # --- time_only exit: fifth session's closable value. If that day is unknown,
    # the outcome is UNKNOWN; this audit does not borrow an earlier day.
    last = days[-1] if len(days) == HOLD else None
    if last is None or last["closable_value"] is None:
        strict = {"status": "UNKNOWN", "pnl_usd": None,
                  "why": "5. seansta en az bir bacagin kotasyonu yok"}
    else:
        v = Decimal(last["closable_value"])
        pnl = ((v - debit) * mult * qty - commission).quantize(C)
        strict = {"status": "REALISED", "exit_day": last["day"], "exit_value": str(v), "pnl_usd": str(pnl)}

    engine = record["exits"]["time_only"]
    agree = strict["pnl_usd"] is not None and engine["pnl_usd"] is not None and \
        Decimal(strict["pnl_usd"]) == Decimal(engine["pnl_usd"])

    flags = []
    if strict["pnl_usd"] is not None and Decimal(strict["pnl_usd"]) < -max_loss:
        flags.append("ZARAR_AZAMI_RISKI_ASIYOR: kapanis degeri negatif — spread, odenen borctan fazlasina kapatildi")
    if any(d["closable_value"] is not None and Decimal(d["closable_value"]) < 0 for d in days):
        flags.append("NEGATIF_PAKET_DEGERI: en az bir gun uzun bid < kisa ask")
    if strict["status"] == "UNKNOWN" and engine["pnl_usd"] is not None:
        flags.append("MOTOR_BILINMEYENI_GERCEKLESMIS_SAYDI: 5. gun fiyatsiz, motor onceki gunu 'time' diye yazdi")
    if not agree and strict["pnl_usd"] is not None:
        flags.append("P&L_UYUSMAZLIGI")

    return {
        "arm": record["arm"], "ticker": ticker, "anchor": record["symbol"], "structure": record["structure"],
        "info_time": {"flow_session_D": record["flow_session"],
                      "known": f"D hacmi/zinciri D+1'de yayimlanmis; giris D+1 kapanisi = {entry}"},
        "entry_session": entry, "entry_source": str(entry_path), "legs": entry_legs,
        "quantity": qty, "multiplier": mult,
        "raw_debit_per_share": str(raw_debit), "debit_after_slippage": str(debit),
        "engine_entry_debit": record["entry_debit"],
        "entry_debit_agrees": Decimal(record["entry_debit"]) == debit,
        "commission_round_trip_usd": str(commission), "max_loss_usd": str(max_loss),
        "hold": days, "independent_time_only": strict, "engine_time_only": engine,
        "pnl_agrees": agree, "flags": flags,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=pathlib.Path, default=RESULT)
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path("artifacts/options-alpha-v1/h10_trade_audit.json"))
    args = parser.parse_args()
    settings = load_settings()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    sessions = sorted(p.name for p in HARVEST.iterdir() if p.is_dir())

    picked = []
    for arm in ("A_cheap", "B_rich"):
        rows = sorted((r for r in result["records"] if r["arm"] == arm), key=pick_key)
        picked += rows[:PER_ARM]
    audits = [audit_one(r, sessions, settings) for r in picked]

    # Whole-sample sweep with the same independent arithmetic: how many records
    # would change if unknown stayed unknown and the package value is taken as is.
    sweep = [audit_one(r, sessions, settings) for r in result["records"]]
    def arm_stats(floor_at_zero: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for arm in ("A_cheap", "B_rich"):
            vals = []
            unknown = 0
            for a in sweep:
                if a["arm"] != arm:
                    continue
                t = a["independent_time_only"]
                if t["pnl_usd"] is None:
                    unknown += 1
                    continue
                pnl = Decimal(t["pnl_usd"])
                if floor_at_zero:
                    pnl = max(pnl, -Decimal(a["max_loss_usd"]))
                vals.append(pnl)
            out[arm] = {"realised": len(vals), "unknown": unknown,
                        "mean_pnl_usd": str((sum(vals) / len(vals)).quantize(C)) if vals else None}
        return out

    summary = {
        "result_file": str(args.result),
        "independent_arms_primary": arm_stats(False),
        "sensitivity_loss_capped_at_max_risk": arm_stats(True),
        "selection": "her kolda sha256(symbol|entry_session) sirasiyla ilk 5 — sonuca bakmaz",
        "audited": len(audits),
        "entry_debit_agrees": sum(a.get("entry_debit_agrees", False) for a in audits),
        "pnl_agrees": sum(a.get("pnl_agrees", False) for a in audits),
        "flags_in_sample": sorted({f.split(":")[0] for a in audits for f in a.get("flags", [])}),
        "full_sweep": {
            "records": len(sweep),
            "pnl_agrees": sum(a.get("pnl_agrees", False) for a in sweep),
            "unknown_booked_as_realised": sum(
                any(f.startswith("MOTOR_BILINMEYENI") for f in a.get("flags", [])) for a in sweep),
            "loss_beyond_max_risk": sum(
                any(f.startswith("ZARAR_AZAMI") for f in a.get("flags", [])) for a in sweep),
            "negative_package_value_seen": sum(
                any(f.startswith("NEGATIF") for f in a.get("flags", [])) for a in sweep),
            "affected": [
                {"arm": a["arm"], "anchor": a["anchor"], "entry": a["entry_session"],
                 "engine": a["engine_time_only"]["pnl_usd"],
                 "independent": a["independent_time_only"]["pnl_usd"],
                 "flags": [f.split(":")[0] for f in a["flags"]]}
                for a in sweep if a.get("flags")
            ],
        },
    }
    args.out.write_text(json.dumps({"summary": summary, "trades": audits}, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
