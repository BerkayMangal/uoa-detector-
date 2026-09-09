"""Phase 3.7.2 tests for the shared ``map_uw_flow_event`` pure function.

The mapper normalises a UW flow object — from either the WS feed or the
``/api/option-flow/recent`` REST endpoint — into a canonical ``RawPrint``.
These tests pin the REST-shape behaviour and the field-name robustness the
Phase 3.7 acceptance contract requires; the WS behaviour is separately
pinned by ``test_unusual_whales_live.py`` (unchanged, D10).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from uoa_detector.sources.unusual_whales.live import map_uw_flow_event


def _rest_row(**overrides: Any) -> dict[str, Any]:
    """A recent-flow REST row. Mirrors the WS shape but with
    ``side_classification`` instead of ``side`` (per sector_peer.py)."""
    base: dict[str, Any] = {
        "id": "rest_evt_1",
        "ticker": "aapl",
        "executed_at": "2024-01-15T15:30:00Z",
        "option_type": "call",
        "strike": "150.00",
        "expiry": "2024-02-16",
        "premium": "12345.00",
        "price": "1.50",
        "bid": "1.45",
        "ask": "1.55",
        "side_classification": "bullish",
        "open_interest": 1000,
        "implied_volatility": 0.25,
        "exchange": "CBOE",
        "alert_type": "sweep",
    }
    base.update(overrides)
    return base


def test_rest_row_maps_to_rawprint() -> None:
    """A well-formed REST row → a fully-populated RawPrint."""
    rp = map_uw_flow_event(
        _rest_row(),
        source_id="unusual_whales",
        source_event_id_fallback="uw-fallback-x",
    )
    assert rp is not None
    assert rp.source_id == "unusual_whales"
    assert rp.source_event_id == "uw-rest_evt_1"
    assert rp.ticker == "AAPL"
    assert rp.option_type == "call"
    assert rp.strike == Decimal("150.00")
    assert rp.premium_paid == Decimal("12345.00")
    assert rp.option_price == Decimal("1.50")
    assert rp.bid == Decimal("1.45")
    assert rp.ask == Decimal("1.55")
    assert rp.open_interest == 1000
    assert rp.implied_volatility == 0.25
    assert rp.exchange == "CBOE"
    assert rp.timestamp == datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    assert "uw:sweep" in rp.source_tags
    # No spot in the recent-flow row → 0 + tag (same as WS).
    assert rp.spot_price == Decimal("0")
    assert "uw:no-spot" in rp.source_tags


def test_side_classification_directional_maps_to_unknown_fill() -> None:
    """`side_classification` carries a *direction* (bullish/bearish/neutral),
    not a *fill* label — so fill_side degrades to 'unknown', never fabricated.
    """
    for direction in ("bullish", "bearish", "neutral"):
        rp = map_uw_flow_event(
            _rest_row(side_classification=direction),
            source_id="unusual_whales",
            source_event_id_fallback="uw-fallback-x",
        )
        assert rp is not None
        assert rp.fill_side == "unknown"


def test_side_field_name_still_honoured_over_rest() -> None:
    """If a REST row ever carries a fill-style `side`, it is mapped; and
    `side` wins over `side_classification` when both are present."""
    rp = map_uw_flow_event(
        _rest_row(side="ASK"),
        source_id="unusual_whales",
        source_event_id_fallback="uw-fallback-x",
    )
    assert rp is not None
    assert rp.fill_side == "at_ask"  # 'side' honoured, not the directional one


def test_missing_required_field_logs_keys_and_returns_none(
    caplog: Any,
) -> None:
    """A row missing a required field → None + an ERROR naming the actual
    keys, so a shape mismatch is a named one-line fix (never silent)."""
    row = _rest_row()
    del row["strike"]
    with caplog.at_level(logging.ERROR):
        rp = map_uw_flow_event(
            row,
            source_id="unusual_whales",
            source_event_id_fallback="uw-fallback-x",
        )
    assert rp is None
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    # The actual keys of the row appear so the operator can see the shape.
    assert "row keys=" in msg
    assert "strike" not in msg.split("row keys=")[1].split("required=")[0]
    # 'strike' is still named as a required field.
    assert "'strike'" in msg
    # A representative surviving key is listed.
    assert "'ticker'" in msg


def test_fallback_id_used_when_no_id_or_event_id() -> None:
    row = _rest_row()
    del row["id"]
    rp = map_uw_flow_event(
        row,
        source_id="unusual_whales",
        source_event_id_fallback="uw-rest-fallback-7",
    )
    assert rp is not None
    assert rp.source_event_id == "uw-rest-fallback-7"


def test_event_id_alias_honoured() -> None:
    row = _rest_row()
    del row["id"]
    row["event_id"] = "alias_99"
    rp = map_uw_flow_event(
        row,
        source_id="unusual_whales",
        source_event_id_fallback="uw-fallback-x",
    )
    assert rp is not None
    assert rp.source_event_id == "uw-alias_99"


def test_expired_contract_dropped() -> None:
    """A trade after expiry → negative DTE → dropped (returns None)."""
    rp = map_uw_flow_event(
        _rest_row(executed_at="2024-03-01T15:30:00Z", expiry="2024-02-16"),
        source_id="unusual_whales",
        source_event_id_fallback="uw-fallback-x",
    )
    assert rp is None


def test_custom_source_id_written_through() -> None:
    rp = map_uw_flow_event(
        _rest_row(),
        source_id="unusual_whales-rest",
        source_event_id_fallback="uw-fallback-x",
    )
    assert rp is not None
    assert rp.source_id == "unusual_whales-rest"
