"""Phase 3.3.3.6 acceptance pin: UnusualWhalesLiveSource integrated
into SourceFusion's multi-source pathway produces
``confidence_tier='unanimous'`` when both sources see the same trade.

This is the integration verification specified in
``docs/phase-3.3-acceptance.md`` (Phase 3.3.3 scope):

    'UnusualWhalesLiveSource integrated into SourceFusion's
     multi-source replay test from Phase 3.2.2.3 — verified that
     confidence_tier="unanimous" appears when both ThetaData and
     UW see the same print.'

The test wires:
  - ``UnusualWhalesLiveSource`` with a fake WebSocket yielding one
    flow event for AAPL.
  - ``SyntheticRawFlowSource`` with source_id='thetadata' yielding
    a matching ``RawPrint`` at the same (ticker, timestamp).
  - Both into ``SourceFusion`` with multi-source semantics.

It asserts the fused canonical ``OptionsPrint`` has
``source_agreement.confidence_tier == 'unanimous'``.

Why SyntheticRawFlowSource for ThetaData rather than
ThetaDataLiveSource with a fake WS? Either works structurally;
the synthetic source is the deterministic stand-in this Phase
2.3.2 test pattern uses. The test pins the specific property —
that UnusualWhalesLiveSource produces RawPrints that fuse
correctly with another source — without requiring two layers of
WS-mock plumbing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import (
    FusionParams,
    TierThresholds,
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.fusion import SourceFusion
from uoa_detector.sources.synthetic import SyntheticRawFlowSource
from uoa_detector.sources.unusual_whales.live import (
    FlowSubscription,
    UnusualWhalesLiveSource,
)

# ---------------------------------------------------------------------------
# Fakes (lifted from test_unusual_whales_live.py for self-containment)
# ---------------------------------------------------------------------------


class _FakeWebSocket:
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
    def __init__(self, sockets: list[_FakeWebSocket]) -> None:
        self._sockets = list(sockets)

    async def __call__(
        self, url: str, headers: dict[str, str],
    ) -> _FakeWebSocket:
        del url, headers
        return self._sockets.pop(0)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_BASE_TS = datetime(2024, 1, 15, 15, 30, 0, tzinfo=UTC)


def _uw_settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings(
        rate_limit_requests_per_second=10.0,
        live_reconnect_max_attempts=0,  # one connection, one frame
        live_reconnect_initial_backoff_s=0.001,
        live_reconnect_max_backoff_s=0.01,
        cache_ttl=UnusualWhalesProviderCacheTTL(),
    )


def _fusion_params() -> FusionParams:
    return FusionParams(
        window_ms=500,
        timestamp_skew_tolerance_ms=100,
        stalled_source_timeout_ms=2000,
        allowed_lateness_ms=200,
        tier_thresholds=TierThresholds(
            unanimous_min_sources=2,
            majority_fraction=0.5,
            premium_disagreement_tolerance_pct=0.05,
        ),
    )


def _make_uw_flow_event() -> dict[str, Any]:
    """Build a UW flow event matching the synthetic ThetaData print."""
    return {
        "id": "uw_evt_42",
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
        "spot_price": "151.25",
    }


def _make_thetadata_raw_print() -> RawPrint:
    """A RawPrint matching the UW event (same ticker/strike/timestamp)."""
    return RawPrint(
        source_id="thetadata",
        source_event_id="td-evt-99",
        timestamp=_BASE_TS,
        ticker="AAPL",
        option_type="call",
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=32,
        spot_price=Decimal("151.25"),
        premium_paid=Decimal("12345.00"),
        option_price=Decimal("1.50"),
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        implied_volatility=0.25,
        open_interest=1000,
        is_iso=False,
    )


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unusual_whales_with_synthetic_thetadata_yields_unanimous() -> None:
    """Phase 3.3.3 acceptance pin: a UW print that matches a parallel
    ThetaData print → ``confidence_tier='unanimous'``.

    This validates UnusualWhalesLiveSource integrates correctly into
    multi-source fusion. Once both sources observe the same trade
    (same ticker, same timestamp within window, same direction), the
    fusion outputs a single OptionsPrint with the unanimous tier.
    """
    # UW source — fake WS yields one flow event then EOF
    uw_frame = json.dumps({
        "type": "flow_alert",
        "data": _make_uw_flow_event(),
    })
    uw_ws = _FakeWebSocket(
        frames=[uw_frame],
        raise_after=ConnectionError("eof"),
    )
    uw_factory = _ScriptedFactory(sockets=[uw_ws])
    uw_source = UnusualWhalesLiveSource(
        api_key=SecretStr("uw_test_key_xyz"),
        settings=_uw_settings(),
        subscriptions=[FlowSubscription(ticker="AAPL")],
        ws_url="wss://test/v1/ws",
        connect_factory=uw_factory,
    )

    # ThetaData stand-in — synthetic source with matching RawPrint
    td_source = SyntheticRawFlowSource(
        "thetadata",
        [_make_thetadata_raw_print()],
    )

    # Multi-source fusion
    fusion = SourceFusion(
        sources=[uw_source, td_source],
        params=_fusion_params(),
    )

    out = [p async for p in fusion.stream()]

    assert len(out) == 1, (
        f"expected exactly one canonical print from two-source fusion, "
        f"got {len(out)}"
    )
    canonical = out[0]
    assert canonical.ticker == "AAPL"
    assert canonical.strike == Decimal("150.00")
    # The acceptance-doc pin:
    assert canonical.source_agreement.confidence_tier == "unanimous", (
        f"expected 'unanimous' (both sources saw the same print), "
        f"got {canonical.source_agreement.confidence_tier!r}"
    )
    # The exchanges_seen tuple should reflect both sources observed CBOE
    assert "CBOE" in canonical.source_agreement.exchanges_seen


@pytest.mark.asyncio
async def test_uw_disagreeing_with_thetadata_not_unanimous() -> None:
    """Sanity pin: when the two sources disagree on a key field
    (premium with > 5% drift), fusion does NOT produce 'unanimous'.

    This pins that the unanimous tier requires actual agreement, not
    just two sources delivering events into the same window.
    """
    # UW reports a much higher premium than ThetaData
    uw_event = _make_uw_flow_event()
    uw_event["premium"] = "100000.00"  # 8x the synthetic value
    uw_frame = json.dumps({"type": "flow_alert", "data": uw_event})
    uw_ws = _FakeWebSocket(
        frames=[uw_frame],
        raise_after=ConnectionError("eof"),
    )
    uw_factory = _ScriptedFactory(sockets=[uw_ws])
    uw_source = UnusualWhalesLiveSource(
        api_key=SecretStr("uw_test_key_xyz"),
        settings=_uw_settings(),
        subscriptions=[FlowSubscription(ticker="AAPL")],
        ws_url="wss://test/v1/ws",
        connect_factory=uw_factory,
    )
    td_source = SyntheticRawFlowSource(
        "thetadata", [_make_thetadata_raw_print()],
    )
    fusion = SourceFusion(
        sources=[uw_source, td_source],
        params=_fusion_params(),
    )

    out = [p async for p in fusion.stream()]
    assert len(out) == 1
    # Disagreement → NOT unanimous (could be 'majority', 'conflicted',
    # depending on exact agreement rules; we just pin the negative).
    assert out[0].source_agreement.confidence_tier != "unanimous"


# Mark unused imports as referenced (asyncio used inside the fakes)
_ = asyncio
_ = timedelta
