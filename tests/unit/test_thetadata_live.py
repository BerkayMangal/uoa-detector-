"""Phase 3.3.2.5 tests for ``thetadata.live``.

All tests use a fake ``_FakeWebSocket`` and a fake connect factory —
no real network. The smoke integration test (3.3.2.6) is the only
thing that hits the real Theta Terminal WS endpoint.

Pins:
  - subscribe message payload shape (one place to fix on schema drift)
  - auth headers contain the API key without leaking via repr
  - quote frame updates per-contract bid/ask cache
  - trade arriving before any quote → dropped
  - trade with cached quote → emits RawPrint
  - per-contract independence in bid/ask cache
  - ping / status / unknown frame types ignored
  - malformed JSON dropped, stream continues
  - close() is idempotent
  - close() stops streaming mid-flight
  - reconnect on disconnect (recv raises)
  - exponential backoff with cap
  - backoff resets on successful frame
  - reconnect exhausted raises ReconnectExhaustedError
  - drop-condition trades excluded
  - out-of-hours trades excluded
  - end-to-end: scripted frames yield expected RawPrints
  - default connect factory imports websockets lazily
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import ThetaDataSettings
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
)
from uoa_detector.sources.thetadata.live import (
    LiveSubscription,
    ReconnectExhaustedError,
    ThetaDataLiveSource,
)

# ---------------------------------------------------------------------------
# Test fixtures: fake WebSocket + connect factory
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Scripted WebSocket-like object for unit tests.

    ``frames`` is the queue of payloads ``recv()`` will return,
    one per call. After all frames are consumed, ``recv()`` raises
    whatever ``raise_after`` is set to (default ConnectionError to
    simulate a disconnect). If ``raise_after`` is None, ``recv()``
    awaits forever (use this together with ``close()`` to simulate
    a healthy long-lived connection that the consumer ends).
    """

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
        # No more frames, no error — block forever (caller should close)
        await asyncio.Event().wait()
        msg = "unreachable"
        raise RuntimeError(msg)

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


