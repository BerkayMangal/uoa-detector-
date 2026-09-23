"""Run one real session end to end, from chain snapshot to a PAPER signal card.

    uv run python scripts/options_alpha_replay_card.py [--session 2026-09-22]

This is the milestone the task puts first: at least one REAL data example that
travels the whole path —

    API snapshot -> eligibility -> contract/spread selection -> cost -> risk
                 -> PAPER card -> outcome record

— rather than an infrastructure pile with nothing running through it.

It is a REPLAY, and it says so everywhere it can. The chain it reads is a frozen
snapshot of a completed session, priced from an end-of-day NBBO whose validity
instant is unknown (quality tier B). A card built from it is `WATCH`, never
`PAPER_ENTRY_READY`: presenting a closed session's prices as an entry would be
the "old card shown as buy now" the task forbids. It is proof the pipeline works,
not a trade.

The trigger is deliberately the plainest thing that can be stated in advance:
the eligible contract with the highest volume whose volume exceeds its open
interest — UW's own notion of new positioning rather than existing inventory. It
carries NO validated edge and the card says so in its own counter-argument. The
point of this milestone is the plumbing, and a trigger invented to look clever
here would be the beginning of a fitted result.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import date
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from uoa_detector.options_alpha.card import (
    DataOrigin,
    ResearchStatus,
    build_card,
)
from uoa_detector.options_alpha.selection import (
    build_candidate,
    eligible_contracts,
    load_chain_snapshot,
)
from uoa_detector.options_alpha.settings import load_settings

HYPOTHESIS = "H00_pipeline_smoke"


def code_version() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default="2026-09-22")
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    session = date.fromisoformat(args.session)
    ticker = args.ticker.upper()
    replay_dir = pathlib.Path(f"artifacts/options-alpha-v1/replay/{session.isoformat()}")

    # The first snapshot was written before the capture was per-ticker, so SPY
    # still lives under the unsuffixed name.
    chain_path = replay_dir / f"chain_trimmed_{ticker}.json"
    if not chain_path.exists():
        chain_path = replay_dir / "chain_trimmed.json"
    if not chain_path.exists():
        print(f"anlik goruntu yok: {replay_dir}/chain_trimmed_{ticker}.json")
        return 2

    settings = load_settings()
    quotes, snapshot_session, underlying = load_chain_snapshot(chain_path)

    # Context only: no selection, cost or risk rule reads the underlying close,
    # so a missing bar must not stop the run or be filled with a guess.
    underlying_path = replay_dir / "underlying.json"
    underlying_bar: dict[str, Any] = {}
    if underlying_path.exists():
        candidate_bar = json.loads(underlying_path.read_text(encoding="utf-8"))
        if candidate_bar.get("ticker") == underlying:
            underlying_bar = candidate_bar
    close = underlying_bar.get("close", "bilinmiyor")

    print(f"seans {snapshot_session}  dayanak {underlying}  kapanis {close}")
    print(f"profil sha256 {settings.profile_sha256[:16]}...  kalite {settings.quality.tier}")

    eligible, funnel = eligible_contracts(quotes, snapshot_session, settings)
    print(f"\ntarandi {funnel.scanned} -> uygun {funnel.eligible}")
    for reason, count in funnel.dropped.most_common(8):
        print(f"   elendi {count:>5}  {reason}")

    if not eligible:
        print("\nuygun kontrat yok — kart uretilmedi")
        return 0

    # Stated in advance, not chosen after seeing outcomes: most traded contract
    # whose volume exceeds its open interest.
    fresh = [q for q in eligible if (q.volume or 0) > (q.open_interest or 0)]
    used_fresh = bool(fresh)
    anchor = max(fresh or eligible, key=lambda q: q.volume or 0)

    # The trigger text must describe the branch that actually ran. Claiming
    # "volume > open interest" on a fallback pick would put a condition on the
    # card that the contract never met — QQQ on 2026-09-08 did exactly that
    # (volume 3,008 against open interest 40,509) before this was fixed.
    if used_fresh:
        trigger_text = (
            f"{anchor.option_symbol}: hacim {anchor.volume} > acik pozisyon "
            f"{anchor.open_interest} (yeni pozisyonlanma), seansin en cok islem goren uygun kontrati"
        )
        branch = "hacim>OI olan en cok islem goren kontrat"
    else:
        trigger_text = (
            f"{anchor.option_symbol}: seansin en cok islem goren uygun kontrati "
            f"(hacim {anchor.volume}, acik pozisyon {anchor.open_interest}). "
            "YEDEK SECIM: hicbir uygun kontratta hacim acik pozisyonu asmadi, "
            "yani yeni pozisyonlanma kosulu SAGLANMADI"
        )
        branch = "YEDEK: hacim>OI saglayan uygun kontrat yok, en cok islem gorene dusuldu"

    print(
        f"\ntetik: {branch}"
        f"  -> {anchor.option_symbol}"
        f"  hacim {anchor.volume}  OI {anchor.open_interest}  delta {anchor.delta}"
    )

    # Full chain, not the eligible subset: the short leg is a hedge and is picked
    # from every listed strike of that expiry.
    candidate = build_candidate(anchor, quotes, settings, funnel)
    print(
        f"\nhuni: yapi {funnel.structures_built} | fiyatlandi {funnel.priced} | "
        f"maliyet kapisi {funnel.passed_cost_gate} | risk kapisi {funnel.passed_risk_gate}"
    )
    if candidate is None:
        print("\naday kartlanamadi — eleme zinciri yukarida")
        return 0

    card = build_card(
        candidate,
        hypothesis_id=HYPOTHESIS,
        research_status=ResearchStatus.RESEARCH_ONLY,
        settings=settings,
        session=snapshot_session,
        underlying=underlying,
        trigger=trigger_text,
        counter_argument=(
            "Bu tetigin dogrulanmis bir edge'i YOK. Hat kanitidir, isaret degil. "
            "Hacmin OI'yi asmasi ayni gun acilip kapanan islemle de olusur ve yon soylemez; "
            "kotasyon gun sonu anlik goruntusu oldugu icin gercek bir giris fiyati vaat etmez."
        ),
        missing_data=tuple(
            filter(
                None,
                [
                    "delta yok" if anchor.delta is None else None,
                    "IV yok" if anchor.implied_volatility is None else None,
                    "kotasyon zaman damgasi yok (last_tape_time islem zamanidir)",
                ],
            )
        ),
        data_origin=DataOrigin.REPLAY,
        code_version=code_version(),
        data_manifest={
            "chain_snapshot": str(chain_path),
            "underlying": underlying_bar,
            "session": snapshot_session.isoformat(),
            "quality_tier": settings.quality.tier,
        },
        sources=(
            "UW /api/stock/{t}/option-chains?date=&greeks=true",
            "UW /api/screener/option-contracts?date=",
            "UW /api/stock/{t}/ohlc/1d (market_time=='r')",
        ),
    )

    out = pathlib.Path(args.out or (replay_dir / f"paper_card_{underlying}.json"))
    payload = {"card": card.as_dict(), "funnel": funnel.as_dict()}
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n--- KART ---")
    print(f"  {card.signal_id}  [{card.research_status}] [{card.opportunity_status}]")
    print(f"  {card.underlying} {card.direction}  {card.structure}")
    for leg in card.legs:
        side = "uzun" if leg.is_long else "kisa"
        print(f"    {side:5s} {leg.occ_symbol}  strike {leg.strike}  bid {leg.bid} ask {leg.ask}")
    print(f"  net debit {card.net_debit_per_share}/hisse  limit {card.limit_price}")
    print(f"  adet {card.quantity}  maliyet {card.entry_cost_usd} $  komisyon {card.commission_usd} $")
    print(f"  yapisal azami zarar {card.structural_max_loss_usd} $  azami kar {card.max_profit_usd}")
    print(f"  basabas {card.breakeven_underlying}  stop {card.planned_stop_usd} $")
    print(f"  azami tutma {card.max_hold_trading_days} islem gunu, vadeye {card.force_close_at_dte} gun kala kapat")
    print(f"  AMA: {card.counter_argument}")
    print(f"\nyazildi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
