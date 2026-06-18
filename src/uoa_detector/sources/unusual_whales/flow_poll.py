"""UnusualWhalesFlowPollSource — REST-polling live flow source (Phase 4).

The UW realtime websocket surface (``UnusualWhalesLiveSource``) targets an
endpoint that is not validated against the live API. This source instead polls
the proven REST endpoint ``GET /api/stock/{ticker}/flow-recent`` on a fixed
interval and emits new prints as a ``RawFlowSource`` stream. A few seconds of
latency is irrelevant for a discretionary screener, and REST is the path the
integration smoke test exercises end-to-end.

The flow-recent records carry the flow print *plus* greeks, IV, OI, the
underlying price and UW's classification ``tags`` — so each ``RawPrint`` is
richer than the websocket shape (real spot, IV and OI ride along).
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

# UW's ``tags`` carry the aggressor side; map to the canonical FillSide.
_TAG_TO_FILL_SIDE: dict[str, FillSide] = {
    "ask_side": "at_ask",
    "above_ask": "above_ask",
    "bid_side": "at_bid",
    "below_bid": "below_bid",
    "mid": "midpoint",
    "no_side": "unknown",
}

# Cap the dedupe set so a long-running process never grows unbounded.
_SEEN_CAP = 50_000


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fill_side_from_tags(tags: Iterable[object]) -> FillSide:
    lowered = {str(t).lower() for t in tags}
    for tag, side in _TAG_TO_FILL_SIDE.items():
        if tag in lowered:
            return side
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
                f"/api/stock/{ticker}/flow-recent",
            )
        except Exception as exc:
            _logger.warning("UW flow-recent fetch failed for %s: %s", ticker, exc)
            return []
        data = resp.get("data") if isinstance(resp, dict) else resp
        return data if isinstance(data, list) else []

    def _to_raw_print(
        self,
        ticker: str,
        record: dict[str, object],
        cutoff: datetime | None,
    ) -> RawPrint | None:
        raw_id = record.get("id") or record.get("flow_alert_id")
        if raw_id is None:
            return None
        event_id = f"uw-{raw_id}"
        if event_id in self._seen:
            return None

        try:
            timestamp = _parse_iso_utc(str(record["executed_at"]))
            option_type = _coerce_option_type(str(record["option_type"]).lower())
            strike = Decimal(str(record["strike"]))
            expiry = _parse_expiry(record["expiry"])
            premium = Decimal(str(record["premium"]))
            option_price = Decimal(str(record["price"]))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            _logger.warning("Dropping malformed UW flow record: %s", exc)
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

        bid = _decimal_or_none(record.get("nbbo_bid")) or Decimal(0)
        ask = _decimal_or_none(record.get("nbbo_ask")) or Decimal(0)
        spot = _decimal_or_none(record.get("underlying_price")) or Decimal(0)

        iv_raw = _decimal_or_none(record.get("implied_volatility"))
        implied_volatility = float(iv_raw) if iv_raw is not None and iv_raw >= 0 else None
        oi_raw = record.get("open_interest")
        open_interest = int(oi_raw) if isinstance(oi_raw, (int, float)) else None

        raw_tags = record.get("tags")
        tags = tuple(str(t) for t in raw_tags) if isinstance(raw_tags, list) else ()
        fill_side = _fill_side_from_tags(tags)

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
            exchange=str(record.get("exchange", "UNKNOWN")),
            implied_volatility=implied_volatility,
            open_interest=open_interest,
            is_iso=False,
            source_tags=tags,
        )
