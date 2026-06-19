"""UnusualWhalesFlowPollSource — REST-polling live flow source (Phase 4).

The UW realtime websocket surface (``UnusualWhalesLiveSource``) targets an
endpoint that is not validated against the live API. This source instead polls
the curated REST endpoint ``GET /api/stock/{ticker}/flow-alerts`` on a fixed
interval and emits new alerts as a ``RawFlowSource`` stream. A few seconds of
latency is irrelevant for a discretionary screener.

flow-alerts is UW's *unusual*-flow feed (each record already matched an
``alert_rule``), so it is far higher-signal than the raw last-50 tape: it is
100-deep per ticker, aggregates a cluster of trades, and carries the
underlying price, IV, OI, side-premium split (aggressor) and a sweep flag —
so each ``RawPrint`` rides with real spot, IV and OI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from uoa_detector.domain.events import FillSide
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.sources.unusual_whales.live import (
    _coerce_option_type,
    _parse_expiry,
    _parse_iso_utc,
)

if TYPE_CHECKING:
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

_logger = logging.getLogger(__name__)

# Cap the dedupe set so a long-running process never grows unbounded.
_SEEN_CAP = 50_000


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fill_side_from_prems(ask_prem: object, bid_prem: object) -> FillSide:
    # flow-alerts splits premium by aggressor side; the dominant side is the
    # aggressor. Ask-side dominant = buyers lifting the offer (urgent/long).
    ask = _decimal_or_none(ask_prem) or Decimal(0)
    bid = _decimal_or_none(bid_prem) or Decimal(0)
    if ask > bid:
        return "at_ask"
    if bid > ask:
        return "at_bid"
    return "unknown"


class UnusualWhalesFlowPollSource:
    """Poll UW ``flow-recent`` per ticker and emit new prints as a stream."""

    def __init__(
        self,
        client: UnusualWhalesClient,
        tickers: Iterable[str],
        *,
        poll_interval_s: float = 15.0,
        min_premium: Decimal = Decimal(0),
        backfill: timedelta = timedelta(minutes=10),
        source_id: str = "unusual_whales",
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.source_id = source_id
        self._client = client
        self._tickers = [t.upper() for t in tickers]
        self._poll_interval_s = poll_interval_s
        self._min_premium = min_premium
        self._backfill = backfill
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._seen: set[str] = set()
        self._closed = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        # On boot, only emit prints within the backfill window so we don't
        # flood the screener with the full last-50 backlog per ticker.
        cutoff = self._now() - self._backfill
        first_pass = True
        while not self._closed:
            for ticker in self._tickers:
                if self._closed:
                    break
                for record in await self._fetch(ticker):
                    print_ = self._to_raw_print(ticker, record, cutoff if first_pass else None)
                    if print_ is not None:
                        yield print_
            first_pass = False
            if len(self._seen) > _SEEN_CAP:
                self._seen.clear()
            await self._sleep(self._poll_interval_s)

    async def close(self) -> None:
        self._closed = True

    async def _fetch(self, ticker: str) -> list[dict[str, object]]:
        try:
            resp = await self._client.request_json(
                f"/api/stock/{ticker}/flow-alerts",
            )
        except Exception as exc:
            _logger.warning("UW flow-alerts fetch failed for %s: %s", ticker, exc)
            return []
        data = resp.get("data") if isinstance(resp, dict) else resp
        return data if isinstance(data, list) else []

    def _to_raw_print(
        self,
        ticker: str,
        record: dict[str, object],
        cutoff: datetime | None,
    ) -> RawPrint | None:
        # flow-alerts has no stable id; key on the option chain + alert time.
        chain = str(record.get("option_chain", ""))
        created = str(record.get("created_at", ""))
        event_id = f"uw-{ticker}-{chain}-{created}"
        if event_id in self._seen:
            return None

        try:
            timestamp = _parse_iso_utc(created)
            option_type = _coerce_option_type(str(record["type"]).lower())
            strike = Decimal(str(record["strike"]))
            expiry = _parse_expiry(record["expiry"])
            premium = Decimal(str(record["total_premium"]))
            option_price = Decimal(str(record["price"]))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            _logger.warning("Dropping malformed UW alert: %s", exc)
            return None

        # Mark seen only once it parses, so a transient bad record can recover.
        self._seen.add(event_id)

        if cutoff is not None and timestamp < cutoff:
            return None
        if premium < self._min_premium:
            return None

        dte = (expiry - timestamp.date()).days
        if dte < 0:
            return None

        # flow-alerts omits NBBO; fall back to the alert's print price.
        bid = _decimal_or_none(record.get("bid")) or option_price
        ask = _decimal_or_none(record.get("ask")) or option_price
        spot = _decimal_or_none(record.get("underlying_price")) or Decimal(0)

        iv_raw = _decimal_or_none(record.get("iv_end"))
        implied_volatility = float(iv_raw) if iv_raw is not None and iv_raw >= 0 else None
        oi_raw = record.get("open_interest")
        open_interest = int(oi_raw) if isinstance(oi_raw, (int, float)) else None

        fill_side = _fill_side_from_prems(
            record.get("total_ask_side_prem"), record.get("total_bid_side_prem"),
        )
        has_sweep = bool(record.get("has_sweep", False))

        tags: list[str] = []
        rule = record.get("alert_rule")
        if isinstance(rule, str) and rule:
            tags.append(f"uw:{rule}")
        if has_sweep:
            tags.append("uw:sweep")
        sector = record.get("sector")
        if isinstance(sector, str) and sector:
            tags.append(f"sector:{sector}")

        return RawPrint(
            source_id=self.source_id,
            source_event_id=event_id,
            timestamp=timestamp,
            ticker=ticker,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            dte=dte,
            spot_price=spot,
            premium_paid=premium,
            option_price=option_price,
            bid=bid,
            ask=ask,
            fill_side=fill_side,
            exchange="UNKNOWN",
            implied_volatility=implied_volatility,
            open_interest=open_interest,
            is_iso=has_sweep,
            source_tags=tuple(tags),
        )
