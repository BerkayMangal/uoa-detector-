"""ThetaData OPRA → ``RawPrint`` field mapping.

Phase 3.3.2.2: pure functions, no I/O. The historical downloader
and the live source both feed rows through these helpers to
canonicalise into the ``RawPrint`` schema the rest of the pipeline
consumes.

ThetaData trade endpoint format (per
https://http-docs.thetadata.us/operations/get-hist-option-trade.html):

  header.format: [ms_of_day, sequence, ext_condition1, ext_condition2,
                  ext_condition3, ext_condition4, condition, size,
                  exchange, price, condition_flags, price_flags,
                  volume_type, records_back, date]

  Example:       [43860664, 602567584, 255, 255, 255, 255, 125,
                  1, 43, 5.84, 0, 1, 0, 0, 20240116]

Field semantics:
  - ms_of_day:  milliseconds since midnight ET (NOT UTC)
  - condition:  OPRA condition code 0-255; 0 = regular sale,
                certain values are cancels / out-of-sequence
                that we filter out (see ``OPRA_DROP_CONDITIONS``)
  - exchange:   numeric exchange code (see ThetaData exchange map)
  - price:      decimal price in dollars
  - size:       contracts traded
  - date:       YYYYMMDD integer

Quote endpoint (separate call) provides bid/ask:
  [ms_of_day, bid_size, bid_exchange, bid, bid_condition,
   ask_size, ask_exchange, ask, ask_condition, date]

Mapping responsibilities:
  1. ET → UTC timestamp conversion (ms_of_day + date → datetime)
  2. Strike normalisation (1/10 cent integer → Decimal dollars)
     — but strike comes from the contract spec, not the row
  3. Condition-code filtering: drop cancels, out-of-sequence
     re-prints, and extended-hours trades that 'do not update
     OHLC' per OPRA spec
  4. Exchange code → human-readable string
  5. Compose ``RawPrint`` with quote data taken-as-of-trade

decision (ET → UTC at the boundary):
  RawPrint timestamps are tz-aware UTC throughout the codebase
  (Phase 1 invariant). ThetaData publishes in ET. We convert
  exactly once, here at the source boundary. ZoneInfo handles
  US/Eastern DST transitions correctly.

decision (drop conditions, not annotate):
  The OPRA spec is explicit that certain conditions don't update
  OHLC — cancels, late out-of-sequence reports. We DROP these at
  the mapping layer (return None) rather than passing them
  through with a tag. Reasoning: every downstream stage would
  need to know about cancel-codes; filtering once is cleaner
  and matches the 'canonical print stream' contract.

decision (no fill_side inference here):
  The acceptance doc's M34 sweep classifier is the system of
  record for fill_side. The mapping passes fill_side='unknown'
  and lets downstream classify.

decision (Pydantic input models, not dict[str, object]):
  Trade and quote rows arrive from the ThetaData JSON response
  as positional arrays; the caller is responsible for the
  array → dict transformation guided by response.header.format.
  Validating them through TradeRow / QuoteRow Pydantic models
  gives us mypy --strict cleanliness, runtime validation that
  rejects malformed rows early, and declarative documentation
  of the row shape. The helpers ``trade_row_from_array`` and
  ``quote_row_from_array`` encapsulate the positional → keyword
  conversion.

decision (post-close filtering by time, not condition code alone):
  OPRA condition codes for extended-hours trades exist but are
  implementation-dependent across participants. Time-of-day
  filtering (09:30-16:00 ET inclusive) is the reliable backstop.
  Half-day sessions (Black Friday, Christmas Eve) close at
  13:00 ET; the historical downloader's calendar layer is
  responsible for not requesting half-day data outside those
  hours.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from uoa_detector.domain.raw_print import RawPrint

_ET = ZoneInfo("America/New_York")


# OPRA condition codes that should NOT produce a RawPrint.
#
# Source: OPRA Pillar Output Specification + ThetaData docs.
# Codes here are the ones that:
#   - cancel a previous trade (post-fact corrections)
#   - report late and out-of-sequence (would corrupt event-time order)
#
# Codes NOT in this set are treated as regular sales — including the
# many informational-prefix codes (electronic, ISO, single-leg-floor,
# etc.) that the OPRA spec says 'process as a regular transaction'.
#
# Extended-hours filtering happens via time-of-day check (see
# ``et_ms_in_regular_hours``) because the OPRA-extended-hours
# condition code is implementation-dependent.
OPRA_DROP_CONDITIONS: Final[frozenset[int]] = frozenset(
    {
        # Out-of-sequence late reports — re-ordering would break the
        # event-time-monotonic contract Phase 2.3.3 fusion depends on.
        2,    # Sold out of sequence
        # Cancel-class — these reverse a prior trade.
        7,    # Cancel
        12,   # Cancel previous open
        13,   # Cancel only one trade
    },
)


# Regular US options market hours (ET).
_REGULAR_OPEN_ET: Final[time] = time(9, 30)
_REGULAR_CLOSE_ET: Final[time] = time(16, 0)


# ThetaData numeric exchange codes → human-readable string.
#
# Includes the major OPRA participants. Unknown codes pass through
# as 'thetadata_<n>' rather than dropping the trade — keeps the
# data stream complete even if a new exchange joins OPRA between
# releases.
_EXCHANGE_NAMES: Final[dict[int, str]] = {
    0:  "NYSE_AMEX",
    1:  "BSE_BX",
    2:  "BX",
    3:  "NSDQ",
    4:  "ARCA",
    5:  "PSE",
    6:  "PHLX",
    7:  "C2",
    8:  "ISE_GEM",
    9:  "GEMINI",
    10: "MIAX",
    14: "BATS",
    15: "EDGX",
    16: "MERCURY",
    18: "CBOE",
    21: "EDGO",
    22: "AMEX",
    23: "BOX",
    25: "EMERALD",
    43: "OPRA",  # generic OPRA-routed
    50: "NYSE",
}


def thetadata_exchange_name(code: int) -> str:
    """Map ThetaData numeric exchange code to a string.

    Unknown codes return ``thetadata_<n>`` rather than raising — a
    new exchange joining OPRA shouldn't break the live stream.
    """
    return _EXCHANGE_NAMES.get(code, f"thetadata_{code}")


def et_ms_to_utc_datetime(*, date_yyyymmdd: int, ms_of_day: int) -> datetime:
    """Convert (YYYYMMDD int, ms-of-day-ET int) → tz-aware UTC datetime.

    ThetaData publishes timestamps in ET; the rest of the codebase
    works in UTC. Conversion happens exactly once, here at the
    boundary. ZoneInfo handles DST automatically.
    """
    yyyy = date_yyyymmdd // 10000
    mm = (date_yyyymmdd // 100) % 100
    dd = date_yyyymmdd % 100
    seconds, ms_remainder = divmod(ms_of_day, 1000)
    hours, sec_remainder = divmod(seconds, 3600)
    minutes, sec = divmod(sec_remainder, 60)
    et_dt = datetime(
        yyyy, mm, dd, hours, minutes, sec, ms_remainder * 1000,
        tzinfo=_ET,
    )
    return et_dt.astimezone(UTC)


def et_ms_in_regular_hours(ms_of_day: int) -> bool:
    """True if ``ms_of_day`` falls in 09:30-16:00 ET inclusive.

    Used by the trade row filter. The downloader's calendar layer
    handles half-day closes (13:00 ET); this function is the
    backstop against extended-hours trades sneaking through.
    """
    seconds, _ = divmod(ms_of_day, 1000)
    hours, sec_remainder = divmod(seconds, 3600)
    minutes, sec = divmod(sec_remainder, 60)
    t = time(hours, minutes, sec)
    return _REGULAR_OPEN_ET <= t <= _REGULAR_CLOSE_ET


def should_drop_trade(condition: int, ms_of_day: int) -> bool:
    """True if this OPRA trade should be dropped from the canonical stream.

    Two filters:
      1. Condition code in the drop set (cancels, out-of-sequence)
      2. Time outside regular market hours (extended-hours trades
         that 'do not update OHLC' per OPRA spec)
    """
    if condition in OPRA_DROP_CONDITIONS:
        return True
    return not et_ms_in_regular_hours(ms_of_day)


# ---------------------------------------------------------------------------
# Input row models — Pydantic-validated views on ThetaData JSON arrays
# ---------------------------------------------------------------------------


OptionRight = Literal["C", "P"]


class TradeRow(BaseModel):
    """One ThetaData trade endpoint row, validated.

    Fields match positions in ``response.header.format``:
      [ms_of_day, sequence, ext_condition1, ext_condition2,
       ext_condition3, ext_condition4, condition, size, exchange,
       price, condition_flags, price_flags, volume_type,
       records_back, date]

    The caller (client.py) is responsible for the positional →
    keyword conversion via ``trade_row_from_array``. We accept the
    dict here so callers and tests can pass kwargs explicitly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    ms_of_day: int = Field(ge=0, le=24 * 60 * 60 * 1000)
    sequence: int = Field(ge=0)
    condition: int = Field(ge=0, le=255)
    size: int = Field(ge=0)
    exchange: int = Field(ge=0)
    price: Decimal = Field(ge=Decimal(0))
    date_yyyymmdd: int = Field(ge=19000101, le=99991231, alias="date")


