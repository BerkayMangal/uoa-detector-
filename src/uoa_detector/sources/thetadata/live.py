"""ThetaData live WebSocket source.

Phase 3.3.2.5: implements the ``RawFlowSource`` Protocol on top of
the ThetaData live WebSocket stream. Subscribes to OPRA trade
events for a configured contract universe, decodes incoming JSON
frames, aligns each trade to the most recent bid/ask quote (held
per-contract), maps to ``RawPrint``, and yields them through
``stream()``.

Phase 3.3.7.4 (v2 → v3): WebSocket URL path changed from ``/v2/ws``
to ``/v1/events`` per docs.thetadata.us/Streaming/Getting-Started.html.
Port 25520 unchanged. Streaming has independent versioning (v1)
from the REST API (which jumped v2 → v3); the message format
itself is unchanged — contract objects still carry root + expiration
(YYYYMMDD int) + strike (1/10 cent int) + right ('C'/'P'), and trade
events still carry (date, ms_of_day) integer pair timestamps. Only
the URL path constant is updated; subscription payload + frame
parsing are byte-for-byte the same as Phase 3.3.2.5.

Reconnect logic uses the ``live_reconnect_*`` profile fields
(Phase 3.3.2.1):
  - max_attempts: cap before raising and surfacing to the operator
  - initial_backoff_s: first retry delay
  - max_backoff_s: ceiling for exponential backoff (2^n × initial)

Tests use a fake "WebSocket-like" class (``_FakeWebSocket``) that
yields scripted frames and lets us simulate disconnects + closes.
The ``ThetaDataLiveSource`` is parameterised by a connection
factory (``connect_factory``) so tests inject the fake; production
wires through ``websockets.connect`` via the default factory.

The smoke integration test (3.3.2.6) hits the real Theta Terminal
WS endpoint and is skipped without a real ``THETADATA_API_KEY``.

decision (RawFlowSource Protocol, not subclass):
  Phase 2 set the contract — duck-typed Protocol with ``source_id``
  attribute, ``stream() -> AsyncIterator[RawPrint]`` method, and
  ``close()``. Implementing the Protocol structurally avoids
  inheritance coupling and matches the existing
  SyntheticRawFlowSource style.

decision (per-contract bid/ask cache):
  ThetaData's WS feed interleaves trade and quote messages per
  contract subscription. We hold the latest (bid, ask) per
  contract in a dict keyed by (ticker, expiry, strike, right) and
  apply it to each subsequent trade. This is the live-stream
  analogue of historical's as-of merge.

decision (drop trades that arrive before any quote):
  Symmetric with the historical downloader's
  align_quotes_to_trades behaviour: a trade with no preceding
  quote can't be priced (no bid/ask). Drop rather than emit a
  RawPrint with bid=ask=0; downstream stages assume bid<=ask.

decision (reconnect: caller-supplied factory):
  ``connect_factory(url, headers)`` returns an awaitable yielding
  the WS-like object. The default factory uses
  ``websockets.connect``; tests pass a callable that returns a
  ``_FakeWebSocket`` whose iterator behaviour is scripted. Keeps
  the WS API surface narrow (recv() / aiter / close()) so future
  swaps to a different WS library are one-line changes.

decision (backoff resets on successful frame):
  After a disconnect-and-reconnect, the backoff counter resets
  the moment any frame is successfully received. A connection
  that flaps once per hour shouldn't accumulate backoff toward
  60s. Pinned by tests that assert backoff resets after a
  successful recv.

decision (close is idempotent and signals streamer to stop):
  Single ``_closed`` flag checked in the recv loop. close() sets
  the flag and best-effort closes the live websocket; in-flight
  recv() raises naturally. Pinned by
  test_close_stops_streaming.

decision (no built-in subscription management persistence):
  The subscription set is constructor-supplied and does not
  change at runtime. A future Phase 3.4 enhancement may add
  dynamic add/remove subscriptions; the live source today is
  start-up-configured.

decision (snapshot context per subscription, caller-supplied):
  Same separation of concerns as the historical downloader:
  spot_price, OI, IV come from sibling endpoints and are
  injected via ``get_context_for_contract(contract) -> ContextSnapshot``.
  Production wires through periodic snapshot refresh; tests pass
  a static lambda.

WebSocket frame format (from ThetaData docs at fetch time):
  Trade frame: {"type": "trade", "contract": {...}, "data": [...]}
    where data is the same positional array the REST endpoint
    returns: [ms_of_day, sequence, ext_cond1..4, condition, size,
    exchange, price, condition_flags, price_flags, volume_type,
    records_back, date]
  Quote frame: {"type": "quote", "contract": {...}, "data": [...]}
    data: [ms_of_day, bid_size, bid_exchange, bid, bid_condition,
    ask_size, ask_exchange, ask, ask_condition, date]
  Ping/heartbeat: {"type": "ping"} — ignored
  Status: {"type": "status", ...} — logged, ignored

The exact frame schema may evolve before integration testing; the
``_decode_frame`` helper centralises parsing so a documented
schema change is a one-place fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from uoa_detector.sources.thetadata.mapping import (
    QuoteRow,
    map_thetadata_trade_to_rawprint,
    quote_row_from_array,
    trade_row_from_array,
)

if TYPE_CHECKING:
    from datetime import date

    from pydantic import SecretStr

    from uoa_detector.calibration.profile import ThetaDataSettings
    from uoa_detector.domain.raw_print import RawPrint
    from uoa_detector.sources.thetadata.historical import (
        ContextSnapshot,
        ContractSpec,
    )
    from uoa_detector.sources.thetadata.mapping import OptionRight


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket abstract surface
# ---------------------------------------------------------------------------


class _WebSocketLike(Protocol):
    """Minimal WS surface the live source uses.

    websockets.WebSocketClientProtocol satisfies this structurally.
    Tests inject a fake.
    """

    async def recv(self) -> str | bytes: ...
    async def send(self, message: str | bytes) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ThetaDataLiveError(RuntimeError):
    """Base for live-source-specific errors."""


class ReconnectExhaustedError(ThetaDataLiveError):
    """All reconnect attempts failed; the operator must intervene."""


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveSubscription:
    """One contract subscription request.

    The live source builds its initial subscribe message from the
    set of LiveSubscriptions passed to the constructor.
    """

    ticker: str
    expiry: date
    strike_dollars: object  # Decimal — avoid Decimal import in this dataclass
    right: OptionRight


# ---------------------------------------------------------------------------
# Live source
# ---------------------------------------------------------------------------


# Type alias for the connection factory.
ConnectFactory = Callable[
    [str, dict[str, str]],
    "Awaitable[_WebSocketLike]",
]


class ThetaDataLiveSource:
    """RawFlowSource implementation backed by the ThetaData WebSocket.

    Lifecycle:
      live = ThetaDataLiveSource(
          api_key=SecretStr(...),
          settings=ThetaDataSettings(...),
          subscriptions=[...],
          get_context_for_contract=lambda c: ContextSnapshot(...),
          ws_url="ws://127.0.0.1:25520/v1/events",
      )
      async for rp in live.stream():
          handle(rp)
      await live.close()
    """

    source_id: str = "thetadata"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        settings: ThetaDataSettings,
        subscriptions: Iterable[LiveSubscription],
        get_context_for_contract: Callable[[ContractSpec], ContextSnapshot],
        ws_url: str = "ws://127.0.0.1:25520/v1/events",
        connect_factory: ConnectFactory | None = None,
        source_id: str = "thetadata",
    ) -> None:
        self._api_key = api_key
        self._settings = settings
        self._subscriptions = list(subscriptions)
        self._get_context = get_context_for_contract
        self._ws_url = ws_url
        self._connect_factory = connect_factory or _default_connect_factory
        self.source_id = source_id

        # Per-contract latest bid/ask cache. Key: (ticker, expiry,
        # strike_dollars, right). Updated on every quote frame.
        self._bid_ask_cache: dict[tuple[str, date, object, str], QuoteRow] = {}
        self._closed = False
        self._current_ws: _WebSocketLike | None = None

    # -- public API -------------------------------------------------------

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield ``RawPrint`` events from the live WS feed.

        Reconnect logic: on disconnect, wait ``initial_backoff_s
        × 2^attempt`` (capped at ``max_backoff_s``) and try again,
        up to ``max_attempts`` times before raising
        ``ReconnectExhaustedError``. Successful frame reception
        resets the attempt counter.
        """
        attempt = 0
        max_attempts = self._settings.live_reconnect_max_attempts
        while not self._closed:
            try:
                async for rp in self._stream_once():
                    if self._closed:
                        return
                    yield rp
                    attempt = 0  # successful frame resets backoff
                # Stream exited cleanly without close() — treat as
                # disconnect and re-attempt unless caller closed.
                if self._closed:
                    return
            except (TimeoutError, ConnectionError, OSError) as exc:
                _logger.warning(
                    "ThetaData WS error (attempt %d): %s", attempt + 1, exc,
                )
            except ThetaDataLiveError:
                raise
            # Reconnect with backoff
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
        return {"X-Api-Key": self._api_key.get_secret_value()}

    def _build_subscribe_message(self) -> str:
        """Build the WS subscribe payload.

        Frame format follows the ThetaData WS docs at the time of
        implementation. Schema-evolution risk lives here; one-place
        fix when the docs change.
        """
        return json.dumps({
            "msg_type": "STREAM",
            "sec_type": "OPTION",
            "req_type": "TRADE",
            "contracts": [
                {
                    "root": s.ticker.upper(),
                    "expiration": _yyyymmdd(s.expiry),
                    "strike": str(s.strike_dollars),
                    "right": s.right,
                }
                for s in self._subscriptions
            ],
        })

    async def _stream_once(self) -> AsyncIterator[RawPrint]:
        """One connection lifecycle: connect, subscribe, recv until error.

        On any exception, the outer loop in ``stream()`` handles the
        reconnect; this generator just propagates.
        """
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
        """Parse one WS frame; return RawPrint if it's a usable trade."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            _logger.warning("Dropping malformed JSON frame")
            return None
        if not isinstance(frame, dict):
            return None

        ftype = frame.get("type") or frame.get("msg_type")
        if ftype in ("ping", "PING", "pong", "PONG", "status", "STATUS"):
            return None
        if ftype in ("quote", "QUOTE"):
            self._handle_quote_frame(frame)
            return None
        if ftype in ("trade", "TRADE"):
            return self._handle_trade_frame(frame)
        # Unknown type — log and ignore so a new ThetaData event
        # type doesn't crash the stream.
        _logger.debug("Ignoring unknown frame type: %s", ftype)
        return None

    def _contract_key(
        self, contract: dict[str, object],
    ) -> tuple[str, date, object, str]:
        """Build the bid/ask cache key from a frame's contract block."""
        from datetime import date as date_cls
        from decimal import Decimal

        ticker = str(contract["root"]).upper()
        exp_int = int(str(contract["expiration"]))
        exp = date_cls(exp_int // 10000, (exp_int // 100) % 100, exp_int % 100)
        strike = Decimal(str(contract["strike"]))
        right = str(contract["right"])
        return (ticker, exp, strike, right)

    def _handle_quote_frame(self, frame: dict[str, object]) -> None:
        """Cache the (bid, ask) for the contract."""
        contract = frame.get("contract")
        data = frame.get("data")
        if not isinstance(contract, dict) or not isinstance(data, list):
            return
        try:
            quote = quote_row_from_array(data)
        except (ValueError, TypeError) as exc:
            _logger.warning("Dropping malformed quote frame: %s", exc)
            return
        key = self._contract_key(contract)
        self._bid_ask_cache[key] = quote

    def _handle_trade_frame(
        self, frame: dict[str, object],
    ) -> RawPrint | None:
        """Decode a trade frame and emit a RawPrint."""
        from decimal import Decimal

        from uoa_detector.sources.thetadata.historical import (
            ContractSpec,
        )

        contract_block = frame.get("contract")
        data = frame.get("data")
        if not isinstance(contract_block, dict) or not isinstance(data, list):
            return None
        try:
            trade = trade_row_from_array(data)
        except (ValueError, TypeError) as exc:
            _logger.warning("Dropping malformed trade frame: %s", exc)
            return None

        key = self._contract_key(contract_block)
        quote = self._bid_ask_cache.get(key)
        if quote is None:
            # Symmetric with historical's align: no preceding quote → drop.
            _logger.debug("Trade arrived before any quote for %s; dropping", key)
            return None

        ticker, expiry, strike_dollars, right = key
        contract = ContractSpec(
            ticker=ticker, expiry=expiry,
            strike_dollars=Decimal(str(strike_dollars)),
            right=right,  # type: ignore[arg-type]
        )
        snapshot = self._get_context(contract)

        return map_thetadata_trade_to_rawprint(
            trade_row=trade,
            ticker=ticker,
            expiry=expiry,
            strike_dollars=Decimal(str(strike_dollars)),
            right=right,  # type: ignore[arg-type]
            spot_price=snapshot.spot_price,
            bid=quote.bid,
            ask=quote.ask,
            open_interest=snapshot.open_interest,
            implied_volatility=snapshot.implied_volatility,
            source_id=self.source_id,
        )


# ---------------------------------------------------------------------------
# Default connection factory (production)
# ---------------------------------------------------------------------------


async def _default_connect_factory(
    url: str, headers: dict[str, str],
) -> _WebSocketLike:
    """Wrap ``websockets.connect`` with the auth headers.

    Imported lazily so test environments without websockets installed
    don't break — though it's a hard dep at the project level
    (Phase 3.3.2.3 added it).
    """
    import websockets

    additional = [(k, v) for k, v in headers.items()]
    ws = await websockets.connect(url, additional_headers=additional)
    return ws  # structural protocol match — _WebSocketLike duck-typed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yyyymmdd(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"
