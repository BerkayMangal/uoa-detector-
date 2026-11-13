"""Bounded Theta Terminal quote capture and retrospective option economics.

Terminal owns authentication. This module sends no account credentials or
invented API-key headers. Native quote snapshots remain quote events; a snapshot
timestamp is never relabeled as a trade time or either side's last update time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import CalendarSettings

_ET = ZoneInfo("America/New_York")
_OCC = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})")
_PATH = "/v3/option/history/quote"
_CENT = Decimal("0.01")
_FEE = Decimal("0.65")
_HUNDRED = Decimal(100)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _native_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not _aware(stamp):
        # The locally documented v3 wire format is New York wall time. Reject
        # DST folds/gaps rather than choosing an undocumented interpretation.
        first, second = stamp.replace(tzinfo=_ET, fold=0), stamp.replace(tzinfo=_ET, fold=1)
        if first.utcoffset() != second.utcoffset():
            return None
        stamp = first
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class ThetaContract:
    option_symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    right: str

    @classmethod
    def from_occ(cls, symbol: str) -> ThetaContract:
        match = _OCC.fullmatch(symbol)
        if match is None:
            raise ValueError("invalid_standard_occ_symbol")
        try:
            expiry = datetime.strptime(match.group(2), "%y%m%d").date()
        except ValueError:
            raise ValueError("invalid_occ_expiry") from None
        strike = Decimal(match.group(4)) / 1000
        if strike <= 0:
            raise ValueError("invalid_occ_strike")
        return cls(symbol, match.group(1), expiry, strike,
                   "call" if match.group(3) == "C" else "put")

    def matches(self, identity: Mapping[str, object]) -> bool:
        expiry = str(identity.get("expiration", ""))
        if len(expiry) == 8 and expiry.isdigit():
            expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
        right = str(identity.get("right", "")).lower()
        right = {"c": "call", "p": "put"}.get(right, right)
        return (identity.get("symbol") == self.underlying
                and expiry == self.expiry.isoformat()
                and _decimal(identity.get("strike")) == self.strike
                and right == self.right)


@dataclass(frozen=True)
class ThetaQuote:
    option_symbol: str
    quote_at: datetime
    source_timestamp: str
    received_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "thetadata", "event_type": "quote",
            "option_symbol": self.option_symbol, "quote_at": self.quote_at.isoformat(),
            "source_timestamp": self.source_timestamp, "received_at": self.received_at.isoformat(),
            "bid": str(self.bid), "ask": str(self.ask),
            "bid_size": self.bid_size, "ask_size": self.ask_size,
            "quote_time_basis": "composite_quote_snapshot",
            "bid_update_age_seconds": None, "ask_update_age_seconds": None,
        }


@dataclass(frozen=True)
class ThetaQuoteBatch:
    option_symbol: str
    session: date
    window_start: datetime
    window_end: datetime
    received_at: datetime
    status: str
    reason: str | None
    quotes: tuple[ThetaQuote, ...] = ()
    rejected_rows: dict[str, int] = field(default_factory=dict)
    coverage_complete: bool = False
    http_status: int | None = None
    raw_sha256: str | None = None
    raw_payload: object = None
    requested_interval: str | None = None

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": "thetadata", "event_type": "quote",
            "option_symbol": self.option_symbol, "session": self.session.isoformat(),
            "window_start": self.window_start.isoformat(), "window_end": self.window_end.isoformat(),
            "received_at": self.received_at.isoformat(), "status": self.status, "reason": self.reason,
            "quotes": [quote.as_dict() for quote in self.quotes], "rejected_rows": self.rejected_rows,
            "coverage_complete": self.coverage_complete,
            "coverage_basis": "bounded_terminal_response_without_continuation_or_rejected_rows",
            "http_status": self.http_status, "raw_sha256": self.raw_sha256,
            "endpoint": _PATH, "requested_interval": self.requested_interval,
            "interval_verified_by_payload": False,
            "executable_fill_proven": False,
        }
        if include_raw:
            result["raw_payload"] = self.raw_payload
        return result


def terminal_clock(moment: datetime, *, round_up: bool) -> str:
    """Format an ET query time for Theta Terminal v3, which rejects microseconds (Phase 5.70.2).

    Quote timestamps are millisecond-resolution, so rounding the start up and the end down keeps exactly the
    quotes inside the original window and never admits one earlier than the start.
    """
    local = moment.astimezone(_ET)
    extra = local.microsecond % 1000
    if extra:
        local += timedelta(microseconds=1000 - extra if round_up else -extra)
    return local.time().isoformat(timespec="milliseconds")


def _window_failure(contract: ThetaContract, session: date,
                    start: datetime, end: datetime, received_at: datetime) -> str | None:
    if not all(_aware(value) for value in (start, end, received_at)):
        return "timezone_required"
    if not 0 < (end - start).total_seconds() <= 120:
        return "window_must_be_positive_and_at_most_120_seconds"
    if start.astimezone(_ET).date() != session or end.astimezone(_ET).date() != session:
        return "window_session_mismatch"
    if contract.expiry < session:
        return "expired_contract"
    if end > received_at:
        return "window_after_retrieval"
    return None


def _has_continuation(payload: object) -> bool:
    """A continuation on any native envelope makes this bounded batch partial."""
    pending: list[object] = [payload]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if any(node.get(key) not in (None, "", False) for key in
                   ("next_page", "next_page_url", "next_cursor", "has_more")):
                return True
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return False


def parse_theta_quote_payload(
    payload: object, *, option_symbol: str, session: date,
    window_start: datetime, window_end: datetime, received_at: datetime,
    max_rows: int = 50_000, http_status: int | None = None, raw_sha256: str | None = None,
    requested_interval: str | None = None,
) -> ThetaQuoteBatch:
    """Parse named v3 rows/wrappers; validate identity, bounds and source times.

    Supported evidence is the local v3 migration notes plus the existing native
    wrapper decoder. Positional v2 data, unidentifiable rows, continuation pages
    and malformed partial results fail closed. Raw payloads remain available to
    the caller for durable audit storage.
    """
    def result(status: str, reason: str | None, *, quotes: tuple[ThetaQuote, ...] = (),
               rejected: dict[str, int] | None = None, complete: bool = False) -> ThetaQuoteBatch:
        return ThetaQuoteBatch(option_symbol, session, window_start, window_end, received_at,
                               status, reason, quotes, rejected or {}, complete,
                               http_status, raw_sha256, payload, requested_interval)

    try:
        contract = ThetaContract.from_occ(option_symbol)
    except ValueError as exc:
        return result("invalid_request", str(exc))
    failure = _window_failure(contract, session, window_start, window_end, received_at)
    if failure:
        return result("invalid_request", failure)
    if isinstance(max_rows, bool) or max_rows < 1:
        return result("invalid_request", "invalid_row_limit")
    if requested_interval != "tick":
        return result("unsupported_payload", "missing_or_unsupported_request_interval")
    if isinstance(http_status, bool) or http_status != 200:
        return result("unsupported_payload", "missing_or_unsuccessful_http_status")
    if _has_continuation(payload):
        return result("unsupported_payload", "continuation_not_supported")
    if isinstance(payload, dict):
        if "contract" in payload and "data" in payload:
            raw_items: object = [payload]
        else:
            raw_items = payload.get("response", payload.get("data"))
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return result("unsupported_payload", "expected_v3_named_rows_or_wrappers")
    flattened: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            return result("unsupported_payload", "non_object_row")
        if "contract" in item:
            identity, data = item.get("contract"), item.get("data")
            if not isinstance(identity, dict) or not isinstance(data, list):
                return result("unsupported_payload", "invalid_contract_wrapper")
            if not contract.matches(identity):
                return result("unsupported_payload", "contract_mismatch")
            for row in data:
                if not isinstance(row, dict):
                    return result("unsupported_payload", "non_object_quote")
                flattened.append((identity, row))
        else:
            flattened.append((item, item))
        if len(flattened) > max_rows:
            return result("response_limit_exceeded", "row_limit_exceeded")
    rejected: Counter[str] = Counter()
    quotes: list[ThetaQuote] = []
    for identity, row in flattened:
        combined = dict(identity)
        combined.update({key: row[key] for key in ("symbol", "expiration", "strike", "right")
                         if key in row})
        if not contract.matches(combined):
            rejected["contract_missing_or_mismatch"] += 1
            continue
        raw_time = row.get("quote_timestamp", row.get("timestamp"))
        quote_at = _native_time(raw_time)
        if quote_at is None:
            rejected["quote_timestamp_missing_or_invalid"] += 1
            continue
        if ("quote_timestamp" in row and "timestamp" in row
                and _native_time(row["quote_timestamp"]) != _native_time(row["timestamp"])):
            rejected["conflicting_quote_timestamps"] += 1
            continue
        if not window_start <= quote_at <= window_end or quote_at.astimezone(_ET).date() != session:
            rejected["quote_outside_requested_window"] += 1
            continue
        if quote_at > received_at:
            rejected["quote_after_retrieval"] += 1
            continue
        bid, ask = _decimal(row.get("bid")), _decimal(row.get("ask"))
        bid_size, ask_size = _decimal(row.get("bid_size")), _decimal(row.get("ask_size"))
        if bid is None or ask is None or bid < 0 or ask < 0 or bid > ask:
            rejected["price_missing_invalid_or_crossed"] += 1
            continue
        if any(size is None or size < 0 or size != size.to_integral_value()
               for size in (bid_size, ask_size)):
            rejected["size_missing_or_invalid"] += 1
            continue
        assert bid_size is not None and ask_size is not None and isinstance(raw_time, str)
        quotes.append(ThetaQuote(option_symbol, quote_at, raw_time, received_at,
                                 bid, ask, int(bid_size), int(ask_size)))
    # A quote timestamp may not uniquely identify a tick. Conflicting values
    # without a sequence number cannot establish which snapshot came first.
    grouped: dict[datetime, list[ThetaQuote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.quote_at, []).append(quote)
    quotes = []
    for group in grouped.values():
        if len({(quote.bid, quote.ask, quote.bid_size, quote.ask_size) for quote in group}) > 1:
            rejected["ambiguous_same_timestamp_quotes"] += len(group)
        else:
            quotes.append(group[0])
    quotes.sort(key=lambda quote: quote.quote_at)
    if rejected:
        return result("partial" if quotes else "unsupported_payload", "rejected_native_rows",
                      quotes=tuple(quotes), rejected=dict(rejected))
    return result("ok" if quotes else "no_quotes", None if quotes else "empty_native_response",
                  quotes=tuple(quotes), complete=True)


class ThetaQuoteClient:
    """One exact-contract request at a time with no retries or credential headers."""

    def __init__(self, base_url: str = "http://127.0.0.1:25503", *,
                 timeout_seconds: float = 20, max_body_bytes: int = 8 * 1024 * 1024,
                 max_rows: int = 50_000, transport: httpx.AsyncBaseTransport | None = None) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("terminal_base_url_must_be_an_origin_without_credentials")
        if not 0 < timeout_seconds <= 60 or not 1 <= max_body_bytes <= 8 * 1024 * 1024:
            raise ValueError("invalid_terminal_request_limits")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 50_000:
            raise ValueError("invalid_terminal_row_limit")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_bytes = max_body_bytes
        self._max_rows = max_rows
        self._transport = transport
        self._semaphore = asyncio.Semaphore(1)

    async def fetch_window(self, *, option_symbol: str, session: date,
                           window_start: datetime, window_end: datetime) -> ThetaQuoteBatch:
        received_at = datetime.now(UTC)

        def failure(status: str, reason: str, code: int | None = None) -> ThetaQuoteBatch:
            return ThetaQuoteBatch(option_symbol, session, window_start, window_end,
                                   datetime.now(UTC), status, reason, http_status=code)

        try:
            contract = ThetaContract.from_occ(option_symbol)
        except ValueError as exc:
            return failure("invalid_request", str(exc))
        invalid = _window_failure(contract, session, window_start, window_end, received_at)
        if invalid:
            return failure("invalid_request", invalid)
        params = {
            "symbol": contract.underlying, "expiration": contract.expiry.strftime("%Y%m%d"),
            "strike": format(contract.strike, "f"), "right": contract.right,
            "date": session.strftime("%Y%m%d"), "interval": "tick",
            "start_time": terminal_clock(window_start, round_up=True),
            "end_time": terminal_clock(window_end, round_up=False),
            "format": "json",
        }
        try:
            async with (
                self._semaphore, asyncio.timeout(self._timeout),
                httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                  follow_redirects=False) as client,
                client.stream("GET", self._base_url + _PATH, params=params) as response,
            ):
                code = response.status_code
                if code in (401, 403):
                    return failure("authentication_failed", "terminal_rejected_access", code)
                if code == 472:
                    return ThetaQuoteBatch(option_symbol, session, window_start, window_end,
                                           datetime.now(UTC), "no_quotes", "native_no_data",
                                           coverage_complete=True, http_status=code, requested_interval="tick")
                if code == 429:
                    return failure("rate_limited", "terminal_rate_limit", code)
                if code != 200:
                    return failure("terminal_error", "unexpected_http_status", code)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        return failure("response_limit_exceeded", "body_limit_exceeded", code)
                    chunks.append(chunk)
            body = b"".join(chunks)
            received_at = datetime.now(UTC)
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                return failure("unsupported_payload", "response_is_not_json", code)
            return parse_theta_quote_payload(
                payload, option_symbol=option_symbol, session=session,
                window_start=window_start, window_end=window_end, received_at=received_at,
                max_rows=self._max_rows, http_status=code, raw_sha256=hashlib.sha256(body).hexdigest(),
                requested_interval="tick",
            )
        except (httpx.RequestError, TimeoutError):
            return failure("terminal_unreachable", "connection_or_timeout_failure")


def _regular(stamp: datetime, session: date, calendar: CalendarSettings) -> bool:
    local = stamp.astimezone(_ET)
    return (calendar.timezone == "America/New_York"
            and is_session_day(session, calendar) is True
            and local.date() == session
            and calendar.regular_open <= local.time() < close_time(session, calendar))


def select_first_theta_quote(batch: ThetaQuoteBatch, *, target_at: datetime,
                             calendar: CalendarSettings) -> ThetaQuote | None:
    """First returned quote snapshot in a complete 120-second window, never a trade."""
    if (not _aware(target_at) or not batch.coverage_complete or batch.status != "ok"
            or batch.requested_interval != "tick" or batch.http_status != 200
            or batch.window_start > target_at
            or batch.window_end < target_at + timedelta(seconds=120)
            or not _regular(target_at, batch.session, calendar)):
        return None
    for quote in sorted(batch.quotes, key=lambda item: item.quote_at):
        if (quote.option_symbol == batch.option_symbol and target_at <= quote.quote_at
                <= target_at + timedelta(seconds=120) and _regular(quote.quote_at, batch.session, calendar)
                and quote.bid > 0 and quote.ask > 0 and quote.bid <= quote.ask
                and quote.bid_size >= 1 and quote.ask_size >= 1):
            return quote
    return None


def evaluate_theta_long_option(
    *, entry_batch: ThetaQuoteBatch, exit_batches: Mapping[int, ThetaQuoteBatch],
    calendar: CalendarSettings,
) -> dict[str, Any]:
    """One-contract ask-in/bid-out diagnostics, $0.01/share and $0.65/side costs."""
    entry = select_first_theta_quote(entry_batch, target_at=entry_batch.window_start, calendar=calendar)
    result: dict[str, Any] = {
        "source": "thetadata", "event_type": "quote", "data_use": "retrospective_quote_markout",
        "option_symbol": entry_batch.option_symbol, "session": entry_batch.session.isoformat(),
        "status": "UNKNOWN", "entry_quote": entry.as_dict() if entry else None,
        "reason": None if entry else "entry_" + (entry_batch.reason or "eligible_quote_unavailable"),
        "planned_entry_at": entry_batch.window_start.isoformat(),
        "quote_time_basis": "composite_quote_snapshot", "side_update_age_seconds": None,
        "standard_deliverable": "100_shares_assumed_unverified", "contracts": 1,
        "slippage_per_share_per_side": "0.01", "fee_per_contract_per_side_usd": "0.65",
        "executable_fill_proven": False, "markouts": [],
        "limitations": [
            "Later-downloaded native quote snapshots do not prove historical system availability or fills.",
            "Composite quote time does not establish individual bid/ask update ages.",
            "The fetch client requests tick data; request provenance and current endpoint behavior require a live check.",
            "15- and 60-minute holds are alternatives, not additive portfolio returns.",
        ],
    }
    for minutes in (15, 60):
        markout: dict[str, Any] = {"horizon_minutes": minutes, "status": "UNKNOWN",
                                   "reason": "entry_unavailable", "modeled_net_pnl_usd": None}
        if entry is not None:
            target = entry.quote_at + timedelta(minutes=minutes)
            markout["target_at"] = target.isoformat()
            batch = exit_batches.get(minutes)
            if not _regular(target, entry_batch.session, calendar):
                markout["reason"] = "exit_target_outside_regular_session"
            elif batch is None:
                markout["reason"] = "exit_batch_missing"
            elif (batch.option_symbol != entry_batch.option_symbol or batch.session != entry_batch.session):
                markout["reason"] = "exit_batch_contract_or_session_mismatch"
            else:
                exit_quote = select_first_theta_quote(batch, target_at=target, calendar=calendar)
                if exit_quote is None:
                    markout["reason"] = "exit_" + (batch.reason or "eligible_quote_unavailable")
                else:
                    buy = entry.ask + _CENT
                    sell = max(Decimal(0), exit_quote.bid - _CENT)
                    gross = (sell - buy) * _HUNDRED
                    net = gross - 2 * _FEE
                    markout.update({
                        "status": "EVALUATED", "reason": None, "exit_quote": exit_quote.as_dict(),
                        "exit_delay_seconds": (exit_quote.quote_at - target).total_seconds(),
                        "modeled_exit_price": str(sell),
                        "quote_gross_pnl_usd": str((exit_quote.bid - entry.ask) * _HUNDRED),
                        "modeled_gross_pnl_usd": str(gross), "modeled_net_pnl_usd": str(net),
                        "modeled_return_pct": str(net / (buy * _HUNDRED + _FEE) * 100),
                    })
        result["markouts"].append(markout)
    if entry is not None:
        complete = all(markout["status"] == "EVALUATED" for markout in result["markouts"])
        result.update({
            "status": "EVALUATED" if complete else "PARTIAL",
            "reason": None if complete else "one_or_more_horizons_unknown",
            "modeled_entry_price": str(entry.ask + _CENT),
            "entry_delay_seconds": (entry.quote_at - entry_batch.window_start).total_seconds(),
            "entry_same_quote_roundtrip_friction_usd": str(
                ((entry.ask + _CENT) - max(Decimal(0), entry.bid - _CENT)) * _HUNDRED + 2 * _FEE),
        })
    return result
