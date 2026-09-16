"""Chase verdict per Alfa Board row (Phase 5.2.B3, contract §6 B3). Pure functions.

One line that answers "did I miss it": what the flagged print paid, what an
entry would pay now, and how far the underlying has moved since.

- **Option change** = current ask / print price − 1, in percent. The print price
  is ``StoredSignal.option_price`` (``src/uoa_detector/backtest/store.py``); the
  ask is the dominant contract's executable ask (``tradability.executable_ask``),
  because an entry now would pay the ask (R-CO1). No usable quote is its own
  verdict, never a number.
- **Verdict bands** come from ``BoardSettings.chase``, in ``Decimal`` so a quote
  sitting exactly on a cutoff classifies exactly:

  | option change | verdict |
  |---|---|
  | ≤ ``reasonable_max_pct`` | ``hâlâ makul`` |
  | between the two cutoffs | ``dikkat`` |
  | ≥ ``late_min_pct`` | ``geç kaldın`` |
  | no usable quote | ``kotasyon yok`` |

- **Underlying move since the print** = spot now / spot at print − 1. Spot at
  print is ``moneyness × strike`` (``StoredSignal.moneyness`` is spot / strike);
  spot now is ``alfa_atm.stock_price``. A missing or stale ATM row reads
  ``bilinmiyor``: the option half of the line still stands on its own.
- **Flow context** is the net premium in the row's direction since the print
  (``netprem.read_net_premium_since``): ``sürüyor`` / ``döndü`` /
  ``bilinmiyor``. It is context only. It never enters the verdict, and it is
  never counted as evidence (K1).
- **Sold-option rows.** The board is written for a buyer entering at the ask
  (R-CO1), so the verdict stays ask-based even when the row's direction came
  from a sold option. The line says so rather than implying the owner would be
  the seller.

Every string comes from the frozen dictionaries below (R-WD1), and none of them
states a probability or an outcome (R-EV1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from webapp.board.tradability import executable_ask

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from uoa_detector.backtest.store import StoredSignal
    from webapp.board.atm import AtmView
    from webapp.board.direction import Direction
    from webapp.board.netprem import TapeSummary
    from webapp.board.settings import ChaseSettings
    from webapp.board.tradability import TradabilityRead

ChaseVerdict = Literal["reasonable", "caution", "late", "no_quote"]
FlowContext = Literal["continuing", "reversed", "unknown"]

_PERCENT: Final = Decimal(100)

# ---------------------------------------------------------------------------
# Frozen copy (contract §6 B3 wording)
# ---------------------------------------------------------------------------

VERDICT_LABELS: Final[Mapping[ChaseVerdict, str]] = MappingProxyType(
    {
        "reasonable": "hâlâ makul",
        "caution": "dikkat",
        "late": "geç kaldın",
        "no_quote": "kotasyon yok",
    },
)

FLOW_LABELS: Final[Mapping[FlowContext, str]] = MappingProxyType(
    {
        "continuing": "akış baskıdan beri sürüyor",
        "reversed": "akış baskıdan beri döndü",
        "unknown": "akış baskıdan beri: bilinmiyor",
    },
)

SIDE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"up": "yukarıda", "down": "aşağıda"},
)

CHASE_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "title": "Kovalama",
        "price": "baskı ${print} → şimdi ${ask} (ask), %{change} {side}",
        "price_flat": "baskı ${print} → şimdi ${ask} (ask), değişmedi",
        "price_no_quote": "baskı ${print} → işlem yapılabilir kotasyon yok",
        "price_unknown": "baskı fiyatı bilinmiyor",
        "underlying": "hisse baskıdan beri %{move}",
        "underlying_unknown": "hisse hareketi bilinmiyor",
        "sold": "bu satır satılan opsiyondan geliyor; hüküm ask'ten giren alıcıya göre",
        "separator": "; ",
    },
)


@dataclass(frozen=True)
class ChaseRead:
    """One row's chase reading. ``flow`` is context; it never moved the verdict."""

    verdict: ChaseVerdict
    print_price: float | None
    ask: float | None
    change_pct: float | None
    spot_at_print: float | None
    spot_now: float | None
    underlying_move_pct: float | None
    flow: FlowContext
    sold: bool

    @property
    def late(self) -> bool:
        """Feeds the mandatory counter-argument's priority 3 (contract §5 A5)."""
        return self.verdict == "late"

    @property
    def label(self) -> str:
        return VERDICT_LABELS[self.verdict]


@dataclass(frozen=True)
class ChaseText:
    """Display strings for one chase cell, formatted from the frozen templates."""

    title: str
    verdict: str
    line: str
    flow: str
    sold_note: str | None


def option_change_pct(print_price: float | None, ask: float | None) -> float | None:
    """``ask / print price − 1`` in percent; ``None`` when either price is unusable."""
    if print_price is None or ask is None:
        return None
    if not _finite(print_price, ask) or print_price <= 0 or ask <= 0:
        return None
    return float((_dec(ask) / _dec(print_price) - 1) * _PERCENT)


