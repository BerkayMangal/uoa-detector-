"""Tests for the Phase 2 source stubs.

The Polygon, Unusual Whales, and IBKR adapters have NO live HTTP / WebSocket
client in Phase 2 — they exist as typed shells so:

  1. The Protocol surface is locked in (``RawFlowSource`` /
     ``QuoteSnapshotSource``), so Phase 3 can swap implementations
     without touching call sites.
  2. The config types are explicit, secrets handling is isolated to
     ``SecretStr``, and CLI/env-var plumbing has somewhere to land.
  3. Anyone who tries to *use* one of these sources before Phase 3
     ships gets a clear ``DataSourceError`` with a phase marker, not
     an obscure attribute error or silent hang.

These tests verify all three of those properties.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from uoa_detector.errors import DataSourceError
from uoa_detector.sources.base import QuoteSnapshotSource, RawFlowSource
from uoa_detector.sources.ibkr_quotes import IBKRConfig, IBKRQuoteSource
from uoa_detector.sources.polygon import PolygonConfig, PolygonFlowSource
from uoa_detector.sources.unusual_whales import (
    UnusualWhalesConfig,
    UnusualWhalesFlowSource,
)

# ---------------------------------------------------------------------------
# PolygonFlowSource
# ---------------------------------------------------------------------------


def test_polygon_satisfies_raw_flow_source_protocol() -> None:
    src = PolygonFlowSource(PolygonConfig(api_key="dummy-key"))
    assert isinstance(src, RawFlowSource)
    assert src.source_id == "polygon"


def test_polygon_config_secret_is_not_in_repr() -> None:
    """``SecretStr`` ensures the API key doesn't leak into logs / tracebacks."""
    cfg = PolygonConfig(api_key="real-secret-value")
    assert "real-secret-value" not in repr(cfg)
    # Confirm the value is still retrievable via the SecretStr accessor.
    assert cfg.api_key.get_secret_value() == "real-secret-value"


def test_polygon_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        PolygonConfig(api_key="k", typo_field="oops")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_polygon_stream_raises_phase_marker() -> None:
    """Calling ``stream()`` on the Phase 2 stub raises with a clear message."""
    src = PolygonFlowSource(PolygonConfig(api_key="dummy"))
    with pytest.raises(DataSourceError, match="Phase 2 stub"):
        async for _ in src.stream():
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_polygon_close_is_idempotent() -> None:
    src = PolygonFlowSource(PolygonConfig(api_key="dummy"))
    await src.close()
    await src.close()  # second call must not raise


# ---------------------------------------------------------------------------
# UnusualWhalesFlowSource
# ---------------------------------------------------------------------------


def test_unusual_whales_satisfies_raw_flow_source_protocol() -> None:
    src = UnusualWhalesFlowSource(UnusualWhalesConfig(api_token="dummy"))
    assert isinstance(src, RawFlowSource)
    assert src.source_id == "unusual_whales"


def test_unusual_whales_config_secret_is_not_in_repr() -> None:
    cfg = UnusualWhalesConfig(api_token="leak-this-and-i-cry")
    assert "leak-this-and-i-cry" not in repr(cfg)
    assert cfg.api_token.get_secret_value() == "leak-this-and-i-cry"


def test_unusual_whales_minimum_premium_must_be_non_negative() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        UnusualWhalesConfig(api_token="k", minimum_premium_usd=-1)


@pytest.mark.asyncio
async def test_unusual_whales_stream_raises_phase_marker() -> None:
    src = UnusualWhalesFlowSource(UnusualWhalesConfig(api_token="dummy"))
    with pytest.raises(DataSourceError, match="Phase 2 stub"):
        async for _ in src.stream():
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_unusual_whales_close_is_idempotent() -> None:
    src = UnusualWhalesFlowSource(UnusualWhalesConfig(api_token="dummy"))
    await src.close()
    await src.close()


# ---------------------------------------------------------------------------
# IBKRQuoteSource
# ---------------------------------------------------------------------------


def test_ibkr_satisfies_quote_snapshot_source_protocol() -> None:
    src = IBKRQuoteSource(IBKRConfig())
    assert isinstance(src, QuoteSnapshotSource)
    assert src.source_id == "ibkr"


def test_ibkr_config_defaults_to_paper_tws_port() -> None:
    """7497 = paper TWS — chosen as default to avoid accidentally hitting
    a live account during testing.
    """
    cfg = IBKRConfig()
    assert cfg.port == 7497


def test_ibkr_config_client_id_range_validation() -> None:
    """Client IDs >= 0 and <= 999 per IBKR API constraints."""
    IBKRConfig(client_id=0)  # boundary OK
    IBKRConfig(client_id=999)  # boundary OK
    with pytest.raises(ValidationError, match="less than or equal to 999"):
        IBKRConfig(client_id=1000)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        IBKRConfig(client_id=-1)


@pytest.mark.asyncio
async def test_ibkr_snapshot_raises_phase_marker() -> None:
    src = IBKRQuoteSource(IBKRConfig())
    with pytest.raises(DataSourceError, match="Phase 2 stub"):
        await src.snapshot(
            ticker="AAPL",
            strike=Decimal("200"),
            expiry=date(2025, 7, 18),
            option_type="call",
            at=datetime(2025, 6, 11, 15, 30, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_ibkr_close_is_idempotent() -> None:
    src = IBKRQuoteSource(IBKRConfig())
    await src.close()
    await src.close()
