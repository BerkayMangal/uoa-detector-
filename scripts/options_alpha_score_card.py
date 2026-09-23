"""Score a PAPER card against what the option actually did afterwards.

    uv run python scripts/options_alpha_score_card.py CARD.json

This is the other half of the loop. The card froze a decision; this walks the
sessions that followed and applies the frozen exit variants to the structure's
own daily closable value, using the per-contract NBBO history the capability
probe verified.

One honesty decision shapes the whole thing. ``/historic`` gives each LEG a daily
high and low, and it is tempting to build a package range from them — long leg's
high minus short leg's low would be the best the spread could have been worth.
That combination never existed: the two extremes did not have to occur at the
same moment, and pricing a package from the best of two different instants is the
"farkli zamanlarin en iyi iki fiyatini birlestirme" trap. So only the CLOSE is
used, and the exit engine records that intraday touches are therefore invisible.
The result is conservative and honest rather than flattering and unprovable.

Read-only against the vendor; writes one outcome artifact. Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.exits import (
    DailyObservation,
    ExitVariantId,
    evaluate_exit,
)
from uoa_detector.options_alpha.settings import load_settings

BASE = "https://api.unusualwhales.com"


def rows_of(payload: Any) -> list[dict[str, Any]]:
    """The envelope rule production uses: ``chains`` first, then ``data``."""
    if isinstance(payload, dict):
        for key in ("chains", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


async def leg_history(client: httpx.AsyncClient, occ: str) -> dict[date, dict[str, Decimal | None]]:
    response = await client.get(f"{BASE}/api/option-contract/{occ}/historic")
    response.raise_for_status()
    out: dict[date, dict[str, Decimal | None]] = {}
    for row in rows_of(response.json()):
        raw_day = row.get("date")
        if not isinstance(raw_day, str):
            continue
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        out[day] = {"bid": dec(row.get("nbbo_bid")), "ask": dec(row.get("nbbo_ask"))}
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("card")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    key = os.environ.get("UNUSUAL_WHALES_API_KEY", "")
    if not key:
        print("UNUSUAL_WHALES_API_KEY tanimli degil.")
        return 2

    card_path = pathlib.Path(args.card)
    card = json.loads(card_path.read_text(encoding="utf-8"))["card"]
    settings = load_settings()

    entry_day = date.fromisoformat(card["session"])
    expiry = date.fromisoformat(card["legs"][0]["expiry"])
    entry_debit = Decimal(card["net_debit_per_share"])
    commission = Decimal(card["commission_usd"])
    structures = int(card["quantity"])

    print(f"kart {card['signal_id']}  {card['underlying']} {card['structure']}")
    print(f"giris {entry_day}  debit {entry_debit}/hisse  adet {structures}  vade {expiry}")

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
        histories = {}
        for leg in card["legs"]:
            histories[leg["occ_symbol"]] = await leg_history(client, leg["occ_symbol"])
            print(f"  {leg['occ_symbol']:22s} {len(histories[leg['occ_symbol']]):>3} gun")

    long_leg = next(x for x in card["legs"] if x["side"] == "long")
    short_leg = next((x for x in card["legs"] if x["side"] == "short"), None)

    # Only sessions strictly AFTER the entry: a position cannot exit on the value
    # that priced its own entry.
    days = sorted(d for d in histories[long_leg["occ_symbol"]] if d > entry_day)

    observations: list[DailyObservation] = []
    for day in days:
        long_row = histories[long_leg["occ_symbol"]].get(day, {})
        long_bid = long_row.get("bid")
        value: Decimal | None = None
        complete = long_bid is not None
        if long_bid is not None:
            value = long_bid
            if short_leg is not None:
                short_ask = histories[short_leg["occ_symbol"]].get(day, {}).get("ask")
                if short_ask is None:
                    complete = False
                    value = None
                else:
                    value = long_bid - short_ask
        observations.append(
            DailyObservation(
                day=day,
                exit_value=None if value is None else value.quantize(Decimal("0.01")),
                dte=(expiry - day).days,
                is_complete=complete,
            )
        )

    print(f"\ngirisken sonraki seans: {len(observations)}")
    for observation in observations[: settings.exit.primary_hold_trading_days]:
        print(f"  {observation.day}  deger {observation.exit_value}  DTE {observation.dte}")

    max_profit = None
    if short_leg is not None:
        width = abs(Decimal(short_leg["strike"]) - Decimal(long_leg["strike"]))
        max_profit = (width - entry_debit).quantize(Decimal("0.01"))

    results: dict[str, Any] = {}
    print("\n--- CIKIS VARYANTLARI ---")
    for variant in ExitVariantId:
        outcome = evaluate_exit(
            variant=variant,
            entry_debit=entry_debit,
            observations=tuple(observations),
            settings=settings,
            structures=structures,
            max_profit_per_share=max_profit,
            commission_usd=commission,
        )
        results[variant.value] = {
            "reason": outcome.reason.value,
            "exit_day": None if outcome.exit_day is None else outcome.exit_day.isoformat(),
            "exit_value": None if outcome.exit_value is None else str(outcome.exit_value),
            "pnl_usd": None if outcome.pnl_usd is None else str(outcome.pnl_usd),
            "return_on_risk": outcome.return_on_risk,
            "held_trading_days": outcome.held_trading_days,
            "mfe_per_share": None if outcome.mfe_per_share is None else str(outcome.mfe_per_share),
            "mae_per_share": None if outcome.mae_per_share is None else str(outcome.mae_per_share),
            "ambiguous_same_day_touch": outcome.ambiguous_same_day_touch,
            "unpriced_days": outcome.unpriced_days,
            "notes": list(outcome.notes),
        }
        ror = "-" if outcome.return_on_risk is None else f"{outcome.return_on_risk * 100:+.1f}%"
        print(
            f"  {variant.value:17s} {outcome.reason.value:12s} "
            f"gun {outcome.exit_day}  deger {outcome.exit_value}  "
            f"P&L {outcome.pnl_usd} $ ({ror})  tutma {outcome.held_trading_days}"
        )

    payload = {
        "card": {k: card[k] for k in ("signal_id", "opportunity_id", "hypothesis_id",
                                      "underlying", "structure", "session",
                                      "net_debit_per_share", "quantity",
                                      "structural_max_loss_usd", "max_profit_usd",
                                      "profile_sha256", "quality_tier")},
        "scored_at_sessions": [o.day.isoformat() for o in observations],
        "price_basis": "per-leg daily NBBO close only; per-leg highs/lows deliberately NOT "
                       "combined into a package range (different instants)",
        "exits": results,
    }
    out = pathlib.Path(args.out or card_path.with_name(card_path.stem + "_outcome.json"))
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nyazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
