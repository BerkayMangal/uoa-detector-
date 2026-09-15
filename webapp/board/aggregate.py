"""Per-(ticker, direction) aggregation for the Alfa Board (Phase 5.2.A1).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A1; decisions P11, P12.

Pure functions over ``BoardPrint`` values; no database and no network.

- **Row.** One per (ticker, side-aware direction) over the whole run. It has
  total premium, print count, distinct contracts and the dominant contract:
  the largest summed premium per (strike, expiry, type).
- **Concentration** = the top strike's summed premium / the row's total
  premium, in percent. A strike is a strike price, summed across expiries
  and types.
- **Dominance** = row premium / (row premium + the same ticker's
  opposite-direction premium), in percent.
- **Position read** comes from ``BoardSettings.aggregation``:
  - ``kasıtlı pozisyon``: concentration ≥ ``intentional_min_top_strike_share_pct``;
  - ``dağınık envanter``: concentration ≤ ``scattered_max_top_strike_share_pct``
    with at least ``min_strikes_for_scattered`` distinct strikes;
  - ``karışık``: anything else, including a zero-premium row.
- **Side counts.** Side-aware and option-type-fallback prints merge into the
  same row, but the row keeps both counts, so nothing merges silently.
- **Order.** Rows sort by total premium, descending. The combined score is
  never a sort key (R-EV2).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from webapp.board.direction import DIRECTION_LABELS, Direction, direction_for, opposite

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import date, datetime

    from webapp.board.settings import AggregationSettings
    from webapp.board.signals import BoardPrint

PositionRead = Literal["intentional", "scattered", "mixed"]

# Contract §5 A1, byte for byte.
POSITION_READ_LABELS: Final[Mapping[PositionRead, str]] = MappingProxyType(
    {
        "intentional": "kasıtlı pozisyon",
        "scattered": "dağınık envanter",
        "mixed": "karışık",
    },
)

_PERCENT: Final = Decimal(100)


@dataclass(frozen=True, order=True)
class ContractKey:
    """A listed contract: strike, expiry and option type."""

    expiry: date
    strike: Decimal
    option_type: str


@dataclass(frozen=True)
class ContractSummary:
    key: ContractKey
    premium: Decimal
    print_count: int
    option_chain: str | None  # the UW chain recorded on a constituent print, if any


@dataclass(frozen=True)
class RowPrint:
    """One constituent print, as listed in the row's detail view."""

    run_id: str
    event_id: str
    timestamp: datetime
    option_type: str
    strike: Decimal
    expiry: date
    dte: int
    premium: Decimal
    option_price: Decimal
    fill_side: str | None  # None: legacy row without print meta
    option_chain: str | None
    side_aware: bool


@dataclass(frozen=True)
class BoardRow:
    ticker: str
    direction: Direction
    total_premium: Decimal
    print_count: int
    distinct_contracts: int
    distinct_strikes: int
    dominant: ContractSummary
    contracts: tuple[ContractSummary, ...]  # summed premium, descending
    top_strike: Decimal
    concentration_pct: float | None
    dominance_pct: float | None
    position_read: PositionRead
    side_aware_prints: int
    fallback_prints: int
    prints: tuple[RowPrint, ...]  # newest first
    latest_ts: datetime

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS[self.direction]

    @property
    def position_label(self) -> str:
        return POSITION_READ_LABELS[self.position_read]


def classify_position(
    concentration_pct: float | None,
    distinct_strikes: int,
    settings: AggregationSettings,
) -> PositionRead:
    """Position read from the top-strike share and the strike count."""
    if concentration_pct is None:
        return "mixed"
    if concentration_pct >= settings.intentional_min_top_strike_share_pct:
        return "intentional"
    if (
        concentration_pct <= settings.scattered_max_top_strike_share_pct
        and distinct_strikes >= settings.min_strikes_for_scattered
    ):
        return "scattered"
    return "mixed"


