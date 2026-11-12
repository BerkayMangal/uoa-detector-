"""Retrospective one-contract NBBO markouts, never claims of executable fills.

The caller supplies already downloaded trade/quote events and establishes that
the searched windows are complete. Quotes attached to later-retrieved trades
can price a historical diagnostic; they do not establish that our system saw
the quote then, or that a broker would have filled an order at it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import CalendarSettings

_ET = ZoneInfo("America/New_York")
_SLIPPAGE = Decimal("0.01")
_FEE = Decimal("0.65")
_MULTIPLIER = Decimal(100)
_ZERO = Decimal(0)
_SYMBOL = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})")
_HORIZONS = (15, 60)


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _regular_session(stamp: datetime, session: date, calendar: CalendarSettings) -> bool:
    local = stamp.astimezone(_ET)
    return (
        local.date() == session
        and is_session_day(session, calendar) is True
        and calendar.regular_open <= local.time() < close_time(session, calendar)
    )


@dataclass(frozen=True)
class QuoteObservation:
    event_id: str
    executed_at: datetime
    bid_time: datetime
    ask_time: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "executed_at": self.executed_at.isoformat(),
            "bid_time": self.bid_time.isoformat(),
            "ask_time": self.ask_time.isoformat(),
            "bid_age_seconds": (self.executed_at - self.bid_time).total_seconds(),
            "ask_age_seconds": (self.executed_at - self.ask_time).total_seconds(),
            "bid": str(self.bid),
            "ask": str(self.ask),
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "quoted_spread_usd": str((self.ask - self.bid) * _MULTIPLIER),
        }


@dataclass(frozen=True)
class IntradayMarkout:
    horizon_minutes: int
    status: str
    reason: str | None
    target_at: datetime | None = None
    exit_quote: QuoteObservation | None = None
    quote_gross_pnl_usd: Decimal | None = None
    modeled_gross_pnl_usd: Decimal | None = None
    modeled_net_pnl_usd: Decimal | None = None
    modeled_return_pct: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon_minutes": self.horizon_minutes,
            "status": self.status,
            "reason": self.reason,
            "target_at": self.target_at.isoformat() if self.target_at else None,
            "exit_quote": self.exit_quote.as_dict() if self.exit_quote else None,
            "exit_delay_seconds": (
                (self.exit_quote.executed_at - self.target_at).total_seconds()
                if self.exit_quote and self.target_at else None
            ),
            "modeled_exit_price": (
                str(max(_ZERO, self.exit_quote.bid - _SLIPPAGE))
                if self.exit_quote else None
            ),
            "quote_gross_pnl_usd": (
                str(self.quote_gross_pnl_usd) if self.quote_gross_pnl_usd is not None else None
            ),
            "modeled_gross_pnl_usd": (
                str(self.modeled_gross_pnl_usd) if self.modeled_gross_pnl_usd is not None else None
            ),
            "modeled_net_pnl_usd": (
                str(self.modeled_net_pnl_usd) if self.modeled_net_pnl_usd is not None else None
            ),
            "modeled_return_pct": (
                str(self.modeled_return_pct) if self.modeled_return_pct is not None else None
            ),
        }


@dataclass(frozen=True)
class IntradayResult:
    option_symbol: str
    session: date
    planned_entry_at: datetime | None
    received_at: datetime | None
    status: str
    reason: str | None
    coverage_complete: bool
    rejected_rows: dict[str, int]
    entry_quote: QuoteObservation | None
    markouts: tuple[IntradayMarkout, ...]

    def as_dict(self) -> dict[str, Any]:
        entry = self.entry_quote
        return {
            "option_symbol": self.option_symbol,
            "session": self.session.isoformat(),
            "planned_entry_at": self.planned_entry_at.isoformat() if self.planned_entry_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "status": self.status,
            "reason": self.reason,
            "coverage_complete": self.coverage_complete,
            "rejected_rows": self.rejected_rows,
            "entry_quote": entry.as_dict() if entry else None,
            "entry_delay_seconds": (
                (entry.executed_at - self.planned_entry_at).total_seconds()
                if entry and self.planned_entry_at else None
            ),
            "modeled_entry_price": str(entry.ask + _SLIPPAGE) if entry else None,
            "entry_same_quote_roundtrip_friction_usd": (
                str(((entry.ask + _SLIPPAGE) - max(_ZERO, entry.bid - _SLIPPAGE))
                    * _MULTIPLIER + 2 * _FEE) if entry else None
            ),
            "contracts": 1,
            "assumed_standard_multiplier": 100,
            "slippage_per_share_per_side": str(_SLIPPAGE),
            "fee_per_contract_per_side_usd": str(_FEE),
            "max_quote_age_seconds": 5,
            "max_wait_seconds": 120,
            "data_use": "retrospective_quote_markout",
            "executable_fill_proven": False,
            "markouts": [markout.as_dict() for markout in self.markouts],
            "limitations": [
                "Later-downloaded trade-attached NBBO is not a historical availability record.",
                "Displayed size and fresh NBBO do not prove a broker fill or available liquidity.",
                "Standard 100-share deliverable is assumed and unverified; regex cannot prove deliverables.",
                "15- and 60-minute markouts are alternative holds and must not be added together.",
            ],
        }


def _parse_quote(
    row: Mapping[str, object], *, option_symbol: str, expiry: date,
    session: date, received_at: datetime, calendar: CalendarSettings,
) -> tuple[QuoteObservation | None, str | None]:
    if row.get("option_chain_id") != option_symbol:
        return None, "contract_mismatch"
    if row.get("expiry") != expiry.isoformat():
        return None, "expiry_missing_or_mismatch"
    if row.get("canceled") is not False:
        return None, "canceled_or_cancel_state_missing"
    executed = _timestamp(row.get("executed_at"))
    bid_time = _timestamp(row.get("nbbo_bid_time"))
    ask_time = _timestamp(row.get("nbbo_ask_time"))
    if executed is None or bid_time is None or ask_time is None:
        return None, "timestamp_missing_or_invalid"
    if executed > received_at:
        return None, "event_after_retrieval"
    if not _regular_session(executed, session, calendar):
        return None, "outside_regular_session"
    if row.get("trading_hours") not in (None, "rth"):
        return None, "provider_not_regular_session"
    if bid_time > executed or ask_time > executed:
        return None, "future_quote"
    if any((executed - stamp).total_seconds() > 5 for stamp in (bid_time, ask_time)):
        return None, "stale_quote"
    if not all(_regular_session(stamp, session, calendar) for stamp in (bid_time, ask_time)):
        return None, "quote_outside_regular_session"
    bid, ask = _number(row.get("nbbo_bid")), _number(row.get("nbbo_ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None, "price_missing_or_invalid"
    if bid > ask:
        return None, "crossed_quote"
    bid_size, ask_size = _number(row.get("nbbo_bid_size")), _number(row.get("nbbo_ask_size"))
    if any(size is None or size < 1 or size != size.to_integral_value()
           for size in (bid_size, ask_size)):
        return None, "size_missing_or_invalid"
    assert bid_size is not None and ask_size is not None
    return QuoteObservation(
        str(row.get("id", "")), executed, bid_time, ask_time,
        bid, ask, int(bid_size), int(ask_size),
    ), None


def _first_quote(quotes: Sequence[QuoteObservation], target: datetime) -> QuoteObservation | None:
    deadline = target + timedelta(seconds=120)
    return next((quote for quote in quotes if target <= quote.executed_at <= deadline), None)


def evaluate_intraday_long_option(
    *, rows: Sequence[Mapping[str, object]], option_symbol: str, session: date,
    planned_entry_at: datetime, received_at: datetime, packet_date: date | None = None,
    multiplier: int = 100, coverage_complete: bool = False,
    calendar: CalendarSettings | None = None,
) -> IntradayResult:
    """Price fixed +15/+60 minute long-option diagnostics with conservative costs.

    ``coverage_complete=True`` is a caller attestation that pagination covered
    every searched entry/exit window; a full 500-row page alone is insufficient.
    The earliest qualifying event at/after each target is used, with a maximum
    120-second delay. Horizons start at the actual selected entry event. Missing
    observations remain UNKNOWN, never cash zero or a substituted contract.
    """
    entry_target, retrieved = _timestamp(planned_entry_at), _timestamp(received_at)
    rejected: Counter[str] = Counter()

    def unknown(reason: str) -> IntradayResult:
        return IntradayResult(
            option_symbol, session, entry_target, retrieved, "UNKNOWN", reason,
            coverage_complete, dict(rejected), None,
            tuple(IntradayMarkout(minutes, "UNKNOWN", reason) for minutes in _HORIZONS),
        )

    if entry_target is None or retrieved is None:
        return unknown("invalid_input_naive_or_invalid_timestamp")
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier != 100:
        return unknown("invalid_input_nonstandard_multiplier")
    match = _SYMBOL.fullmatch(option_symbol)
    if match is None:
        return unknown("invalid_input_nonstandard_contract_symbol")
    try:
        expiry = datetime.strptime(match.group(2), "%y%m%d").date()
    except ValueError:
        return unknown("invalid_input_contract_expiry")
    if expiry < session:
        return unknown("expired_contract")
    if packet_date is not None and packet_date != session:
        return unknown("packet_session_mismatch")
    if calendar is None:
        return unknown("missing_session_calendar")
    if calendar.timezone != "America/New_York":
        return unknown("invalid_input_session_timezone")
    if is_session_day(session, calendar) is None:
        return unknown("session_calendar_year_unknown")
    if not _regular_session(entry_target, session, calendar):
        return unknown("entry_target_outside_regular_session")
    if retrieved < entry_target:
        return unknown("retrieval_before_entry_target")
    if coverage_complete is not True:
        return unknown("incomplete_source_coverage")
    # Check native versions before validating individual quotes: a cancellation
    # or malformed correction must not disappear and leave an earlier usable
    # version of the same event behind. JSON signatures preserve distinctions
    # such as invalid boolean sizes versus numeric 1, unlike Python equality.
    native_groups: dict[str, list[Mapping[str, object]]] = {}
    ungrouped: list[Mapping[str, object]] = []
    for row in rows:
        event_id = row.get("id")
        if isinstance(event_id, str) and event_id:
            native_groups.setdefault(event_id, []).append(row)
        else:
            ungrouped.append(row)
    rows_to_parse = list(ungrouped)
    for native_group in native_groups.values():
        versions = {
            json.dumps(dict(row), sort_keys=True, default=str)
            for row in native_group
        }
        if len(versions) > 1:
            rejected["conflicting_duplicate_event"] += len(native_group)
        else:
            rows_to_parse.append(native_group[0])
    quotes: list[QuoteObservation] = []
    for row in rows_to_parse:
        quote, reason = _parse_quote(
            row, option_symbol=option_symbol, expiry=expiry,
            session=session, received_at=retrieved, calendar=calendar,
        )
        if quote is not None:
            quotes.append(quote)
        elif reason is not None:
            rejected[reason] += 1
    # Repeated identical vendor events add no observations. Conflicting versions
    # cannot be resolved by choosing the cheaper or lexicographically first row.
    grouped: dict[str, list[QuoteObservation]] = {}
    for quote in quotes:
        key = quote.event_id or quote.executed_at.isoformat()
        grouped.setdefault(key, []).append(quote)
    quotes = []
    for group in grouped.values():
        if len(set(group)) > 1:
            rejected["conflicting_duplicate_event"] += len(group)
        else:
            quotes.append(group[0])
    quotes.sort(key=lambda quote: (quote.executed_at, quote.event_id))
    entry = _first_quote(quotes, entry_target)
    if entry is None:
        return unknown("missing_eligible_entry_quote")
    entry_price = entry.ask + _SLIPPAGE
    entry_cost = entry_price * _MULTIPLIER + _FEE
    markouts: list[IntradayMarkout] = []
    for minutes in _HORIZONS:
        target = entry.executed_at + timedelta(minutes=minutes)
        if not _regular_session(target, session, calendar):
            markouts.append(IntradayMarkout(
                minutes, "UNKNOWN", "exit_target_outside_regular_session", target,
            ))
            continue
        exit_quote = _first_quote(quotes, target)
        if exit_quote is None:
            markouts.append(IntradayMarkout(
                minutes, "UNKNOWN", "missing_eligible_exit_quote", target,
            ))
            continue
        exit_price = max(_ZERO, exit_quote.bid - _SLIPPAGE)
        quote_gross = (exit_quote.bid - entry.ask) * _MULTIPLIER
        gross = (exit_price - entry_price) * _MULTIPLIER
        net = gross - 2 * _FEE
        markouts.append(IntradayMarkout(
            minutes, "EVALUATED", None, target, exit_quote,
            quote_gross, gross, net, net / entry_cost * 100,
        ))
    complete = all(markout.status == "EVALUATED" for markout in markouts)
    return IntradayResult(
        option_symbol, session, entry_target, retrieved,
        "EVALUATED" if complete else "PARTIAL",
        None if complete else "one_or_more_horizons_unknown",
        coverage_complete, dict(rejected), entry, tuple(markouts),
    )
