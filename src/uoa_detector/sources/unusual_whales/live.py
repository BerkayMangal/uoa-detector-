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


# Required fields on a UW flow object, as alias groups: the WS name first,
# then the REST flow-alerts name (Phase 3.9.4, contract §3.1). When every
# alias of a group is absent or unparseable the event is unmappable; the
# shared mapper logs the row's actual keys and returns None (Phase 3.7.2).
_REQUIRED_FLOW_FIELDS: tuple[str, ...] = (
    "ticker",
    "executed_at|created_at",
    "option_type|type",
    "strike",
    "expiry",
    "premium|total_premium",
    "price",
    "bid",
    "ask",
)


def map_uw_flow_event(
    event: dict[str, object],
    *,
    source_id: str,
    source_event_id_fallback: str,
) -> RawPrint | None:
    """Map a UW flow event dict (WS or REST shape) to a canonical RawPrint.

    Shared by ``UnusualWhalesLiveSource`` (WS) and
    ``UnusualWhalesRestFlowSource`` (REST) so the two feeds normalise flow
    identically. Pure function: no I/O, no mutation of ``event``.

    Field aliases (Phase 3.9.4, contract §3.1). The WS name keeps priority
    and the REST ``/api/option-trades/flow-alerts`` name is the fallback,
    so WS events map exactly as before:

      RawPrint field       WS name              flow-alert name
      timestamp            executed_at          created_at
      option_type          option_type          type (call/put only)
      premium_paid         premium              total_premium
      spot_price           spot_price           underlying_price
      implied_volatility   implied_volatility   iv_end
      is_iso               is_iso               has_sweep
      source_tags          alert_type           alert_rule

      - ``timestamp`` falls back to ``created_at`` (alert publication time,
        no look-ahead) also when ``executed_at`` is present but unparseable.
      - IV and OI accept a number or a numeric string; a negative or
        non-finite value is treated as absent (None).
      - ``has_multileg`` true adds the ``uw:multileg`` tag; the alert is kept.
      - A spot from either name suppresses the ``uw:no-spot`` tag.

    Fill side (never fabricated):
      - A ``side`` (WS) or ``side_classification`` key is looked up in the
        fixed table; an unrecognised value maps to ``"unknown"``.
        ``side_classification`` is directional (bullish/bearish/neutral),
        not a fill label, so it degrades to "unknown" (Phase 3.7 §4).
      - Otherwise, when both ``total_ask_side_prem`` and
        ``total_bid_side_prem`` parse: ask > bid is ``at_ask``, bid > ask is
        ``at_bid``, equal is ``unknown``. The split is UW's per-trade
        aggressor classification summed by premium, so it is fill data; it
        cannot tell ``above_ask`` from ``at_ask``.

    Required fields:
      - If a required field group (``_REQUIRED_FLOW_FIELDS``) is absent or
        unparseable, log an ERROR that names ``sorted(event.keys())`` and
        the alias groups, and return None. ``bid``/``ask`` have no alias. A
        shape mismatch is then a named one-line fix, never a silent drop and
        never a guessed value.

    :param source_id: value written to ``RawPrint.source_id``.
    :param source_event_id_fallback: ``source_event_id`` to use when the
        event carries neither ``id`` nor ``event_id``. The caller owns
        uniqueness of this string.
    :returns: a ``RawPrint``, or None if the event is unmappable or the
        contract already expired at execution time (DTE < 0).
    """
    try:
        ticker = str(event["ticker"]).upper()
        timestamp = _flow_timestamp(event)
        option_type_raw = str(_first_present(event, "option_type", "type")).lower()
        option_type = _coerce_option_type(option_type_raw)
        strike = Decimal(str(event["strike"]))
        expiry = _parse_expiry(event["expiry"])
        premium = Decimal(str(_first_present(event, "premium", "total_premium")))
        price = Decimal(str(event["price"]))
        bid = Decimal(str(event["bid"]))
        ask = Decimal(str(event["ask"]))
    except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
        _logger.error(
            "Dropping unmappable UW flow event (source_id=%s): %s; "
            "row keys=%s; required=%s",
            source_id,
            exc,
            sorted(event.keys()),
            list(_REQUIRED_FLOW_FIELDS),
        )
        return None

    # Optional fields
    open_interest = _optional_non_negative_int(event.get("open_interest"))
    implied_volatility = _optional_non_negative_float(event.get("implied_volatility"))
    if implied_volatility is None:
        implied_volatility = _optional_non_negative_float(event.get("iv_end"))
    is_iso_raw = event.get("is_iso")
    is_iso = bool(event.get("has_sweep", False) if is_iso_raw is None else is_iso_raw)
    exchange = str(event.get("exchange", "UNKNOWN"))

    # Fill side — a side label when the event carries one (WS 'side' or
    # 'side_classification'; unrecognised → "unknown"), else the flow-alert
    # ask/bid premium split. Never fabricated.
    fill_side: FillSide
    if "side" in event or "side_classification" in event:
        side_raw = str(
            event.get("side") or event.get("side_classification") or "",
        ).upper()
        fill_side = _UW_SIDE_TO_FILL_SIDE.get(side_raw, "unknown")
    else:
        fill_side = _fill_side_from_side_premium(
            event.get("total_ask_side_prem"), event.get("total_bid_side_prem"),
        )

    # DTE — drop if already expired at execution time.
    dte = (expiry - timestamp.date()).days
    if dte < 0:
        return None

    # Source event id
    event_id_raw = event.get("id") or event.get("event_id")
    source_event_id = (
        source_event_id_fallback
        if event_id_raw is None
        else f"uw-{event_id_raw}"
    )

    # Source tags — preserve UW's classification labels.
    tags: list[str] = []
    label = event.get("alert_type")
    if not (isinstance(label, str) and label):
        label = event.get("alert_rule")
    if isinstance(label, str) and label:
        tags.append(f"uw:{label.lower()}")
    if event.get("has_multileg") is True:
        tags.append("uw:multileg")
    # Spot: WS 'spot_price', else flow-alert 'underlying_price'. The WS feed
    # generally carries neither → 0 + 'uw:no-spot'.
    spot_price = _optional_decimal(event.get("spot_price"))
    if spot_price is None:
        spot_price = _optional_decimal(event.get("underlying_price"))
    if spot_price is None:
        spot_price = Decimal("0")
        tags.append("uw:no-spot")

    # premium_paid from UW: total notional dollars; option_price: per-share
    # mid-ish price. RawPrint expects both.
    return RawPrint(
        source_id=source_id,
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
        """Map a UW flow event dict to a canonical RawPrint.

        Thin wrapper over the shared ``map_uw_flow_event`` pure function
        (Phase 3.7.2). The WS source owns the fallback-id sequence so
        every emitted print stays globally unique even when UW omits an
        ``id`` on a malformed frame; the counter is bumped per mapped
        event and handed to the shared mapper as the candidate fallback.
        """
        self._fallback_seq += 1
        return map_uw_flow_event(
            event,
            source_id=self.source_id,
            source_event_id_fallback=f"uw-fallback-{self._fallback_seq}",
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


def _first_present(event: dict[str, object], *names: str) -> object:
    """Value of the first alias that is present and not None.

    :raises KeyError: naming the alias group (``"a|b"``) when every alias is
        absent or None.
    """
    for name in names:
        value = event.get(name)
        if value is not None:
            return value
    raise KeyError("|".join(names))


def _flow_timestamp(event: dict[str, object]) -> datetime:
    """Event time: ``executed_at`` (WS), else ``created_at`` (flow-alerts).

    ``created_at`` is also used when ``executed_at`` is present but does not
    parse. When neither parses the error propagates (event unmappable).
    """
    executed_at = event.get("executed_at")
    created_at = event.get("created_at")
    if executed_at is not None:
        try:
            return _parse_iso_utc(str(executed_at))
        except ValueError:
            if created_at is None:
                raise
    if created_at is None:
        raise KeyError("executed_at|created_at")
    return _parse_iso_utc(str(created_at))


def _optional_decimal(raw: object) -> Decimal | None:
    """A finite Decimal from a number or numeric string, else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = Decimal(str(raw).strip())
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _optional_non_negative_float(raw: object) -> float | None:
    """A finite float >= 0 from a number or numeric string, else None."""
    value = _optional_decimal(raw)
    if value is None or value < 0:
        return None
    return float(value)


def _optional_non_negative_int(raw: object) -> int | None:
    """An int >= 0 from a number or numeric string, else None."""
    value = _optional_decimal(raw)
    if value is None or value < 0:
        return None
    return int(value)


def _fill_side_from_side_premium(ask_prem: object, bid_prem: object) -> FillSide:
    """Aggressor fill side from a flow alert's ask/bid premium split.

    ask > bid → ``at_ask``; bid > ask → ``at_bid``; equal, or either side
    absent/unparseable → ``unknown``. ``above_ask``/``below_bid`` are never
    produced: a premium split cannot distinguish them (contract §3.1).
    """
    ask = _optional_decimal(ask_prem)
    bid = _optional_decimal(bid_prem)
    if ask is None or bid is None:
        return "unknown"
    if ask > bid:
        return "at_ask"
    if bid > ask:
        return "at_bid"
    return "unknown"
