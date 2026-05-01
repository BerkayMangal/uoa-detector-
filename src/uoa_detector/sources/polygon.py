"""Polygon ``RawFlowSource`` adapter — stub for Phase 3 implementation.

Polygon delivers OPRA tape direct: every options trade on every US options
exchange, plus NBBO updates. The data shape is rich and source-specific,
which is why fusion happens at the ``OptionsPrint`` boundary, not the
``RawPrint`` boundary.

Expected per-trade fields from Polygon's OPRA stream
(https://polygon.io/docs/options/get_v3_trades__optionsticker):
  - ``participant_timestamp`` (ns since epoch): when the exchange's matching
    engine timestamped the trade. Maps to ``RawPrint.timestamp``.
  - ``sip_timestamp`` (ns since epoch): when SIP received it from the venue.
    Used to compute SIP-vs-participant skew, an indicator of feed quality
    rather than scoring; not stored on ``RawPrint`` directly. Phase 3 may
    surface this through ``SourceAgreement.timestamp_skew_ms`` for
    Polygon-internal observability.
  - ``exchange_id`` (int): SIP exchange code (1=NYSE Arca, 2=NASDAQ OMX BX,
    11=ISE, 12=CBOE, …). Translates to ``RawPrint.exchange`` via the
    OPRA exchange-code table.
  - ``conditions[]`` (list[int]): trade condition codes. Most relevant for
    UOA: code 233 = ISO sweep on most exchanges; codes 232/235 are also
    sweep variants. Maps to ``RawPrint.is_iso``.
  - ``price``, ``size``: trade price (per contract, not per share) and
    contract count. Map to ``RawPrint.option_price`` and ``RawPrint.size``
    (a field that may be added in Phase 3 — Phase 2 already derives
    ``premium_paid = price * size * 100`` upstream).

Fields NOT supplied by Polygon's trade stream (must come from elsewhere):
  - ``implied_volatility``, ``open_interest`` — Polygon provides these via
    snapshot endpoints (separate API calls), not the trade stream. Adapters
    that wire Polygon as a flow source should pair it with an
    ``IBKRQuoteSource`` or a ``PolygonQuoteSource`` (Phase 3) supplying
    these fields, or the fusion layer will raise ``DataSourceError`` per
    the v2.3.3 missing-field contract.

Phase 3 wiring needs (TODOs, not implemented in Phase 2):
  - Polygon WebSocket client for real-time OPRA stream (T.\\* and Q.\\*
    channels). Use ``websockets`` or Polygon's official SDK.
  - Backoff/reconnection policy on disconnect — Polygon recommends
    exponential backoff capped at 60s.
  - Replay support via the REST trades endpoint for backtest harness.
  - Symbol-universe filtering: subscribing to all OPRA contracts is
    expensive; profile-driven ticker allowlist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from uoa_detector.errors import DataSourceError

if TYPE_CHECKING:
    from uoa_detector.domain.raw_print import RawPrint


class PolygonConfig(BaseModel):
    """Connection settings for the Polygon adapter.

    Phase 2 stores config only — no live connection is opened. Phase 3 will
    use ``api_key`` to authenticate against ``base_url`` and stream from
    ``ws_url``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr = Field(description="Polygon API key (env: UOA_POLYGON__API_KEY)")
    base_url: str = Field(
        default="https://api.polygon.io",
        description="REST base URL — used for snapshot/replay endpoints in Phase 3",
    )
    ws_url: str = Field(
        default="wss://socket.polygon.io/options",
        description="WebSocket URL for the OPRA real-time stream",
    )
    ticker_allowlist: tuple[str, ...] = Field(
        default=(),
        description=(
            "If non-empty, restrict subscriptions to these tickers. Empty = "
            "subscribe to T.* (all OPRA trades), which is firehose-volume."
        ),
    )


class PolygonFlowSource:
    """``RawFlowSource`` adapter for Polygon OPRA tape.

    Phase 2 STUB: ``stream()`` raises ``DataSourceError``. Phase 3 will
    implement the WebSocket consumer and per-trade ``RawPrint`` materialisation.
    """

    source_id = "polygon"

    def __init__(self, config: PolygonConfig) -> None:
        self._config = config
        self._closed = False

    async def stream(self) -> AsyncIterator[RawPrint]:
        """Yield ``RawPrint`` events from Polygon's OPRA trade stream.

        Phase 2: not implemented. The pipeline must construct a
        ``SyntheticRawFlowSource`` for testing or wait for Phase 3.
        """
        msg = (
            "PolygonFlowSource is a Phase 2 stub — HTTP/WebSocket client not "
            "yet wired. Use SyntheticRawFlowSource for testing, or wait for "
            "Phase 3 which adds the live OPRA stream consumer."
        )
        raise DataSourceError(msg)
        # Make this an async generator structurally (unreachable but required
        # so the function's return type is AsyncIterator[RawPrint] at runtime).
        yield  # pragma: no cover  # type: ignore[unreachable]

    async def close(self) -> None:
        """Idempotent close. Phase 3 will tear down the WebSocket here."""
        self._closed = True
