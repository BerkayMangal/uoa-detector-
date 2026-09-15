"""Tradability chip and cost gate for the Alfa Board (Phase 5.2.A2). Pure functions.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A2 ("Row face",
"States", "Gate"); decisions P15 (disclosed defaults) and P16 (quote age).

Cost uses real prices only (R-CO1): entry at ask, exit at bid, both legs,
commission included. Mid is computed only as the denominator of spread %
(the ``penalties.py:62-65`` formula). It is never displayed.

- spread % of mid = (ask − bid) / ((ask + bid) / 2) × 100
- round trip for 1 contract = (ask − bid) × 100 + 2 × commission per contract
- 1 contract costs ask × 100 dollars, shown as a percent of
  ``sizing.capital_usd``
- exit depth = the NBBO bid size at the contract's last print (``/flow``), or
  unknown

States, evaluated in this order:

1. ``kotasyon yok`` (an unknown state, never ``İŞLENMEZ``) when:
   - there is no quote row;
   - UW did not return the symbol;
   - the NBBO is null (the contract has not traded today; probe 2026-09-15);
   - the quote is older than ``tradability.max_quote_age_seconds``;
   - ask is below bid.
2. ``İŞLENMEZ``: bid = 0, or spread % > ``penalty_triggers.spread_pct_threshold``
   (read from the calibration profile, never modified).
3. ``DAR``: spread % > ``tradability.max_tradable_spread_pct``, or a known exit
   depth below ``tradability.min_exit_bid_size``.
4. ``İŞLENİR``: everything else. An unknown exit depth does not demote a row.

Spread math runs in ``Decimal`` over each price's decimal text, like
``penalties.py``, so a quote sitting exactly on a cutoff classifies exactly.
An exit-depth reading older than ``max_quote_age_seconds`` counts as unknown.
The spread cutoff may be omitted only when no priced quote needs classifying.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from webapp.board.settings import CostSettings, SizingSettings, TradabilitySettings

TradabilityState = Literal["tradable", "narrow", "untradable", "no_quote"]

# Contract §5 A2, byte for byte.
STATE_LABELS: Final[Mapping[TradabilityState, str]] = MappingProxyType(
    {
        "tradable": "İŞLENİR",
        "narrow": "DAR",
        "untradable": "İŞLENMEZ",
        "no_quote": "kotasyon yok",
    },
)

REASON_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "wide_spread": "{ticker} %{spread} makas",
        "zero_bid": "{ticker} bid 0, çıkış fiyatı yok",
        "thin_exit": "{ticker} çıkış derinliği {size} kontrat",
        "no_quote_row": "{ticker} için kotasyon kaydı yok",
        "not_returned": "{ticker} sözleşmesi UW'de yok",
        "nbbo_null": "{ticker} bugün işlem görmedi, NBBO yok",
        "crossed": "{ticker} kotasyonu tutarsız (ask bid'in altında)",
        "stale": "{ticker} kotasyonu {age} sn önce alındı, eski",
    },
)

CHIP_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "spread": "Makas",
        "round_trip": "Gidiş-dönüş (1 kontrat, iki bacak, komisyon dahil)",
        "lot": "1 kontrat",
        "lot_pct": "sermayenin %{pct} kadarı",
        "exit_depth": "Çıkış derinliği",
        "contracts": "{size} kontrat",
        "unknown": "bilinmiyor",
        "quote_age": "kotasyon {seconds} sn önce alındı",
        "last_trade": "son işlem {minutes} dk önce",
        "at_last_trade": "son işlem anında",
        "default_value": "(varsayılan değer)",
        "bid_ask": "bid ${bid} / ask ${ask}",
    },
)

_PERCENT: Final = Decimal(100)
_CONTRACT_MULTIPLIER: Final = Decimal(100)
_LEGS: Final = Decimal(2)
_SECONDS_PER_MINUTE: Final = 60


@dataclass(frozen=True)
class QuoteView:
    """The board's read of one ``alfa_quote`` row."""

    option_symbol: str
    nbbo_bid: float | None
    nbbo_ask: float | None
    volume: int | None
    last_tape_time: datetime | None  # last trade today; None when untraded
    fetched_at: datetime
    returned: bool


@dataclass(frozen=True)
class DepthView:
    """The board's read of one ``alfa_contract_depth`` row (as of the last print)."""

    option_symbol: str
    nbbo_bid_size: int | None
    nbbo_ask_size: int | None
    quote_time: datetime | None  # the later of nbbo_bid_time and nbbo_ask_time
    fetched_at: datetime


@dataclass(frozen=True)
class TradabilityRead:
    state: TradabilityState
    reason: str | None
    bid: float | None
    ask: float | None
    spread_pct: float | None
    round_trip_usd: float | None
    lot_cost_usd: float | None
    lot_pct_capital: float | None
    exit_depth: int | None
    exit_depth_at_last_print: bool
    quote_age_seconds: int | None
    last_trade_minutes: int | None
    values_confirmed: bool

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def spread_pct_of_mid(bid: float, ask: float) -> float | None:
    """(ask − bid) / mid × 100, or ``None`` when mid is not positive."""
    spread = _spread_decimal(_dec(bid), _dec(ask))
    return None if spread is None else float(spread)