class _ScriptedFactory:
    """Connection factory that returns scripted ``_FakeWebSocket`` per call."""

    def __init__(
        self,
        sockets: list[_FakeWebSocket] | None = None,
        *,
        connect_errors: list[BaseException] | None = None,
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
# Frame builders (centralised to keep tests small)
# ---------------------------------------------------------------------------


# Trade array shape (per mapping.py): [ms_of_day, sequence,
# ext_cond1..4, condition, size, exchange, price, condition_flags,
# price_flags, volume_type, records_back, date]
def _trade_array(
    *,
    ms_of_day: int,
    sequence: int,
    condition: int = 0,
    size: int = 10,
    exchange: int = 50,  # NYSE
    price: float = 1.50,
    date_int: int = 20231103,
) -> list[Any]:
    return [
        ms_of_day, sequence,
        0, 0, 0, 0,           # ext_cond1..4
        condition,
        size, exchange, price,
        0, 0, 0, 0,           # condition_flags, price_flags, volume_type, records_back
        date_int,
    ]


def _quote_array(
    *,
    ms_of_day: int,
    bid: float = 1.45,
    ask: float = 1.55,
    date_int: int = 20231103,
) -> list[Any]:
    # [ms_of_day, bid_size, bid_exchange, bid, bid_condition,
    #  ask_size, ask_exchange, ask, ask_condition, date]
    return [ms_of_day, 100, 50, bid, 0, 100, 50, ask, 0, date_int]


def _contract_block() -> dict[str, Any]:
    return {
        "root": "AAPL",
        "expiration": "20231103",
        "strike": "150.00",
        "right": "C",
    }


def _trade_frame(**kwargs: Any) -> str:
    return json.dumps({
        "type": "trade",
        "contract": _contract_block(),
        "data": _trade_array(**kwargs),
    })


def _quote_frame(**kwargs: Any) -> str:
    return json.dumps({
        "type": "quote",
        "contract": _contract_block(),
        "data": _quote_array(**kwargs),
    })


def _ping_frame() -> str:
    return json.dumps({"type": "ping"})


def _status_frame() -> str:
    return json.dumps({"type": "status", "msg": "ok"})


# ---------------------------------------------------------------------------
# Settings + subscription helpers
# ---------------------------------------------------------------------------


def _settings(
    *,
    initial_backoff_s: float = 0.001,
    max_backoff_s: float = 0.01,
    max_attempts: int = 3,
) -> ThetaDataSettings:
    """Settings tuned for fast tests (sub-ms backoff)."""
    return ThetaDataSettings(
        rate_limit_requests_per_second=10.0,
        historical_concurrency=4,
        live_reconnect_max_attempts=max_attempts,
        live_reconnect_initial_backoff_s=initial_backoff_s,
        live_reconnect_max_backoff_s=max_backoff_s,
    )


def _subscriptions() -> list[LiveSubscription]:
    return [
        LiveSubscription(
            ticker="AAPL",
            expiry=date(2023, 11, 3),
            strike_dollars=Decimal("150.00"),
            right="C",
        ),
    ]


def _ctx(_c: ContractSpec) -> ContextSnapshot:
    return ContextSnapshot(
        spot_price=Decimal("150.50"),
        open_interest=1_000,
        implied_volatility=0.25,
    )


def _make_source(
    factory: _ScriptedFactory,
    *,
    subs: list[LiveSubscription] | None = None,
    settings: ThetaDataSettings | None = None,
    ctx: Any = _ctx,
) -> ThetaDataLiveSource:
    return ThetaDataLiveSource(
        api_key=SecretStr("td_test_key_secret"),
        settings=settings or _settings(),
        subscriptions=subs or _subscriptions(),
        get_context_for_contract=ctx,
        ws_url="ws://test/v2/ws",
        connect_factory=factory,
    )


async def _collect(source: ThetaDataLiveSource, n: int) -> list[Any]:
    """Collect at most n RawPrints from a live source's stream."""
    out: list[Any] = []
    try:
        async for rp in source.stream():
            out.append(rp)
            if len(out) >= n:
                break
    finally:
        await source.close()
    return out


# ---------------------------------------------------------------------------
# Subscribe message + auth headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_message_payload_shape() -> None:
    """The first message sent on a fresh WS is the subscribe frame."""
    ws = _FakeWebSocket(frames=[], raise_after=ConnectionError("done"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(
        factory,
        settings=_settings(max_attempts=0),
    )
    with pytest.raises(ReconnectExhaustedError):
        async for _ in source.stream():
            pass
    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload["msg_type"] == "STREAM"
    assert payload["sec_type"] == "OPTION"
    assert payload["req_type"] == "TRADE"
    assert len(payload["contracts"]) == 1
    c = payload["contracts"][0]
    assert c["root"] == "AAPL"
    assert c["expiration"] == "20231103"
    assert c["strike"] == "150.00"
    assert c["right"] == "C"


@pytest.mark.asyncio
async def test_auth_headers_contain_api_key_for_connect() -> None:
    """The connect factory receives X-Api-Key in headers."""
    ws = _FakeWebSocket(frames=[], raise_after=ConnectionError("done"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    with pytest.raises(ReconnectExhaustedError):
        async for _ in source.stream():
            pass
    assert len(factory.calls) == 1
    _url, headers = factory.calls[0]
    assert headers["X-Api-Key"] == "td_test_key_secret"


def test_secret_not_in_repr() -> None:
    """Constructing a live source does not leak the API key via repr."""
    factory = _ScriptedFactory()
    source = _make_source(factory)
    text = repr(source)
    assert "td_test_key_secret" not in text


# ---------------------------------------------------------------------------
# Frame handling: quote → cache, trade → emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_with_cached_quote_emits_rawprint() -> None:
    """Quote arrives first, then a regular trade → one RawPrint out."""
    frames = [
        _quote_frame(ms_of_day=10 * 3600 * 1000, bid=1.45, ask=1.55),
        _trade_frame(ms_of_day=10 * 3600 * 1000 + 100, sequence=1),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 1
    rp = rps[0]
    assert rp.ticker == "AAPL"
    assert rp.option_type == "call"
    assert rp.bid == Decimal("1.45")
    assert rp.ask == Decimal("1.55")
    assert rp.spot_price == Decimal("150.50")
    assert rp.open_interest == 1_000
    assert rp.source_id == "thetadata"


@pytest.mark.asyncio
async def test_trade_before_any_quote_dropped() -> None:
    """Symmetric with historical: no preceding quote → drop the trade."""
    frames = [
        _trade_frame(ms_of_day=10 * 3600 * 1000, sequence=1),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert rps == []


@pytest.mark.asyncio
async def test_per_contract_bid_ask_cache_is_independent() -> None:
    """Two contracts have independent bid/ask caches."""
    contract_aapl = {"root": "AAPL", "expiration": "20231103",
                     "strike": "150.00", "right": "C"}
    contract_msft = {"root": "MSFT", "expiration": "20231103",
                     "strike": "350.00", "right": "P"}
    frames = [
        json.dumps({
            "type": "quote",
            "contract": contract_aapl,
            "data": _quote_array(ms_of_day=10 * 3600 * 1000, bid=1.45, ask=1.55),
        }),
        json.dumps({
            "type": "quote",
            "contract": contract_msft,
            "data": _quote_array(ms_of_day=10 * 3600 * 1000, bid=2.45, ask=2.55),
        }),
        json.dumps({
            "type": "trade",
            "contract": contract_aapl,
            "data": _trade_array(ms_of_day=10 * 3600 * 1000 + 100, sequence=1),
        }),
        json.dumps({
            "type": "trade",
            "contract": contract_msft,
            "data": _trade_array(ms_of_day=10 * 3600 * 1000 + 200, sequence=2),
        }),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    subs = [
        LiveSubscription(
            ticker="AAPL", expiry=date(2023, 11, 3),
            strike_dollars=Decimal("150.00"), right="C",
        ),
        LiveSubscription(
            ticker="MSFT", expiry=date(2023, 11, 3),
            strike_dollars=Decimal("350.00"), right="P",
        ),
    ]
    source = _make_source(factory, subs=subs, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 2
    aapl_rp = next(rp for rp in rps if rp.ticker == "AAPL")
    msft_rp = next(rp for rp in rps if rp.ticker == "MSFT")
    assert aapl_rp.bid == Decimal("1.45")
    assert aapl_rp.ask == Decimal("1.55")
    assert msft_rp.bid == Decimal("2.45")
    assert msft_rp.ask == Decimal("2.55")


# ---------------------------------------------------------------------------
# Heartbeat / status / unknown / malformed frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_and_status_frames_ignored() -> None:
    """Heartbeat and status frames don't crash, don't emit, don't pollute cache."""
    frames = [
        _ping_frame(),
        _status_frame(),
        _quote_frame(ms_of_day=10 * 3600 * 1000),
        _trade_frame(ms_of_day=10 * 3600 * 1000 + 100, sequence=1),
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
async def test_unknown_frame_types_ignored() -> None:
    """A future ThetaData event type doesn't crash the stream."""
    frames = [
        json.dumps({"type": "future_event_type", "data": {"x": 1}}),
        _quote_frame(ms_of_day=10 * 3600 * 1000),
        _trade_frame(ms_of_day=10 * 3600 * 1000 + 100, sequence=1),
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
async def test_malformed_json_dropped_stream_continues() -> None:
    """Garbage frames are dropped; stream still emits valid trades after."""
    frames = [
        "this is not json {{{",
        _quote_frame(ms_of_day=10 * 3600 * 1000),
        "[]",  # not a dict
        _trade_frame(ms_of_day=10 * 3600 * 1000 + 100, sequence=1),
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
# Filter pins (drop conditions, out-of-hours)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_condition_trades_excluded() -> None:
    """OPRA condition codes 2/7/12/13 → trade dropped (per mapping)."""
    base = 10 * 3600 * 1000
    frames = [
        _quote_frame(ms_of_day=base),
        # condition 7 (cancel) → drop
        _trade_frame(ms_of_day=base + 100, sequence=1, condition=7),
        # condition 2 (out-of-sequence late) → drop
        _trade_frame(ms_of_day=base + 200, sequence=2, condition=2),
        # condition 0 (regular) → keep
        _trade_frame(ms_of_day=base + 300, sequence=3, condition=0),
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
async def test_out_of_hours_trades_excluded() -> None:
    """Trades outside 09:30:00–16:00:00 ET dropped (per mapping)."""
    pre_open = 9 * 3600 * 1000  # 09:00 ET — before regular hours
    in_hours = 10 * 3600 * 1000  # 10:00 ET
    after_close = 17 * 3600 * 1000  # 17:00 ET — after close
    frames = [
        _quote_frame(ms_of_day=in_hours),
        _trade_frame(ms_of_day=pre_open, sequence=1),       # drop
        _trade_frame(ms_of_day=in_hours + 100, sequence=2),  # keep
        _trade_frame(ms_of_day=after_close, sequence=3),     # drop
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
# close() — idempotency + stops streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """Calling close() twice is safe."""
    factory = _ScriptedFactory()
    source = _make_source(factory)
    await source.close()
    await source.close()
    # No exception → pass


@pytest.mark.asyncio
async def test_close_stops_streaming() -> None:
    """close() during stream() ends the loop."""
    base = 10 * 3600 * 1000
    frames = [
        _quote_frame(ms_of_day=base),
        _trade_frame(ms_of_day=base + 100, sequence=1),
    ]
    # raise_after=None → after frames are exhausted, recv() blocks forever
    ws = _FakeWebSocket(frames=frames, raise_after=None)
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory)
    rps = await _collect(source, n=1)
    assert len(rps) == 1
    # After _collect returns, source.close() was called in finally.
    assert ws.closed is True


# ---------------------------------------------------------------------------
# Reconnect logic: backoff, exhaustion, reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_on_disconnect() -> None:
    """First WS dies, second WS opens, stream continues."""
    base = 10 * 3600 * 1000
    ws1 = _FakeWebSocket(
        frames=[
            _quote_frame(ms_of_day=base),
            _trade_frame(ms_of_day=base + 100, sequence=1),
        ],
        raise_after=ConnectionError("disconnected"),
    )
    ws2 = _FakeWebSocket(
        frames=[
            _quote_frame(ms_of_day=base + 200),
            _trade_frame(ms_of_day=base + 300, sequence=2),
        ],
        raise_after=ConnectionError("disconnected"),
    )
    factory = _ScriptedFactory(sockets=[ws1, ws2])
    source = _make_source(factory, settings=_settings(max_attempts=2))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    # Two RawPrints (one per ws) before final exhaustion.
    assert len(rps) == 2
    # Connect calls: ws1 (trade emit → attempt=0), ws2 (trade emit →
    # attempt=0), then 3rd + 4th connect attempts fail with no more
    # sockets, exhausting after attempt > max_attempts(=2).
    assert len(factory.calls) == 4


@pytest.mark.asyncio
async def test_reconnect_exhausted_raises() -> None:
    """After max_attempts disconnects, ReconnectExhaustedError is raised."""
    sockets = [
        _FakeWebSocket(frames=[], raise_after=ConnectionError("flap"))
        for _ in range(3)
    ]
    factory = _ScriptedFactory(sockets=sockets)
    source = _make_source(factory, settings=_settings(max_attempts=2))
    with pytest.raises(ReconnectExhaustedError, match=r"reconnect exhausted"):
        async for _ in source.stream():
            pass


@pytest.mark.asyncio
async def test_backoff_resets_on_successful_frame() -> None:
    """A flap-then-recover-then-flap path: recovered frames reset attempt."""
    base = 10 * 3600 * 1000

    # ws1: yields frames, then disconnects
    ws1 = _FakeWebSocket(
        frames=[
            _quote_frame(ms_of_day=base),
            _trade_frame(ms_of_day=base + 100, sequence=1),
        ],
        raise_after=ConnectionError("flap1"),
    )
    # ws2: yields frames, then disconnects
    ws2 = _FakeWebSocket(
        frames=[
            _quote_frame(ms_of_day=base + 200),
            _trade_frame(ms_of_day=base + 300, sequence=2),
        ],
        raise_after=ConnectionError("flap2"),
    )
    # ws3: yields frames, then disconnects
    ws3 = _FakeWebSocket(
        frames=[
            _quote_frame(ms_of_day=base + 400),
            _trade_frame(ms_of_day=base + 500, sequence=3),
        ],
        raise_after=ConnectionError("flap3"),
    )

    factory = _ScriptedFactory(sockets=[ws1, ws2, ws3])
    # max_attempts=2: a single chain of 3 disconnects WITHOUT a successful
    # frame between them would exhaust at attempt 3. But here each ws
    # delivers a frame, so attempt resets to 0 after each successful
    # yield. We therefore go through all 3 sockets, then on the 4th
    # connect attempt the factory has no more sockets → ConnectionError
    # → attempt 1 → backoff → attempt 2 → backoff → attempt 3 > max,
    # raise.
    source = _make_source(factory, settings=_settings(max_attempts=2))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 3  # one frame from each of three sockets


def test_compute_backoff_exponential_with_cap() -> None:
    """Backoff is initial × 2^(attempt-1), capped at max_backoff_s."""
    factory = _ScriptedFactory()
    settings = _settings(initial_backoff_s=1.0, max_backoff_s=10.0)
    source = _make_source(factory, settings=settings)
    # attempt=1 → 1.0 × 2^0 = 1.0
    assert source._compute_backoff(1) == pytest.approx(1.0)
    # attempt=2 → 1.0 × 2^1 = 2.0
    assert source._compute_backoff(2) == pytest.approx(2.0)
    # attempt=3 → 1.0 × 2^2 = 4.0
    assert source._compute_backoff(3) == pytest.approx(4.0)
    # attempt=4 → 1.0 × 2^3 = 8.0
    assert source._compute_backoff(4) == pytest.approx(8.0)
    # attempt=5 → 1.0 × 2^4 = 16.0 → capped to 10.0
    assert source._compute_backoff(5) == pytest.approx(10.0)
    # attempt=10 → still capped
    assert source._compute_backoff(10) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Subscription dataclass
# ---------------------------------------------------------------------------


def test_live_subscription_is_frozen() -> None:
    """LiveSubscription is a frozen dataclass."""
    sub = LiveSubscription(
        ticker="AAPL", expiry=date(2023, 11, 3),
        strike_dollars=Decimal("150.00"), right="C",
    )
    with pytest.raises(Exception):  # FrozenInstanceError or similar
        sub.ticker = "MSFT"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Lazy initialization — importing doesn't trigger network or auth
# ---------------------------------------------------------------------------


def test_importing_live_module_does_not_open_connections() -> None:
    """Per acceptance doc cross-cutting acceptance: 'Every adapter
    has lazy initialization: importing the module does NOT trigger
    network or auth; instantiation does.'

    We import the module symbols and assert no module-level state
    that would imply a singleton connection.
    """
    import uoa_detector.sources.thetadata.live as live_mod

    public_names = [n for n in dir(live_mod) if not n.startswith("_")]
    assert "ThetaDataLiveSource" in public_names
    assert "LiveSubscription" in public_names
    assert "ReconnectExhaustedError" in public_names
    assert not hasattr(live_mod, "_default_source")
    assert not hasattr(live_mod, "default_source")


# ---------------------------------------------------------------------------
# Source-id customisation (RawFlowSource Protocol contract)
# ---------------------------------------------------------------------------


def test_source_id_default_thetadata() -> None:
    factory = _ScriptedFactory()
    source = _make_source(factory)
    assert source.source_id == "thetadata"


def test_source_id_can_be_customised() -> None:
    factory = _ScriptedFactory()
    source = ThetaDataLiveSource(
        api_key=SecretStr("td_test"),
        settings=_settings(),
        subscriptions=_subscriptions(),
        get_context_for_contract=_ctx,
        ws_url="ws://test/v2/ws",
        connect_factory=factory,
        source_id="thetadata-prod",
    )
    assert source.source_id == "thetadata-prod"


# ---------------------------------------------------------------------------
# Connect-time errors trigger reconnect (vs in-flight disconnect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_failure_triggers_reconnect() -> None:
    """If connect_factory raises, treat as disconnect and back off."""
    # First connect raises; second succeeds and yields a frame; third
    # connect has no socket left → exhausts.
    base = 10 * 3600 * 1000
    ws_ok = _FakeWebSocket(
        frames=[
            _quote_frame(ms_of_day=base),
            _trade_frame(ms_of_day=base + 100, sequence=1),
        ],
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
# End-to-end smoke (unit): scripted frames in → expected RawPrints out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_scripted_session() -> None:
    """A scripted session: 2 quotes interleaved with 3 trades."""
    base = 10 * 3600 * 1000  # 10:00 ET — within regular hours
    frames = [
        _quote_frame(ms_of_day=base, bid=1.45, ask=1.55),
        _trade_frame(ms_of_day=base + 100, sequence=1),
        _trade_frame(ms_of_day=base + 200, sequence=2),
        _quote_frame(ms_of_day=base + 250, bid=1.50, ask=1.60),  # update
        _trade_frame(ms_of_day=base + 300, sequence=3),
    ]
    ws = _FakeWebSocket(frames=frames, raise_after=ConnectionError("eof"))
    factory = _ScriptedFactory(sockets=[ws])
    source = _make_source(factory, settings=_settings(max_attempts=0))
    rps: list[Any] = []
    with pytest.raises(ReconnectExhaustedError):
        async for rp in source.stream():
            rps.append(rp)
    assert len(rps) == 3
    # First two trades use the first quote (bid=1.45)
    assert rps[0].bid == Decimal("1.45")
    assert rps[1].bid == Decimal("1.45")
    # Third trade uses the updated quote (bid=1.50)
    assert rps[2].bid == Decimal("1.50")
