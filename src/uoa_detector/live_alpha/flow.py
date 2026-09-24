"""Per-ticker flow summary (contract §5 "Flow summary").

Direction is side-aware, the same rule as ``webapp.board.direction.direction_for``
(decision P11): bought calls and sold puts point up, bought puts and sold calls
point down. A print with no known aggressor side is counted as side-unknown and
never assigned a direction here — the board's option-type fallback is a display
aid, not evidence. ``tests/unit/test_live_alpha_flow.py`` pins that the two rules
agree on every side the board knows.

Repeated prints of the same contract inside ``dedupe_seconds`` are one economic
event (one sweep printed across venues), so they are merged before anything is
counted.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from uoa_detector.live_alpha.model import Direction, FlowPrint, FlowSummary

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from uoa_detector.live_alpha.settings import FlowSettings

BOUGHT_SIDES = frozenset({"at_ask", "above_ask"})
SOLD_SIDES = frozenset({"at_bid", "below_bid"})


def side_direction(option_type: str, fill_side: str | None) -> Direction | None:
    """The print's direction, or None when the aggressor side is unknown."""
    by_type: Direction = "up" if option_type == "call" else "down"
    if fill_side in BOUGHT_SIDES:
        return by_type
    if fill_side in SOLD_SIDES:
        return "down" if by_type == "up" else "up"
    return None


def contract_key(p: FlowPrint) -> str:
    if p.option_chain:
        return p.option_chain
    return f"{p.ticker}|{p.expiry.isoformat()}|{p.option_type}|{p.strike}"


def dedupe(prints: Sequence[FlowPrint], window_seconds: int) -> list[FlowPrint]:
    """Merge same-contract, same-side prints within ``window_seconds`` into one.

    The merged print keeps the first timestamp and the summed premium.
    """
    ordered = sorted(prints, key=lambda p: (contract_key(p), p.fill_side or "", p.ts))
    out: list[FlowPrint] = []
    for p in ordered:
        if out:
            last = out[-1]
            same = contract_key(last) == contract_key(p) and (last.fill_side or "") == (p.fill_side or "")
            if same and (p.ts - last.ts).total_seconds() <= window_seconds:
                out[-1] = FlowPrint(
                    event_id=last.event_id, ticker=last.ticker, ts=last.ts,
                    option_type=last.option_type, strike=last.strike, expiry=last.expiry,
                    premium=last.premium + p.premium, fill_side=last.fill_side,
                    option_chain=last.option_chain,
                )
                continue
        out.append(p)
    return sorted(out, key=lambda p: p.ts)


def summarise(
    ticker: str, run_id: str, prints: Sequence[FlowPrint], settings: FlowSettings,
) -> FlowSummary:
    raw = [p for p in prints if p.ticker.upper() == ticker.upper()]
    merged = dedupe(raw, settings.dedupe_seconds)
    up = down = unknown = 0.0
    by_contract: dict[str, float] = defaultdict(float)
    contract_dir: dict[str, str] = {}
    largest = 0.0
    largest_contract: str | None = None
    for p in merged:
        prem = float(p.premium)
        d = side_direction(p.option_type, p.fill_side)
        if d == "up":
            up += prem
        elif d == "down":
            down += prem
        else:
            unknown += prem
        key = contract_key(p)
        by_contract[key] += prem
        contract_dir[key] = d or "?"
        if prem > largest:
            largest, largest_contract = prem, key
    top = sorted(by_contract.items(), key=lambda kv: kv[1], reverse=True)[:3]
    return FlowSummary(
        ticker=ticker.upper(),
        run_id=run_id,
        prints_raw=len(raw),
        prints_deduped=len(merged),
        premium_up=up,
        premium_down=down,
        premium_side_unknown=unknown,
        distinct_contracts=len(by_contract),
        first_ts=merged[0].ts if merged else None,
        last_ts=merged[-1].ts if merged else None,
        largest_premium=largest,
        largest_contract=largest_contract,
        top_contracts=tuple((k, v, contract_dir[k]) for k, v in top),
        strikes_seen=tuple(sorted({float(p.strike) for p in raw})),
    )


def qualifying_direction(summary: FlowSummary, settings: FlowSettings) -> tuple[Direction | None, str]:
    """The direction the flow qualifies in, or None with the first failed condition."""
    if summary.prints_deduped == 0:
        return None, "bu seansta akış kaydı yok"
    if summary.side_aware_share < settings.side_aware_share_min:
        return None, (
            f"alım/satım tarafı bilinen prim payı %{summary.side_aware_share * 100:.0f} "
            f"(< %{settings.side_aware_share_min * 100:.0f})"
        )
    for direction in ("up", "down"):
        d: Direction = "up" if direction == "up" else "down"
        prem = summary.premium_up if d == "up" else summary.premium_down
        if (
            prem >= settings.min_directional_premium_usd
            and summary.share(d) >= settings.dominance_min
            and summary.distinct_contracts >= settings.min_distinct_contracts
        ):
            return d, ""
    dominant: Direction = "up" if summary.premium_up >= summary.premium_down else "down"
    prem = summary.premium_up if dominant == "up" else summary.premium_down
    if prem < settings.min_directional_premium_usd:
        return None, f"yönlü prim ${prem:,.0f} (< ${settings.min_directional_premium_usd:,.0f})"
    if summary.share(dominant) < settings.dominance_min:
        return None, (
            f"yön baskınlığı %{summary.share(dominant) * 100:.0f} "
            f"(< %{settings.dominance_min * 100:.0f}); akış karışık"
        )
    return None, f"yalnız {summary.distinct_contracts} farklı kontrat (< {settings.min_distinct_contracts})"


def tickers_in(prints: Iterable[FlowPrint]) -> list[str]:
    return sorted({p.ticker.upper() for p in prints})
