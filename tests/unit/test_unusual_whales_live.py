"""Phase 3.3.3.3 tests for ``unusual_whales.live``.

All tests use a fake ``_FakeWebSocket`` and a fake connect factory —
no real network. The smoke integration tests (3.3.3.6) hit the real
UW endpoint.

Pins:
  - subscribe message: 'flow_alerts' channel + ticker list (uppercase)
  - Bearer auth header on connect (vs ThetaData's X-Api-Key)
  - SecretStr never in repr
  - happy-path event → RawPrint with all fields populated
  - UW alert_type → ``RawPrint.source_tags`` (e.g. 'uw:sweep')
  - UW side label mapping (parametric: ASK/BID/MID/etc.)
  - spot_price absent → Decimal(0) + 'uw:no-spot' tag
  - DTE<0 dropped
  - malformed JSON dropped, stream continues
  - missing required fields → drop, log a warning
  - ping/heartbeat/status frames ignored
  - reconnect on disconnect with exponential backoff
  - backoff resets on successful frame
  - reconnect exhausted → ReconnectExhaustedError
  - close() idempotent + stops streaming
  - source_id default 'unusual_whales' + customisable
  - lazy import (no module-level connections)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.sources.unusual_whales.live import (
    FlowSubscription,
    ReconnectExhaustedError,
    UnusualWhalesLiveSource,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Scripted WebSocket-like object."""

    def __init__(
        self,
        frames: list[str],
        *,
        raise_after: BaseException | None = None,
    ) -> None:
        self._frames = list(frames)
        self._raise_after = raise_after
        self.sent: list[str | bytes] = []
        self.closed = False
        self._idx = 0

    async def recv(self) -> str | bytes:
        if self.closed:
            msg = "fake ws closed"
            raise ConnectionError(msg)
        if self._idx < len(self._frames):
            f = self._frames[self._idx]
            self._idx += 1
            return f
        if self._raise_after is not None:
            raise self._raise_after
        await asyncio.Event().wait()
        msg = "unreachable"
        raise RuntimeError(msg)

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