def _spread_decimal(bid: Decimal, ask: Decimal) -> Decimal | None:
    mid = (bid + ask) / _LEGS
    if mid <= 0:
        return None
    return (ask - bid) / mid * _PERCENT


def round_trip_usd(bid: float, ask: float, commission_per_contract_usd: float) -> float:
    """1 contract bought at ask and sold at bid, plus commission on both legs."""
    return float(
        (_dec(ask) - _dec(bid)) * _CONTRACT_MULTIPLIER
        + _LEGS * _dec(commission_per_contract_usd),
    )


def one_lot_cost_usd(ask: float) -> float:
    return float(_dec(ask) * _CONTRACT_MULTIPLIER)


def one_lot_pct_of_capital(ask: float, capital_usd: float) -> float:
    return float(_dec(ask) * _CONTRACT_MULTIPLIER / _dec(capital_usd) * _PERCENT)


def format_pct(value: float) -> str:
    """One decimal, trailing zero dropped: 17.0 → ``17``, 5.25 → ``5.2``."""
    return f"{round(value, 1):g}"


def _reason(key: str, **values: object) -> str:
    return REASON_TEMPLATES[key].format(**values)


def _age_seconds(since: datetime, now: datetime) -> int:
    return max(0, int((now - since).total_seconds()))


def assess_tradability(
    ticker: str,
    quote: QuoteView | None,
    depth: DepthView | None,
    *,
    tradability: TradabilitySettings,
    spread_cutoff_pct: float | None,
    cost: CostSettings,
    sizing: SizingSettings,
    now: datetime,
) -> TradabilityRead:
    """The chip for one contract: state, reason and the cost cells.

    Raises ``ValueError`` when a priced quote must be classified and
    ``spread_cutoff_pct`` is ``None``.
    """
    confirmed = sizing.values_confirmed_by_owner
    max_age = tradability.max_quote_age_seconds

    usable_depth = depth if depth is not None and _age_seconds(depth.fetched_at, now) <= max_age else None
    exit_depth = usable_depth.nbbo_bid_size if usable_depth is not None else None

    def read(state: TradabilityState, reason: str | None, *, priced: bool) -> TradabilityRead:
        bid = quote.nbbo_bid if quote is not None and priced else None
        ask = quote.nbbo_ask if quote is not None and priced else None
        last_trade: datetime | None = quote.last_tape_time if quote is not None else None
        if last_trade is None and usable_depth is not None:
            last_trade = usable_depth.quote_time
        return TradabilityRead(
            state=state,
            reason=reason,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct_of_mid(bid, ask) if bid is not None and ask is not None else None,
            round_trip_usd=(
                round_trip_usd(bid, ask, cost.commission_per_contract_usd)
                if bid is not None and ask is not None
                else None
            ),
            lot_cost_usd=one_lot_cost_usd(ask) if ask is not None else None,
            lot_pct_capital=one_lot_pct_of_capital(ask, sizing.capital_usd) if ask is not None else None,
            exit_depth=exit_depth,
            exit_depth_at_last_print=exit_depth is not None,
            quote_age_seconds=_age_seconds(quote.fetched_at, now) if quote is not None else None,
            last_trade_minutes=(
                _age_seconds(last_trade, now) // _SECONDS_PER_MINUTE if last_trade is not None else None
            ),
            values_confirmed=confirmed,
        )

    if quote is None:
        return read("no_quote", _reason("no_quote_row", ticker=ticker), priced=False)
    if not quote.returned:
        return read("no_quote", _reason("not_returned", ticker=ticker), priced=False)
    if quote.nbbo_bid is None or quote.nbbo_ask is None:
        return read("no_quote", _reason("nbbo_null", ticker=ticker), priced=False)
    age = _age_seconds(quote.fetched_at, now)
    if age > max_age:
        return read("no_quote", _reason("stale", ticker=ticker, age=age), priced=True)

    bid = _dec(quote.nbbo_bid)
    ask = _dec(quote.nbbo_ask)
    if ask < bid:
        return read("no_quote", _reason("crossed", ticker=ticker), priced=True)
    spread = _spread_decimal(bid, ask)
    if bid == 0 or spread is None:
        return read("untradable", _reason("zero_bid", ticker=ticker), priced=True)
    if spread_cutoff_pct is None:
        msg = "spread_cutoff_pct (penalty_triggers.spread_pct_threshold) is required to classify a quote"
        raise ValueError(msg)
    wide = _reason("wide_spread", ticker=ticker, spread=format_pct(float(spread)))
    if spread > _dec(spread_cutoff_pct):
        return read("untradable", wide, priced=True)
    if spread > _dec(tradability.max_tradable_spread_pct):
        return read("narrow", wide, priced=True)
    if exit_depth is not None and exit_depth < tradability.min_exit_bid_size:
        return read("narrow", _reason("thin_exit", ticker=ticker, size=exit_depth), priced=True)
    return read("tradable", None, priced=True)
