"""Unusual Whales live WebSocket source.

Phase 3.3.3.3: implements ``RawFlowSource`` Protocol on top of UW's
flow alerts WebSocket stream. UW emits pre-classified flow events
(each event contains contract identity, bid/ask, fill side, premium,
and UW's own alert classification). Compared to ThetaData's two-stream
(trade + quote) feed, UW is simpler: one event per print.

Key design difference from ThetaDataLiveSource:
  - **No per-contract bid/ask cache.** UW supplies bid/ask in every
    event. The cache that lived in ThetaDataLiveSource exists because
    OPRA streams trades and quotes separately; UW pre-aligns them
    server-side.
  - **UW labels preserved in ``RawPrint.source_tags``.** Phase 3.4's
    M34 (Multi-Source Reconciliation) runs its own classifier and
    does NOT consult UW's ``alert_type`` for scoring decisions. The
    label is kept so post-hoc audit can compare classifications.

Reconnect logic uses ``live_reconnect_*`` profile fields, identical
to ThetaData. Authentication is via the ``Authorization: Bearer``
header on the WS connect (matches the HTTP client pattern from
3.3.3.2).

Tests use a fake ``_FakeWebSocket`` and a fake connect factory —
no real network. Smoke integration tests (3.3.3.6) hit the real UW
endpoint and skip without UNUSUAL_WHALES_API_KEY.

decision (RawFlowSource Protocol via duck-type):
  Same shape as ThetaDataLiveSource: structural Protocol,
  ``source_id`` attr, ``stream() -> AsyncIterator[RawPrint]``,
  ``close()``. No inheritance from ThetaData's class — they are
  parallel implementations of the same Protocol.

decision (UW labels into ``source_tags``, prefixed with 'uw:'):
  ``RawPrint.source_tags`` is a tuple[str, ...] designed for this
  use. We prefix with 'uw:' so downstream code can filter by
  source while the original label is preserved (e.g. 'uw:sweep',
  'uw:block', 'uw:split', 'uw:repeated'). M34 ignores these tags
  by design — the comment in M34's docstring will reference this
  decision.

decision (fill_side mapping is a fixed table, not configurable):
  UW's documented side strings map deterministically to
  FillSide. A configurable mapping would be over-engineering — if
  UW changes their labels, that's a code change with a unit-test
  pin to update.

decision (skip events with unparseable required fields, log a
warning, do NOT crash):
  Live streams must not crash on a single bad event. Per-frame
  defensive parsing: malformed → log + skip; valid → emit. Pinned
  by ``test_malformed_event_dropped_stream_continues``.

decision (no spot_price snapshot from a separate endpoint):
  UW does not include spot_price in flow events. Spot is fetched
  via a sibling provider (Phase 3.4 wiring) or from ThetaData's
  stream when running multi-source. For the live source in
  isolation, we fall back to ``Decimal(0)`` and tag the print with
  ``'uw:no-spot'`` so downstream can filter or merge with another
  feed. This is intentionally lossy; the multi-source case
  (which is what M34 is for) supplies spot from elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from uoa_detector.domain.events import FillSide, OptionType
from uoa_detector.domain.raw_print import RawPrint

if TYPE_CHECKING:
    from datetime import date

    from pydantic import SecretStr

    from uoa_detector.calibration.profile import UnusualWhalesSettings


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket abstract surface
# ---------------------------------------------------------------------------


class _WebSocketLike(Protocol):
    """Minimal WS surface the live source uses.

    websockets.WebSocketClientProtocol satisfies this structurally.
    """

    async def recv(self) -> str | bytes: ...
    async def send(self, message: str | bytes) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnusualWhalesLiveError(RuntimeError):
    """Base for live-source-specific errors."""


class ReconnectExhaustedError(UnusualWhalesLiveError):
    """All reconnect attempts failed; the operator must intervene."""


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowSubscription:
    """One ticker-level subscription request.

    UW's flow stream is keyed by ticker (not by individual contract,
    unlike ThetaData). The subscribe message lists tickers; UW emits
    flow events for any contract on those tickers.
    """

    ticker: str


# ---------------------------------------------------------------------------
# Live source
# ---------------------------------------------------------------------------


# Type alias for the connection factory.
ConnectFactory = Callable[
    [str, dict[str, str]],
    "Awaitable[_WebSocketLike]",
]


# UW side label → canonical FillSide. Frozen at import time.
_UW_SIDE_TO_FILL_SIDE: dict[str, FillSide] = {
    "ASK": "at_ask",
    "AT_ASK": "at_ask",
    "BID": "at_bid",
    "AT_BID": "at_bid",
    "MID": "midpoint",
    "MIDPOINT": "midpoint",
    "ABOVE_ASK": "above_ask",
    "BELOW_BID": "below_bid",
}


class UnusualWhalesLiveSource:
    """``RawFlowSource`` implementation backed by the UW flow WebSocket.

    Lifecycle:
      live = UnusualWhalesLiveSource(
          api_key=SecretStr(...),
          settings=UnusualWhalesSettings(...),
          subscriptions=[FlowSubscription(ticker='AAPL'), ...],
          ws_url='wss://api.unusualwhales.com/v1/ws',
      )
      async for rp in live.stream():
          handle(rp)
      await live.close()
    """

    source_id: str = "unusual_whales"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        settings: UnusualWhalesSettings,
        subscriptions: Iterable[FlowSubscription],
        ws_url: str = "wss://api.unusualwhales.com/v1/ws",
        connect_factory: ConnectFactory | None = None,
        source_id: str = "unusual_whales",
    ) -> None:
        self._api_key = api_key
        self._settings = settings
        self._subscriptions = list(subscriptions)
        self._ws_url = ws_url
        self._connect_factory = connect_factory or _default_connect_factory
        self.source_id = source_id

        self._closed = False
        self._current_ws: _WebSocketLike | None = None
        # Per-source-id event sequence for source_event_id uniqueness in
        # the unlikely case UW omits an id field on a malformed event.
        self._fallback_seq = 0

    # -- public API -------------------------------------------------------

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield ``RawPrint`` events from the live WS feed.

        Reconnect logic identical to ThetaDataLiveSource: on
        disconnect, exponential backoff up to ``max_attempts``, then
        ``ReconnectExhaustedError``. Successful frame resets attempt.
        """
        attempt = 0
        max_attempts = self._settings.live_reconnect_max_attempts
        while not self._closed:
            try:
                async for rp in self._stream_once():
                    if self._closed:
                        return
                    yield rp
                    attempt = 0
                if self._closed:
                    return
            except (TimeoutError, ConnectionError, OSError) as exc:
                _logger.warning(
                    "UnusualWhales WS error (attempt %d): %s",
                    attempt + 1, exc,
                )
            except UnusualWhalesLiveError:
                raise
            attempt += 1
            if attempt > max_attempts:
                msg = (
                    f"reconnect exhausted after {max_attempts} attempts; "
                    "operator must intervene"
                )
                raise ReconnectExhaustedError(msg)
            backoff = self._compute_backoff(attempt)
            _logger.info(
                "Reconnecting in %.2fs (attempt %d/%d)",
                backoff, attempt, max_attempts,
            )
            await asyncio.sleep(backoff)

    async def close(self) -> None:
        """Release the active WebSocket connection. Idempotent."""
        self._closed = True
        ws = self._current_ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
            self._current_ws = None

    # -- internals --------------------------------------------------------

    def _compute_backoff(self, attempt: int) -> float:
        """Exponential backoff capped at max_backoff_s."""
        initial = self._settings.live_reconnect_initial_backoff_s
        cap = self._settings.live_reconnect_max_backoff_s
        return float(min(initial * (2 ** (attempt - 1)), cap))

    def _auth_headers(self) -> dict[str, str]:
        """Bearer auth on the WS handshake."""
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
        }

    def _build_subscribe_message(self) -> str:
        """Build the WS subscribe payload.

        UW's documented WS subscribe shape:
          {"channel": "flow_alerts", "tickers": ["AAPL", "MSFT", ...]}

        Schema-evolution risk centralised here; one-place fix when
        the docs change.
        """
        return json.dumps({
            "channel": "flow_alerts",
            "tickers": [s.ticker.upper() for s in self._subscriptions],
        })

    async def _stream_once(self) -> AsyncIterator[RawPrint]:
        """One connection lifecycle."""
        ws = await self._connect_factory(self._ws_url, self._auth_headers())
        self._current_ws = ws
        try:
            await ws.send(self._build_subscribe_message())
            while not self._closed:
                raw = await ws.recv()
                rp = self._handle_frame(raw)
                if rp is not None:
                    yield rp
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            self._current_ws = None

    def _handle_frame(self, raw: str | bytes) -> RawPrint | None:
        """Parse one WS frame; return RawPrint if it's a usable event."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            _logger.warning("Dropping malformed JSON frame")
            return None
        if not isinstance(frame, dict):
            return None

        ftype = frame.get("type") or frame.get("channel")
        if ftype in ("ping", "pong", "heartbeat", "status", "subscribed"):
            return None
        if ftype not in ("flow_alert", "flow_alerts", "trade", "alert"):
            _logger.debug("Ignoring unknown frame type: %s", ftype)
            return None

        # The actual event payload is nested in 'data' for most UW shapes
        data = frame.get("data") or frame
        if not isinstance(data, dict):
            return None
        return self._map_event_to_raw_print(data)

    def _map_event_to_raw_print(
        self, event: dict[str, object],
    ) -> RawPrint | None:
        """Map a UW flow event dict to a canonical RawPrint."""
        try:
            ticker = str(event["ticker"]).upper()
            executed_at_raw = str(event["executed_at"])
            timestamp = _parse_iso_utc(executed_at_raw)
            option_type_raw = str(event["option_type"]).lower()
            option_type = _coerce_option_type(option_type_raw)
            strike = Decimal(str(event["strike"]))
            expiry = _parse_expiry(event["expiry"])
            premium = Decimal(str(event["premium"]))
            price = Decimal(str(event["price"]))
            bid = Decimal(str(event["bid"]))
            ask = Decimal(str(event["ask"]))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            _logger.warning("Dropping malformed UW event: %s", exc)
            return None

        # Optional fields
        oi_raw = event.get("open_interest")
        open_interest = (
            int(oi_raw) if isinstance(oi_raw, (int, float)) else None
        )
        iv_raw = event.get("implied_volatility")
        implied_volatility = (
            float(iv_raw) if isinstance(iv_raw, (int, float)) else None
        )
        is_iso = bool(event.get("is_iso", False))
        exchange = str(event.get("exchange", "UNKNOWN"))

        # Side mapping
        side_raw = str(event.get("side", "")).upper()
        fill_side: FillSide = _UW_SIDE_TO_FILL_SIDE.get(side_raw, "unknown")

        # DTE — drop if expired
        timestamp_date = timestamp.date()
        dte = (expiry - timestamp_date).days
        if dte < 0:
            return None

        # Source event id
        event_id_raw = event.get("id") or event.get("event_id")
        if event_id_raw is None:
            self._fallback_seq += 1
            source_event_id = f"uw-fallback-{self._fallback_seq}"
        else:
            source_event_id = f"uw-{event_id_raw}"

        # Source tags — preserve UW's classification labels
        tags: list[str] = []
        alert_type = event.get("alert_type")
        if isinstance(alert_type, str) and alert_type:
            tags.append(f"uw:{alert_type.lower()}")
        # Spot price not provided by UW flow stream
        spot_raw = event.get("spot_price")
        if isinstance(spot_raw, (int, float, str)):
            try:
                spot_price = Decimal(str(spot_raw))
            except ArithmeticError:
                spot_price = Decimal("0")
                tags.append("uw:no-spot")
        else:
            spot_price = Decimal("0")
            tags.append("uw:no-spot")

        # premium_paid from UW: total notional dollars; option_price: per-share
        # mid-ish price. RawPrint expects both.
        return RawPrint(
            source_id=self.source_id,
            source_event_id=source_event_id,
            timestamp=timestamp,
            ticker=ticker,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            dte=dte,
            spot_price=spot_price,
            premium_paid=premium,
            option_price=price,
            bid=bid,
            ask=ask,
            fill_side=fill_side,
            exchange=exchange,
            implied_volatility=implied_volatility,
            open_interest=open_interest,
            is_iso=is_iso,
            source_tags=tuple(tags),
        )


# ---------------------------------------------------------------------------
# Default connection factory (production)
# ---------------------------------------------------------------------------


async def _default_connect_factory(
    url: str, headers: dict[str, str],
) -> _WebSocketLike:
    """Wrap ``websockets.connect`` with the auth headers."""
    import websockets

    additional = [(k, v) for k, v in headers.items()]
    ws = await websockets.connect(url, additional_headers=additional)
    return ws  # structural protocol match


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO8601 UTC timestamp; raise ValueError if not parseable."""
    # Accept trailing 'Z' as well as offset form
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        msg = f"timestamp not tz-aware: {raw}"
        raise ValueError(msg)
    return parsed


def _coerce_option_type(raw: str) -> OptionType:
    """Map UW's option_type string to canonical 'call' or 'put'."""
    if raw in ("call", "c"):
        return "call"
    if raw in ("put", "p"):
        return "put"
    msg = f"unknown option_type: {raw}"
    raise ValueError(msg)


def _parse_expiry(raw: object) -> date:
    """Parse expiry from UW. Accepts ISO date strings or YYYY-MM-DD."""
    from datetime import date as date_cls

    if isinstance(raw, date_cls):
        return raw
    if not isinstance(raw, str):
        msg = f"expiry not parseable: {raw!r}"
        raise TypeError(msg)
    # UW typically sends 'YYYY-MM-DD' or full ISO with time
    if "T" in raw:
        return _parse_iso_utc(raw if raw.endswith("Z") else raw + "Z").date()
    return date_cls.fromisoformat(raw)