def _row_print(p: BoardPrint) -> tuple[Direction, RowPrint]:
    sig = p.signal
    fill_side = p.meta.fill_side if p.meta is not None else None
    read = direction_for(sig.option_type, fill_side)
    return read.direction, RowPrint(
        run_id=p.run_id,
        event_id=p.event_id,
        timestamp=sig.timestamp,
        option_type=sig.option_type,
        strike=sig.strike,
        expiry=sig.expiry,
        dte=sig.dte,
        premium=sig.premium,
        option_price=sig.option_price,
        fill_side=fill_side,
        option_chain=p.meta.option_chain if p.meta is not None else None,
        side_aware=read.side_aware,
    )


def _share_pct(part: Decimal, whole: Decimal) -> float | None:
    if whole <= 0:
        return None
    return float(part * _PERCENT / whole)


def _contracts(prints: Iterable[RowPrint]) -> tuple[ContractSummary, ...]:
    premium: dict[ContractKey, Decimal] = defaultdict(Decimal)
    count: dict[ContractKey, int] = defaultdict(int)
    chain: dict[ContractKey, str] = {}
    for rp in prints:
        key = ContractKey(expiry=rp.expiry, strike=rp.strike, option_type=rp.option_type)
        premium[key] += rp.premium
        count[key] += 1
        if rp.option_chain is not None:
            chain.setdefault(key, rp.option_chain)
    summaries = [
        ContractSummary(key=k, premium=premium[k], print_count=count[k], option_chain=chain.get(k))
        for k in premium
    ]
    # Largest summed premium first; ties: more prints, nearer expiry, lower strike, call first.
    summaries.sort(key=lambda s: (-s.premium, -s.print_count, s.key))
    return tuple(summaries)


def _top_strike(prints: Iterable[RowPrint]) -> tuple[Decimal, Decimal, int]:
    by_strike: dict[Decimal, Decimal] = defaultdict(Decimal)
    for rp in prints:
        by_strike[rp.strike] += rp.premium
    strike, premium = min(by_strike.items(), key=lambda item: (-item[1], item[0]))
    return strike, premium, len(by_strike)


def build_board_rows(prints: Iterable[BoardPrint], settings: AggregationSettings) -> list[BoardRow]:
    """One ``BoardRow`` per (ticker, direction), sorted by total premium, descending."""
    grouped: dict[tuple[str, Direction], list[RowPrint]] = defaultdict(list)
    for p in prints:
        direction, rp = _row_print(p)
        grouped[(p.signal.ticker, direction)].append(rp)

    totals: dict[tuple[str, Direction], Decimal] = {
        key: sum((rp.premium for rp in members), Decimal(0)) for key, members in grouped.items()
    }

    rows: list[BoardRow] = []
    for (ticker, direction), members in grouped.items():
        total = totals[(ticker, direction)]
        opposite_total = totals.get((ticker, opposite(direction)), Decimal(0))
        contracts = _contracts(members)
        top_strike, top_premium, strike_count = _top_strike(members)
        concentration = _share_pct(top_premium, total)
        newest_first = tuple(sorted(members, key=lambda rp: (rp.timestamp, rp.event_id), reverse=True))
        side_aware = sum(1 for rp in members if rp.side_aware)
        rows.append(
            BoardRow(
                ticker=ticker,
                direction=direction,
                total_premium=total,
                print_count=len(members),
                distinct_contracts=len(contracts),
                distinct_strikes=strike_count,
                dominant=contracts[0],
                contracts=contracts,
                top_strike=top_strike,
                concentration_pct=concentration,
                dominance_pct=_share_pct(total, total + opposite_total),
                position_read=classify_position(concentration, strike_count, settings),
                side_aware_prints=side_aware,
                fallback_prints=len(members) - side_aware,
                prints=newest_first,
                latest_ts=newest_first[0].timestamp,
            ),
        )
    rows.sort(key=lambda r: (-r.total_premium, r.ticker, r.direction))
    return rows
