"""Required move vs expected move for the Alfa Board (Phase 5.2.B2a, contract §6 B2).

Pure arithmetic over ``alfa_atm`` views; no I/O and no Unusual Whales calls.

- Required move for the dominant contract, entered at the current ask, against the
  current spot (``alfa_atm.stock_price``), in percent:
  - call: ``(strike + ask) / spot - 1``;
  - put: ``1 - (strike - ask) / spot``.
- Expected move = ATM straddle mid for the same expiry, or the nearest stored one:
  ``(call mid + put mid) / spot``. The ATM strike's offset from spot is disclosed,
  and so is a nearest-expiry substitution.
- Fallback, labelled ``IV tahmini (straddle değil)``: ``atm_iv * sqrt(days / 365)``.
  It is used when the ATM row has no two-sided quote (its IV is used), or when
  there is no ATM row and the caller passes an IV.

No probability is stated anywhere (R-EV1). Turkish copy comes only from the frozen
constants below (program rule 10).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Final, Literal
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from webapp.board.atm import AtmView

_ET: Final = ZoneInfo("America/New_York")
# Market structure, not a cutoff: US equity options stop trading at 16:00 ET.
_SESSION_CLOSE_ET: Final = time(16, 0)
# The contract's fallback formula: atm_iv * sqrt(days / 365).
_DAYS_PER_YEAR: Final = 365.0
_SECONDS_PER_DAY: Final = 86400.0
_PERCENT: Final = 100.0

# ---------------------------------------------------------------------------
# Frozen copy (contract §6 B2 wording)
# ---------------------------------------------------------------------------

REQUIRED_TEMPLATE: Final = "Başabaş için %{required} gerekir"
STRADDLE_TEMPLATE: Final = "ATM straddle bu vadeye %{expected} fiyatlıyor"
IV_FALLBACK_LABEL: Final = "IV tahmini (straddle değil)"
IV_FALLBACK_TEMPLATE: Final = "IV tahmini (straddle değil) bu vadeye %{expected} fiyatlıyor"
REQUIRED_UNKNOWN: Final = "Başabaş için gereken hareket: bilinmiyor"
EXPECTED_UNKNOWN: Final = "ATM straddle: bilinmiyor"
NEAREST_EXPIRY_TEMPLATE: Final = "en yakın ATM vadesi {expiry} (bu vade yok)"
STRIKE_OFFSET_TEMPLATE: Final = "ATM strike {strike}, spot {spot}, fark %{offset}"
SEPARATOR: Final = " · "

ExpectedSource = Literal["straddle", "iv_estimate"]


@dataclass(frozen=True)
class ExpectedMove:
    source: ExpectedSource
    expected_move_pct: float
    days_to_expiry: float
    atm_expiry: date | None
    same_expiry: bool
    atm_strike: float | None
    spot: float | None
    strike_offset_pct: float | None


@dataclass(frozen=True)
class MoveComparison:
    required_move_pct: float | None
    expected: ExpectedMove | None
    spot: float | None
    text: str
    disclosure: str


def required_move_pct(
    *,
    option_type: Literal["call", "put"],
    strike: float,
    ask: float | None,
    spot: float | None,
) -> float | None:
    """Break-even move in percent for a buyer at ``ask``; None when an input is unusable."""
    if ask is None or spot is None or not _finite(ask, spot, strike):
        return None
    if spot <= 0 or strike <= 0 or ask < 0:
        return None
    if option_type == "call":
        return ((strike + ask) / spot - 1.0) * _PERCENT
    return (1.0 - (strike - ask) / spot) * _PERCENT


def days_to_expiry(expiry: date, now: datetime) -> float | None:
    """Calendar days from ``now`` to the 16:00 ET close on ``expiry``; None once past."""
    close = datetime.combine(expiry, _SESSION_CLOSE_ET, tzinfo=_ET)
    seconds = (close - _as_utc(now)).total_seconds()
    return seconds / _SECONDS_PER_DAY if seconds > 0 else None


def straddle_mid_pct(row: AtmView) -> float | None:
    """(call mid + put mid) / spot in percent, or None without two two-sided quotes."""
    call_mid = _mid(row.call_bid, row.call_ask)
    put_mid = _mid(row.put_bid, row.put_ask)
    spot = row.stock_price
    if call_mid is None or put_mid is None or spot is None or spot <= 0:
        return None
    return (call_mid + put_mid) / spot * _PERCENT


def iv_move_pct(atm_iv: float | None, days: float | None) -> float | None:
    """Labelled fallback: ``atm_iv * sqrt(days / 365)`` in percent."""
    if atm_iv is None or days is None or not _finite(atm_iv, days) or atm_iv <= 0 or days <= 0:
        return None
    return atm_iv * math.sqrt(days / _DAYS_PER_YEAR) * _PERCENT


def expected_move(
    *,
    atm_rows: Sequence[AtmView],
    expiry: date,
    now: datetime,
    fallback_iv: float | None = None,
) -> ExpectedMove | None:
    """Straddle-implied move for ``expiry`` (or the nearest stored expiry), else the IV fallback."""
    days = days_to_expiry(expiry, now)
    if days is None:
        return None
    row = _nearest_row(atm_rows, expiry=expiry, now=now)
    if row is not None:
        spot = row.stock_price
        offset = (
            (row.strike - spot) / spot * _PERCENT if spot is not None and spot > 0 else None
        )
        straddle = straddle_mid_pct(row)
        if straddle is not None:
            return ExpectedMove(
                source="straddle", expected_move_pct=straddle, days_to_expiry=days,
                atm_expiry=row.expiry, same_expiry=row.expiry == expiry,
                atm_strike=row.strike, spot=spot, strike_offset_pct=offset,
            )
        row_iv = _mean_iv(row.call_iv, row.put_iv)
        estimate = iv_move_pct(row_iv, days)
        if estimate is not None:
            return ExpectedMove(
                source="iv_estimate", expected_move_pct=estimate, days_to_expiry=days,
                atm_expiry=row.expiry, same_expiry=row.expiry == expiry,
                atm_strike=row.strike, spot=spot, strike_offset_pct=offset,
            )
    estimate = iv_move_pct(fallback_iv, days)
    if estimate is None:
        return None
    return ExpectedMove(
        source="iv_estimate", expected_move_pct=estimate, days_to_expiry=days,
        atm_expiry=None, same_expiry=False, atm_strike=None, spot=None,
        strike_offset_pct=None,
    )


def compare_moves(
    *,
    option_type: Literal["call", "put"],
    strike: float,
    expiry: date,
    ask: float | None,
    atm_rows: Sequence[AtmView],
    now: datetime,
    fallback_iv: float | None = None,
) -> MoveComparison:
    """Required vs expected move for one contract, with its frozen-copy sentence."""
    row = _nearest_row(atm_rows, expiry=expiry, now=now)
    spot = row.stock_price if row is not None else None
    required = required_move_pct(option_type=option_type, strike=strike, ask=ask, spot=spot)
    expected = expected_move(atm_rows=atm_rows, expiry=expiry, now=now, fallback_iv=fallback_iv)
    return MoveComparison(
        required_move_pct=required, expected=expected, spot=spot,
        text=move_sentence(required, expected), disclosure=move_disclosure(expected),
    )


def move_sentence(required: float | None, expected: ExpectedMove | None) -> str:
    left = (
        REQUIRED_UNKNOWN if required is None
        else REQUIRED_TEMPLATE.format(required=_pct(required))
    )
    if expected is None:
        right = EXPECTED_UNKNOWN
    elif expected.source == "straddle":
        right = STRADDLE_TEMPLATE.format(expected=_pct(expected.expected_move_pct))
    else:
        right = IV_FALLBACK_TEMPLATE.format(expected=_pct(expected.expected_move_pct))
    return left + SEPARATOR + right


def move_disclosure(expected: ExpectedMove | None) -> str:
    """Strike offset and nearest-expiry substitution; empty when there is nothing to disclose."""
    if expected is None:
        return ""
    parts: list[str] = []
    if expected.atm_expiry is not None and not expected.same_expiry:
        parts.append(NEAREST_EXPIRY_TEMPLATE.format(expiry=expected.atm_expiry.strftime("%d.%m")))
    if (
        expected.atm_strike is not None
        and expected.spot is not None
        and expected.strike_offset_pct is not None
    ):
        parts.append(STRIKE_OFFSET_TEMPLATE.format(
            strike=_price(expected.atm_strike), spot=_price(expected.spot),
            offset=_pct(expected.strike_offset_pct),
        ))
    return SEPARATOR.join(parts)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _nearest_row(rows: Sequence[AtmView], *, expiry: date, now: datetime) -> AtmView | None:
    today = _as_utc(now).astimezone(_ET).date()
    live = [r for r in rows if r.expiry >= today]
    if not live:
        return None
    return min(live, key=lambda r: (abs((r.expiry - expiry).days), r.expiry))


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or not _finite(bid, ask):
        return None
    if bid < 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _mean_iv(call_iv: float | None, put_iv: float | None) -> float | None:
    values = [v for v in (call_iv, put_iv) if v is not None and math.isfinite(v) and v > 0]
    return sum(values) / len(values) if values else None


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _pct(value: float) -> str:
    return f"{value:.1f}"


def _price(value: float) -> str:
    return f"{value:g}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