class _ScriptedFactory:
    def __init__(
        self,
        sockets: list[_FakeWebSocket] | None = None,
        *,
        connect_errors: list[BaseException | None] | None = None,
    ) -> None:
        self._sockets = list(sockets or [])
        self._connect_errors = list(connect_errors or [])
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(
        self, url: str, headers: dict[str, str],
    ) -> _FakeWebSocket:
        self.calls.append((url, dict(headers)))
        if self._connect_errors:
            err = self._connect_errors.pop(0)
            if err is not None:
                raise err
        if not self._sockets:
            msg = "no more scripted sockets"
            raise ConnectionError(msg)
        return self._sockets.pop(0)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _settings(
    *,
    initial_backoff_s: float = 0.001,
    max_backoff_s: float = 0.01,
    max_attempts: int = 3,
) -> UnusualWhalesSettings:
    return UnusualWhalesSettings(
        rate_limit_requests_per_second=10.0,
        historical_concurrency=2,
        live_reconnect_max_attempts=max_attempts,
        live_reconnect_initial_backoff_s=initial_backoff_s,
        live_reconnect_max_backoff_s=max_backoff_s,
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


def _flow_event(**overrides: Any) -> dict[str, Any]:
    """Default UW flow event dict; override via kwargs for variation."""
    base: dict[str, Any] = {
        "id": "evt_12345",
        "ticker": "AAPL",
        "executed_at": "2024-01-15T15:30:00Z",
        "option_type": "call",
        "strike": "150.00",
        "expiry": "2024-02-16",
        "premium": "12345.00",
        "price": "1.50",
        "bid": "1.45",
        "ask": "1.55",
        "side": "ASK",
        "open_interest": 1000,
        "implied_volatility": 0.25,
        "is_iso": False,
        "exchange": "CBOE",
        "alert_type": "sweep",
    }
    base.update(overrides)
    return base


def _flow_frame(**overrides: Any) -> str:
    return json.dumps({
        "type": "flow_alert",
        "data": _flow_event(**overrides),
    })


def _ping_frame() -> str:
    return json.dumps({"type": "ping"})


def _make_source(
    factory: _ScriptedFactory,
    *,
    settings: UnusualWhalesSettings | None = None,
    subs: list[FlowSubscription] | None = None,
) -> UnusualWhalesLiveSource:
    return UnusualWhalesLiveSource(
        api_key=SecretStr("uw_test_key_secret_value"),
        settings=settings or _settings(),
        subscriptions=subs or [FlowSubscription(ticker="AAPL")],
        ws_url="wss://test/v1/ws",
        connect_factory=factory,
    )


# ---------------------------------------------------------------------------
# Subscribe message + auth headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_message_payload_shape() -> None:
    """First message on a fresh WS is the subscribe frame."""
    ws = _FakeWebSocket(frames=[], raise_after=ConnectionError("done"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    with pytest.raises(ReconnectExhaustedError):
        async for _ in source.stream():
            pass
    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload["channel"] == "flow_alerts"
    assert payload["tickers"] == ["AAPL"]


@pytest.mark.asyncio
async def test_subscribe_with_multiple_tickers_uppercased() -> None:
    """Tickers are uppercased; mixed case input still works."""
    ws = _FakeWebSocket(frames=[], raise_after=ConnectionError("done"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(
        factory,
        settings=_settings(max_attempts=0),
        subs=[
            FlowSubscription(ticker="aapl"),
            FlowSubscription(ticker="MSFT"),
            FlowSubscription(ticker="Tsla"),
        ],
    )
    with pytest.raises(ReconnectExhaustedError):
        async for _ in source.stream():
            pass
    payload = json.loads(ws.sent[0])
    assert payload["tickers"] == ["AAPL", "MSFT", "TSLA"]


@pytest.mark.asyncio
async def test_bearer_auth_header_on_connect() -> None:
    """UW uses 'Authorization: Bearer <key>' on the WS handshake."""
    ws = _FakeWebSocket(frames=[], raise_after=ConnectionError("done"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    with pytest.raises(ReconnectExhaustedError):
        async for _ in source.stream():
            pass
    assert len(factory.calls) == 1
    _url, headers = factory.calls[0]
    assert headers["Authorization"] == "Bearer uw_test_key_secret_value"
    # Negative pin: X-Api-Key NOT used
    assert "X-Api-Key" not in headers


def test_secret_not_in_repr() -> None:
    """Constructing a live source does not leak the API key via repr."""
    factory = _ScriptedFactory()
    source = _make_source(factory)
    text = repr(source)
    assert "uw_test_key_secret_value" not in text


# ---------------------------------------------------------------------------
# Happy-path event → RawPrint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_event_emits_rawprint_with_correct_fields() -> None:
    """One flow event → one RawPrint with correctly mapped fields."""
    frames = [_flow_frame()]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1
    rp = rps[0]
    assert rp.source_id == "unusual_whales"
    assert rp.source_event_id == "uw-evt_12345"
    assert rp.ticker == "AAPL"
    assert rp.option_type == "call"
    assert rp.strike == Decimal("150.00")
    assert rp.premium_paid == Decimal("12345.00")
    assert rp.option_price == Decimal("1.50")
    assert rp.bid == Decimal("1.45")
    assert rp.ask == Decimal("1.55")
    assert rp.fill_side == "at_ask"
    assert rp.exchange == "CBOE"
    assert rp.open_interest == 1000
    assert rp.implied_volatility == 0.25
    assert rp.is_iso is False
    # Phase 3.3.3.3 design pin: alert_type lifts into source_tags
    assert "uw:sweep" in rp.source_tags
    # Phase 3.3.3.3 design pin: spot_price absent → 0 + 'uw:no-spot'
    assert rp.spot_price == Decimal("0")
    assert "uw:no-spot" in rp.source_tags


# ---------------------------------------------------------------------------
# Side label mapping (parametric)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side_raw", "expected"),
    [
        ("ASK", "at_ask"),
        ("BID", "at_bid"),
        ("MID", "midpoint"),
        ("ABOVE_ASK", "above_ask"),
        ("BELOW_BID", "below_bid"),
        ("at_ask", "at_ask"),     # case-insensitive
        ("AT_BID", "at_bid"),
        ("WEIRD", "unknown"),     # unknown → unknown
    ],
)
async def test_uw_side_mapping_to_fill_side(
    side_raw: str, expected: str,
) -> None:
    frames = [_flow_frame(side=side_raw)]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert rps[0].fill_side == expected


# ---------------------------------------------------------------------------
# Spot-price tag pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spot_price_present_used_no_tag() -> None:
    """If UW (or a wrapper) supplies spot_price, use it and don't tag."""
    frames = [_flow_frame(spot_price="151.25")]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert rps[0].spot_price == Decimal("151.25")
    assert "uw:no-spot" not in rps[0].source_tags


# ---------------------------------------------------------------------------
# DTE filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_contract_dropped() -> None:
    """A trade after expiry produces a negative DTE; drop it."""
    frames = [
        _flow_frame(
            executed_at="2024-03-01T15:30:00Z",
            expiry="2024-02-16",  # expired before trade
        ),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert rps == []


# ---------------------------------------------------------------------------
# Resilience: malformed JSON, missing fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_dropped_stream_continues() -> None:
    """Garbage frame dropped; valid event after still emits."""
    frames = [
        "this is not json {{",
        _flow_frame(id="evt_after_garbage"),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1
    assert rps[0].source_event_id == "uw-evt_after_garbage"


@pytest.mark.asyncio
async def test_missing_required_field_dropped_stream_continues() -> None:
    """An event missing 'strike' is dropped; the next valid event emits."""
    bad = _flow_event()
    bad.pop("strike")
    frames = [
        json.dumps({"type": "flow_alert", "data": bad}),
        _flow_frame(id="evt_recovery"),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1
    assert rps[0].source_event_id == "uw-evt_recovery"


@pytest.mark.asyncio
async def test_missing_id_uses_fallback_sequence() -> None:
    """When UW omits 'id', the source assigns uw-fallback-{N}."""
    bad = _flow_event()
    bad.pop("id")
    frames = [
        json.dumps({"type": "flow_alert", "data": bad}),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1
    assert rps[0].source_event_id.startswith("uw-fallback-")


# ---------------------------------------------------------------------------
# Heartbeat / status / unknown frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_and_status_frames_ignored() -> None:
    frames = [
        _ping_frame(),
        json.dumps({"type": "status", "msg": "ok"}),
        _flow_frame(),
        _ping_frame(),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1


@pytest.mark.asyncio
async def test_unknown_frame_type_ignored() -> None:
    """A future UW event type doesn't crash the stream."""
    frames = [
        json.dumps({"type": "future_event_type", "data": {}}),
        _flow_frame(),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1


# ---------------------------------------------------------------------------
# alert_type tagging variations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alert_type", ["sweep", "block", "split", "repeated"],
)
async def test_alert_type_lifts_to_source_tags(alert_type: str) -> None:
    frames = [_flow_frame(alert_type=alert_type)]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert f"uw:{alert_type}" in rps[0].source_tags


# ---------------------------------------------------------------------------
# Reconnect / backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_on_disconnect() -> None:
    """First WS dies, second WS opens, stream continues."""
    ws1 = _FakeWebSocket(
        frames=[_flow_frame(id="ev1")],
        raise_after=ConnectionError("disconnected"),
    )
    ws2 = _FakeWebSocket(
        frames=[_flow_frame(id="ev2")],
        raise_after=ConnectionError("disconnected"),
    )
    factory = _ScriptedFactory(sockets=[ws1, ws2])
    source = _make_source(factory, settings=_settings(max_attempts=2))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 2
    assert {rp.source_event_id for rp in rps} == {"uw-ev1", "uw-ev2"}


@pytest.mark.asyncio
async def test_reconnect_exhausted_raises() -> None:
    """After max_attempts disconnects with no successful frames."""
    sockets = [
        _FakeWebSocket(frames=[], raise_after=ConnectionError("flap"))
        for _ in range(3)
    ]
    factory = _ScriptedFactory(sockets=sockets)
    source = _make_source(factory, settings=_settings(max_attempts=2))
    with pytest.raises(ReconnectExhaustedError, match=r"reconnect exhausted"):
        async for _ in source.stream():
            pass


def test_compute_backoff_exponential_with_cap() -> None:
    """attempt=N backoff = initial * 2^(N-1), capped at max."""
    factory = _ScriptedFactory()
    settings = _settings(initial_backoff_s=1.0, max_backoff_s=10.0)
    source = _make_source(factory, settings=settings)
    assert source._compute_backoff(1) == pytest.approx(1.0)
    assert source._compute_backoff(2) == pytest.approx(2.0)
    assert source._compute_backoff(5) == pytest.approx(10.0)  # capped
    assert source._compute_backoff(10) == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_connect_failure_triggers_reconnect() -> None:
    ws_ok = _FakeWebSocket(
        frames=[_flow_frame()],
        raise_after=ConnectionError("eof"),
    )
    factory = _ScriptedFactory(
        sockets=[ws_ok],
        connect_errors=[ConnectionError("connect refused"), None],
    )
    source = _make_source(factory, settings=_settings(max_attempts=2))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1


# ---------------------------------------------------------------------------
# close() behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    factory = _ScriptedFactory()
    source = _make_source(factory)
    await source.close()
    await source.close()


@pytest.mark.asyncio
async def test_close_stops_streaming() -> None:
    """close() during stream() ends the loop cleanly."""
    ws = _FakeWebSocket(frames=[_flow_frame()], raise_after=None)
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory)
    rps: list[Any] = []
    try:
        async for rp in source.stream():
            rps.append(rp)
            if len(rps) >= 1:
                break
    finally:
        await source.close()
    assert len(rps) == 1
    assert ws.closed is True


# ---------------------------------------------------------------------------
# source_id customisation
# ---------------------------------------------------------------------------


def test_source_id_default_unusual_whales() -> None:
    factory = _ScriptedFactory()
    source = _make_source(factory)
    assert source.source_id == "unusual_whales"


def test_source_id_can_be_customised() -> None:
    factory = _ScriptedFactory()
    source = UnusualWhalesLiveSource(
        api_key=SecretStr("uw_test"),
        settings=_settings(),
        subscriptions=[FlowSubscription(ticker="AAPL")],
        ws_url="wss://test/v1/ws",
        connect_factory=factory,
        source_id="unusual_whales-prod",
    )
    assert source.source_id == "unusual_whales-prod"


# ---------------------------------------------------------------------------
# Lazy import
# ---------------------------------------------------------------------------


def test_importing_live_module_does_not_open_connections() -> None:
    """Cross-cutting acceptance: import != network/auth."""
    import uoa_detector.sources.unusual_whales.live as live_mod

    public_names = [n for n in dir(live_mod) if not n.startswith("_")]
    assert "UnusualWhalesLiveSource" in public_names
    assert "FlowSubscription" in public_names
    assert "ReconnectExhaustedError" in public_names
    assert not hasattr(live_mod, "_default_source")


# ---------------------------------------------------------------------------
# FlowSubscription frozen
# ---------------------------------------------------------------------------


def test_flow_subscription_is_frozen() -> None:
    sub = FlowSubscription(ticker="AAPL")
    with pytest.raises(Exception):
        sub.ticker = "MSFT"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timestamp_parsed_as_utc() -> None:
    """UW's 'Z' suffix is parsed correctly."""
    frames = [_flow_frame(executed_at="2024-01-15T15:30:00Z")]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert rps[0].timestamp == datetime(2024, 1, 15, 15, 30, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_timestamp_with_explicit_offset_parsed() -> None:
    """UW may emit explicit +00:00 offsets too."""
    frames = [_flow_frame(executed_at="2024-01-15T15:30:00+00:00")]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert rps[0].timestamp.tzinfo is not None