class QuoteRow(BaseModel):
    """One ThetaData quote endpoint row, validated.

    Fields match the quote endpoint's header.format:
      [ms_of_day, bid_size, bid_exchange, bid, bid_condition,
       ask_size, ask_exchange, ask, ask_condition, date]
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ms_of_day: int = Field(ge=0, le=24 * 60 * 60 * 1000)
    bid: Decimal = Field(ge=Decimal(0))
    ask: Decimal = Field(ge=Decimal(0))
    bid_size: int = Field(default=0, ge=0)
    ask_size: int = Field(default=0, ge=0)


_TRADE_FIELDS: Final[tuple[str, ...]] = (
    "ms_of_day", "sequence",
    "ext_condition1", "ext_condition2", "ext_condition3", "ext_condition4",
    "condition", "size", "exchange", "price",
    "condition_flags", "price_flags", "volume_type", "records_back",
    "date",
)


_QUOTE_FIELDS: Final[tuple[str, ...]] = (
    "ms_of_day",
    "bid_size", "bid_exchange", "bid", "bid_condition",
    "ask_size", "ask_exchange", "ask", "ask_condition",
    "date",
)


# Field names that TradeRow / QuoteRow consume (the rest are
# discarded). We only forward known fields to the Pydantic model.
_TRADE_KEEP: Final[frozenset[str]] = frozenset(
    {"ms_of_day", "sequence", "condition", "size", "exchange",
     "price", "date"},
)


_QUOTE_KEEP: Final[frozenset[str]] = frozenset(
    {"ms_of_day", "bid", "ask", "bid_size", "ask_size"},
)


def trade_row_from_array(
    values: list[float | int],
    *,
    fields: tuple[str, ...] = _TRADE_FIELDS,
) -> TradeRow:
    """Build a ``TradeRow`` from a ThetaData JSON positional array.

    ThetaData responses look like::

        {"header": {"format": ["ms_of_day", "sequence", ..., "date"]},
         "response": [[43860664, 602567584, ..., 20240116], ...]}

    The caller passes one element of ``response`` (an array) plus
    optionally the format header (defaults to the documented order).
    We zip them, drop fields TradeRow doesn't model, and validate.
    """
    if len(values) != len(fields):
        msg = (
            f"TradeRow array has {len(values)} values, expected "
            f"{len(fields)} (per ThetaData header.format)"
        )
        raise ValueError(msg)
    raw = dict(zip(fields, values, strict=True))
    kept: dict[str, float | int] = {
        k: v for k, v in raw.items() if k in _TRADE_KEEP
    }
    return TradeRow.model_validate(kept)


def quote_row_from_array(
    values: list[float | int],
    *,
    fields: tuple[str, ...] = _QUOTE_FIELDS,
) -> QuoteRow:
    """Build a ``QuoteRow`` from a ThetaData JSON positional array."""
    if len(values) != len(fields):
        msg = (
            f"QuoteRow array has {len(values)} values, expected "
            f"{len(fields)} (per ThetaData header.format)"
        )
        raise ValueError(msg)
    raw = dict(zip(fields, values, strict=True))
    kept: dict[str, float | int] = {
        k: v for k, v in raw.items() if k in _QUOTE_KEEP
    }
    return QuoteRow.model_validate(kept)


# ---------------------------------------------------------------------------
# Strike conversions
# ---------------------------------------------------------------------------


def thetadata_strike_to_dollars(strike_tenths_of_cent: int) -> Decimal:
    """Convert ThetaData strike (1/10 cent integer) → dollars Decimal.

    ThetaData encodes strikes as integers in tenths of a cent so a
    $170.00 strike is the integer 170000 and $170.50 is 170500.
    Always exact (no float rounding).
    """
    return Decimal(strike_tenths_of_cent) / Decimal(10000)


def thetadata_dollars_to_strike(dollars: Decimal) -> int:
    """Inverse of ``thetadata_strike_to_dollars``.

    Used when constructing API request URLs from a Decimal strike.
    Asserts the result is a whole integer (ThetaData rejects
    fractional 1/10-cent strikes).
    """
    scaled = dollars * Decimal(10000)
    if scaled != scaled.to_integral_value():
        msg = (
            f"strike {dollars} cannot be represented as integer 1/10 "
            "cents; ThetaData requires whole strikes"
        )
        raise ValueError(msg)
    return int(scaled)


# ---------------------------------------------------------------------------
# Trade → RawPrint mapping
# ---------------------------------------------------------------------------


def map_thetadata_trade_to_rawprint(
    *,
    trade_row: TradeRow,
    ticker: str,
    expiry: date,
    strike_dollars: Decimal,
    right: OptionRight,
    spot_price: Decimal,
    bid: Decimal,
    ask: Decimal,
    open_interest: int | None = None,
    implied_volatility: float | None = None,
    source_id: str = "thetadata",
) -> RawPrint | None:
    """Map one ThetaData trade row + quote snapshot to a ``RawPrint``.

    Returns ``None`` if the trade is filtered (cancels, out-of-sequence,
    extended hours) or if DTE is negative (data error).

    ``trade_row`` is a validated ``TradeRow``; build it from the raw
    JSON array via ``trade_row_from_array``.

    Strike, expiry, and right come from the request parameters (the
    contract being queried), not the row data. ``spot_price``, ``bid``,
    ``ask``, ``open_interest``, ``implied_volatility`` come from
    sibling endpoints and must be aligned to the trade timestamp by
    the caller (Phase 3.3.2.4 historical downloader does this).
    """
    if should_drop_trade(trade_row.condition, trade_row.ms_of_day):
        return None

    timestamp = et_ms_to_utc_datetime(
        date_yyyymmdd=trade_row.date_yyyymmdd, ms_of_day=trade_row.ms_of_day,
    )

    # DTE: calendar days from trade date to expiry.
    date_int = trade_row.date_yyyymmdd
    trade_d = date(date_int // 10000, (date_int // 100) % 100, date_int % 100)
    dte = (expiry - trade_d).days
    if dte < 0:
        # Defensive — a trade reported on a date past expiry is
        # definitely a data error; drop rather than pass through.
        return None

    # Premium paid in dollars (price × size × 100, OCC contract size).
    premium_paid = trade_row.price * Decimal(trade_row.size) * Decimal(100)

    return RawPrint(
        source_id=source_id,
        source_event_id=f"thetadata-{date_int}-{trade_row.sequence}",
        timestamp=timestamp,
        ticker=ticker.upper(),
        option_type="call" if right == "C" else "put",
        strike=strike_dollars,
        expiry=expiry,
        dte=dte,
        spot_price=spot_price,
        premium_paid=premium_paid,
        option_price=trade_row.price,
        bid=bid,
        ask=ask,
        fill_side="unknown",  # M34 classifies; mapping doesn't infer
        exchange=thetadata_exchange_name(trade_row.exchange),
        implied_volatility=implied_volatility,
        open_interest=open_interest,
        is_iso=False,
        source_tags=(),
    )


def map_thetadata_quote_to_bid_ask(
    quote_row: QuoteRow,
) -> tuple[Decimal, Decimal]:
    """Extract (bid, ask) from a validated ``QuoteRow``."""
    return quote_row.bid, quote_row.ask
