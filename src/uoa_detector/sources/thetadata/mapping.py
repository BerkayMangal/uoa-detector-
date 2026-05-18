"""ThetaData OPRA → ``RawPrint`` field mapping.

Phase 3.3.2.2: pure functions, no I/O. The historical downloader
and the live source both feed rows through these helpers to
canonicalise into the ``RawPrint`` schema the rest of the pipeline
consumes.

Phase 3.3.7.2 (v2 → v3): ThetaData REST API moved from v2 to v3.
The wire formats differ:
  - v2 REST + v3 streaming WS:  positional JSON arrays, ms_of_day
    + date_yyyymmdd integer pair, 1/10-cent integer strikes, 'C'/'P'
    rights.
  - v3 REST: array of named-dict objects, ISO timestamp strings,
    dollar float strikes, 'call'/'put' rights.
The streaming WS still uses the v2-style positional encoding per
docs.thetadata.us/Streaming/Getting-Started.html. mapping.py
exposes parsers + formatters for BOTH wire formats so historical
(v3 REST) and live (v3 streaming WS) callers each have the right
boundary.

ThetaData v2 trade endpoint format (per
https://http-docs.thetadata.us/operations/get-hist-option-trade.html);
identical positional shape as v3 streaming WS messages:

  header.format: [ms_of_day, sequence, ext_condition1, ext_condition2,
                  ext_condition3, ext_condition4, condition, size,
                  exchange, price, condition_flags, price_flags,
                  volume_type, records_back, date]

  Example:       [43860664, 602567584, 255, 255, 255, 255, 125,
                  1, 43, 5.84, 0, 1, 0, 0, 20240116]

ThetaData v3 trade endpoint shape (per
https://docs.thetadata.us/operations/option_history_trade.html):

  Array of objects, each:
    {"symbol": "AAPL", "expiration": "2024-11-08", "strike": 220.00,
     "right": "call", "timestamp": "2024-11-04T09:30:00.000",
     "sequence": 12345, "ext_condition1": 0, ..., "condition": 0,
     "size": 1, "exchange": 7, "price": 12.50}

Field semantics (shared across v2/v3 representations):
  - ms_of_day:  milliseconds since midnight ET (NOT UTC)
  - condition:  OPRA condition code 0-255; 0 = regular sale,
                certain values are cancels / out-of-sequence
                that we filter out (see ``OPRA_DROP_CONDITIONS``)
  - exchange:   numeric exchange code (see ThetaData exchange map)
  - price:      decimal price in dollars
  - size:       contracts traded
  - date:       YYYYMMDD integer
  - timestamp (v3 REST): "YYYY-MM-DDTHH:mm:ss.SSS" ET-naive ISO
                string (parsed via ``iso_timestamp_to_utc_datetime``)

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

decision (fill_side inferred from the NBBO — Phase 3.5.5 A1):
  Originally the mapping passed fill_side='unknown' on the
  assumption M34 would classify it. M34 only *reads* fill_side,
  never derives it, so historical replays had no aggression
  signal at all. The mapping now classifies fill_side from the
  trade price against bid/ask (``classify_fill_side``).

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

from uoa_detector.domain.events import FillSide, OptionType
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

    Used for v2 REST + v3 streaming WS messages (both still use
    the (date, ms_of_day) integer pair encoding).
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


def iso_timestamp_to_utc_datetime(s: str) -> datetime:
    """Convert v3 REST ISO timestamp string → tz-aware UTC datetime.

    Phase 3.3.7.2 (J1 from migration spec): v3 REST responses
    carry timestamps as ISO strings of the form
    ``YYYY-MM-DDTHH:mm:ss.SSS`` (no timezone marker).

    ThetaData reports ET; we attach ET as the implicit timezone
    here at the boundary, then convert to UTC. If a future v3
    response includes an explicit timezone (e.g., trailing 'Z' or
    '+00:00'), we honour it directly without re-interpreting.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt.astimezone(UTC)


def utc_datetime_to_et_date_ms(dt: datetime) -> tuple[int, int]:
    """Convert a UTC datetime → (date_yyyymmdd, ms_of_day) ET pair.

    Phase 3.3.7.2 helper: bridges ``iso_timestamp_to_utc_datetime``
    output back to the (date, ms_of_day) representation that
    ``TradeRow`` and ``QuoteRow`` model. Used by the v3 REST row
    parsers so the downstream ``map_thetadata_*`` functions don't
    need a separate code path for v3 vs v2 timestamps.
    """
    if dt.tzinfo is None:
        msg = "utc_datetime_to_et_date_ms requires a tz-aware datetime"
        raise ValueError(msg)
    et_dt = dt.astimezone(_ET)
    date_yyyymmdd = et_dt.year * 10000 + et_dt.month * 100 + et_dt.day
    ms_of_day = (
        et_dt.hour * 3_600_000
        + et_dt.minute * 60_000
        + et_dt.second * 1_000
        + et_dt.microsecond // 1000
    )
    return date_yyyymmdd, ms_of_day


def format_v3_strike_param(dollars: Decimal) -> str:
    """Format a Decimal strike as the v3 REST URL ``strike`` query value.

    Phase 3.3.7.2 (J4 from migration spec): v3 REST takes
    ``strike`` as a string in dollars. The doc's canonical
    example uses 2 decimals (``"100.00"``). v3 also accepts 3
    decimals (``"220.000"``); we standardise on 2 to match the
    documentation's canonical representation.

    Asserts the strike fits 2-decimal precision exactly. v3 doesn't
    document support for fractional pennies; rejecting them here
    is consistent with v2's whole-1/10-cent invariant.
    """
    quantized = dollars.quantize(Decimal("0.01"))
    if quantized != dollars:
        msg = (
            f"strike {dollars} cannot be represented in 2 decimals; "
            f"v3 REST strike parameter requires whole-cent precision"
        )
        raise ValueError(msg)
    return f"{quantized:.2f}"


