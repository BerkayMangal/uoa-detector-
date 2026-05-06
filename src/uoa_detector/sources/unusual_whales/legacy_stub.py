"""Unusual Whales ``RawFlowSource`` adapter — stub for Phase 3 implementation.

Unusual Whales is a labeled aggregator: they consume the OPRA tape, run
their own classification (sweep/block/ISO detection, UOA flagging, dark
pool correlation), and republish enriched events. This means UW's stream
already carries fields that the v5 detector would otherwise have to
classify from raw Polygon data — fusion can carry these labels through
to scoring as a corroborating signal rather than re-deriving from scratch.

Expected per-event fields from the UW flow alerts feed
(https://unusualwhales.com/api):
  - ``executed_at`` (ISO8601 UTC): trade timestamp. Maps to ``RawPrint.timestamp``.
  - ``ticker``, ``strike``, ``expiry``, ``option_type``: contract identity.
  - ``premium`` (USD): notional dollar premium of the trade. Already
    aggregated for split fills, unlike Polygon which delivers each fill
    separately. Maps to ``RawPrint.premium_paid``.
  - ``price``: option mid-price for the trade. Maps to ``RawPrint.option_price``.
  - ``size`` (contracts): contract count.
  - ``side`` ("ASK" | "BID" | "MID" | "ABOVE_ASK" | "BELOW_BID"): pre-
    computed fill side. Maps to ``RawPrint.fill_side``. UW's algorithm
    accounts for NBBO drift around trade time, which Polygon's raw-condition-
    codes approach does not.
  - ``is_iso`` (bool): UW's ISO classifier. Maps to ``RawPrint.is_iso``.
  - ``alert_type`` ("sweep" | "block" | "split" | "repeated"): UW's own
    UOA classification — useful as a feature in Phase 3+ but does not
    affect ``RawPrint`` directly. Phase 3 may surface via a
    ``raw_metadata`` dict on ``RawPrint``.
  - ``open_interest``, ``implied_volatility``: UW snapshots both at trade
    time. Map to ``RawPrint.open_interest`` and ``RawPrint.implied_volatility``.
    UW is a useful pairing partner for Polygon, which doesn't supply these
    in its trade stream.
  - ``exchange``: venue code, similar coverage to OPRA. Maps to ``RawPrint.exchange``.

Latency profile: UW publishes on a ~500-1000ms delay vs the OPRA feed
(they run their own classifier between SIP receive and republish). This
is the principal reason the fusion window default is 500ms — buckets need
to stay open long enough for UW to catch up to a Polygon trade. The
``profile.fusion.window_ms`` knob is what the operator tunes to balance
fusion accuracy vs detector latency.

Phase 3 wiring needs (TODOs, not implemented in Phase 2):
  - WebSocket client for the UW flow alerts stream (REST polling is also
    available but introduces unnecessary additional latency).
  - Token-bucket rate limiter — UW has tier-based per-minute quotas.
  - Symbol-universe filtering driven by ``profile`` rather than UW's own
    UOA filter (we want raw flow to do our own classification, not UW's
    pre-filtered alerts).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from uoa_detector.errors import DataSourceError

if TYPE_CHECKING:
    from uoa_detector.domain.raw_print import RawPrint


class UnusualWhalesConfig(BaseModel):
    """Connection settings for the Unusual Whales adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_token: SecretStr = Field(
        description="UW API token (env: UOA_UNUSUAL_WHALES__API_TOKEN)",
    )
    base_url: str = Field(
        default="https://api.unusualwhales.com",
        description="REST base URL for snapshot/historical endpoints",
    )
    ws_url: str = Field(
        default="wss://api.unusualwhales.com/socket",
        description="WebSocket URL for the live flow alerts stream",
    )
    ticker_allowlist: tuple[str, ...] = Field(
        default=(),
        description=(
            "If non-empty, subscribe only to these tickers. Empty = subscribe "
            "to all flow events (rate-limited by UW tier)."
        ),
    )
    minimum_premium_usd: int = Field(
        default=0,
        ge=0,
        description=(
            "Server-side filter: drop events below this premium. Set to 0 to "
            "receive everything; the v5 detector applies its own thresholds "
            "downstream so this should usually stay 0."
        ),
    )


class UnusualWhalesFlowSource:
    """``RawFlowSource`` adapter for the Unusual Whales flow alerts stream.

    Phase 2 STUB: ``stream()`` raises ``DataSourceError``. Phase 3 will
    implement the WebSocket consumer and per-event ``RawPrint`` materialisation.
    """

    source_id = "unusual_whales"

    def __init__(self, config: UnusualWhalesConfig) -> None:
        self._config = config
        self._closed = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield ``RawPrint`` events from UW's flow alerts stream.

        Phase 2: not implemented. The pipeline must construct a
        ``SyntheticRawFlowSource`` for testing or wait for Phase 3.
        """
        msg = (
            "UnusualWhalesFlowSource is a Phase 2 stub — HTTP/WebSocket "
            "client not yet wired. Use SyntheticRawFlowSource for testing, "
            "or wait for Phase 3."
        )
        raise DataSourceError(msg)
        yield  # pragma: no cover  # type: ignore[unreachable]

    async def close(self) -> None:
        """Idempotent close. Phase 3 will tear down the WebSocket here."""
        self._closed = True