def classify_chase(change_pct: float | None, settings: ChaseSettings) -> ChaseVerdict:
    """The verdict band for an option-price change; no usable quote is its own state."""
    if change_pct is None:
        return "no_quote"
    change = _dec(change_pct)
    if change >= _dec(settings.late_min_pct):
        return "late"
    if change <= _dec(settings.reasonable_max_pct):
        return "reasonable"
    return "caution"


def spot_at_print(moneyness: float | Decimal | None, strike: float | Decimal | None) -> float | None:
    """``moneyness × strike``: the underlying price when the print was flagged."""
    if moneyness is None or strike is None:
        return None
    try:
        value = Decimal(str(moneyness)) * Decimal(str(strike))
    except (InvalidOperation, ValueError):
        return None
    return float(value) if value > 0 else None


def underlying_move_pct(at_print: float | None, now: float | None) -> float | None:
    """``spot now / spot at print − 1`` in percent; ``None`` when either is unusable."""
    if at_print is None or now is None:
        return None
    if not _finite(at_print, now) or at_print <= 0 or now <= 0:
        return None
    return float((_dec(now) / _dec(at_print) - 1) * _PERCENT)


def current_spot(
    atm_rows: Sequence[AtmView], *, max_age_seconds: int, now: datetime,
) -> float | None:
    """The newest ``alfa_atm`` stock price for the ticker, or ``None`` when missing or stale."""
    priced = [
        row for row in atm_rows
        if row.stock_price is not None and row.stock_price > 0 and math.isfinite(row.stock_price)
    ]
    if not priced:
        return None
    newest = max(priced, key=lambda row: row.fetched_at)
    age = (now - newest.fetched_at).total_seconds()
    if age > max_age_seconds:
        return None
    return newest.stock_price


def flow_context(tape: TapeSummary | None, direction: Direction) -> FlowContext:
    """Net premium in the row's direction since the print: context only, never a verdict input."""
    if tape is None:
        return "unknown"
    net = tape.net_premium_for(direction)
    if net > 0:
        return "continuing"
    if net < 0:
        return "reversed"
    return "unknown"


def build_chase(
    *,
    signal: StoredSignal | None,
    chip: TradabilityRead,
    direction: Direction,
    sold: bool,
    atm_rows: Sequence[AtmView],
    tape_since: TapeSummary | None,
    settings: ChaseSettings,
    max_spot_age_seconds: int,
    now: datetime,
) -> ChaseRead:
    """The chase reading for one row, from its source print and its current quote."""
    print_price = float(signal.option_price) if signal is not None else None
    ask = executable_ask(chip)
    change = option_change_pct(print_price, ask)
    at_print = (
        spot_at_print(signal.moneyness, signal.strike) if signal is not None else None
    )
    spot_now = current_spot(atm_rows, max_age_seconds=max_spot_age_seconds, now=now)
    return ChaseRead(
        verdict=classify_chase(change, settings),
        print_price=print_price,
        ask=ask,
        change_pct=change,
        spot_at_print=at_print,
        spot_now=spot_now,
        underlying_move_pct=underlying_move_pct(at_print, spot_now),
        flow=flow_context(tape_since, direction),
        sold=sold,
    )


def chase_text(read: ChaseRead) -> ChaseText:
    """The chase cell's strings; every unknown is labelled, never blank."""
    return ChaseText(
        title=CHASE_COPY["title"],
        verdict=read.label,
        line=CHASE_COPY["separator"].join((_price_part(read), _underlying_part(read))),
        flow=FLOW_LABELS[read.flow],
        sold_note=CHASE_COPY["sold"] if read.sold else None,
    )


def _price_part(read: ChaseRead) -> str:
    if read.print_price is None:
        return CHASE_COPY["price_unknown"]
    printed = _usd(read.print_price)
    if read.ask is None or read.change_pct is None:
        return CHASE_COPY["price_no_quote"].format(print=printed)
    values = {"print": printed, "ask": _usd(read.ask)}
    if read.change_pct == 0:
        return CHASE_COPY["price_flat"].format(**values)
    return CHASE_COPY["price"].format(
        **values,
        change=_pct(abs(read.change_pct)),
        side=SIDE_LABELS["up" if read.change_pct > 0 else "down"],
    )


def _underlying_part(read: ChaseRead) -> str:
    if read.underlying_move_pct is None:
        return CHASE_COPY["underlying_unknown"]
    return CHASE_COPY["underlying"].format(move=_signed_pct(read.underlying_move_pct))


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _usd(value: float) -> str:
    return f"{value:,.2f}"


def _pct(value: float) -> str:
    """One decimal, trailing zero dropped: 4.0 → ``4``, 18.75 → ``18.8``."""
    return f"{round(value, 1):g}"


def _signed_pct(value: float) -> str:
    """Always signed, one decimal: ``+0.6``, ``-1.2``."""
    return f"{value:+.1f}"