def format_v3_right(opt_type: OptionType) -> Literal["call", "put"]:
    """Format domain ``OptionType`` as the v3 REST URL ``right`` value.

    Phase 3.3.7.2: domain ``OptionType`` is already
    ``Literal["call", "put"]`` (matches v3 wire). Function is
    explicit identity to centralise wire-format conversions in
    mapping.py — call sites in historical.py make the v3 contract
    visible at the boundary.
    """
    if opt_type == "call":
        return "call"
    if opt_type == "put":
        return "put"
    msg = f"format_v3_right: unsupported option_type {opt_type!r}"
    raise ValueError(msg)


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

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    ms_of_day: int = Field(ge=0, le=24 * 60 * 60 * 1000)
    # Phase 3.3.13: v3 Terminal emits 32-bit signed sequence values
    # (negative IDs observed in production). The legacy ge=0
    # constraint rejected valid rows. Sequence is an opaque
    # tie-breaker; sign is irrelevant downstream.
    sequence: int = Field()
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

    model_config = ConfigDict(frozen=True, extra="ignore")

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
# v3 REST dict → row constructors (Phase 3.3.7.2)
# ---------------------------------------------------------------------------
#
# v3 REST returns rows as named-dict objects (not positional arrays).
# Timestamps are ISO strings in ET. We re-encode each row into the
# same TradeRow / QuoteRow shape the streaming-WS path produces, so
# downstream callers (``map_thetadata_*``) don't branch on wire
# version.


def trade_row_from_v3_dict(obj: dict[str, object]) -> TradeRow:
    """Build a ``TradeRow`` from one v3 REST trade response object.

    v3 trade response shape (per
    docs.thetadata.us/operations/option_history_trade.html):

        {"symbol": str, "expiration": str, "strike": float,
         "right": "call"|"put", "timestamp": str (ISO),
         "sequence": int, "ext_condition1..4": int, "condition": int,
         "size": int, "exchange": int, "price": float}

    Symbol / expiration / strike / right are redundant with the
    request params (we know what we asked for) and are dropped via
    extra="ignore". Timestamp ISO string → (date_yyyymmdd, ms_of_day)
    for storage in TradeRow, matching the v2 streaming WS shape.
    """
    timestamp_raw = obj.get("timestamp")
    if not isinstance(timestamp_raw, str):
        msg = (
            "trade_row_from_v3_dict: 'timestamp' missing or not a "
            f"string (got {type(timestamp_raw).__name__})"
        )
        raise ValueError(msg)
    utc_dt = iso_timestamp_to_utc_datetime(timestamp_raw)
    date_yyyymmdd, ms_of_day = utc_datetime_to_et_date_ms(utc_dt)

    # Build the kwargs that TradeRow expects.
    fields = {
        "ms_of_day": ms_of_day,
        "date_yyyymmdd": date_yyyymmdd,
        "sequence": obj.get("sequence"),
        "condition": obj.get("condition"),
        "size": obj.get("size"),
        "exchange": obj.get("exchange"),
        "price": obj.get("price"),
    }
    return TradeRow.model_validate(fields)


def quote_row_from_v3_dict(obj: dict[str, object]) -> QuoteRow:
    """Build a ``QuoteRow`` from one v3 REST quote response object.

    Working assumption (J3 from migration spec; verified by
    smoke test in 3.3.7.5): v3 quote response carries the same
    fields as v2 (bid_size, bid_exchange, bid, bid_condition,
    ask_size, ask_exchange, ask, ask_condition) plus a
    timestamp ISO string. Strike / right / symbol / expiration
    are redundant and dropped via extra="ignore" (J2).
    """
    fields: dict[str, object] = {}
    for k in ("bid", "ask", "bid_size", "ask_size"):
        if k in obj:
            fields[k] = obj[k]
    # ms_of_day required by QuoteRow validation; derive from
    # timestamp if present (v3 REST), else trust caller-supplied
    # value (rare back-compat path).
    if "timestamp" in obj:
        timestamp_raw = obj["timestamp"]
        if not isinstance(timestamp_raw, str):
            msg = (
                "quote_row_from_v3_dict: 'timestamp' must be string "
                f"(got {type(timestamp_raw).__name__})"
            )
            raise ValueError(msg)
        utc_dt = iso_timestamp_to_utc_datetime(timestamp_raw)
        _, ms_of_day = utc_datetime_to_et_date_ms(utc_dt)
        fields["ms_of_day"] = ms_of_day
    elif "ms_of_day" in obj:
        fields["ms_of_day"] = obj["ms_of_day"]
    return QuoteRow.model_validate(fields)


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


def classify_fill_side(price: Decimal, bid: Decimal, ask: Decimal) -> FillSide:
    """Classify a trade's aggression from its price against the NBBO.

    Phase 3.5.5 (Track A1): a trade executed at-or-above the ask is an
    aggressive buy; at-or-below the bid an aggressive sell; the gradations
    in between place it on the bid- or ask-half of the spread. This is the
    primary "unusual aggression" signal feeding the UOA score.

    Returns ``"unknown"`` when the quote is missing or crossed (bid > ask)
    or non-positive — the side genuinely cannot be inferred.
    """
    if bid <= 0 or ask <= 0 or bid > ask:
        return "unknown"
    if price >= ask:
        return "above_ask"
    if price <= bid:
        return "below_bid"
    midpoint = (bid + ask) / 2
    if price > midpoint:
        return "at_ask"
    if price < midpoint:
        return "at_bid"
    return "midpoint"


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
        fill_side=classify_fill_side(trade_row.price, bid, ask),
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
