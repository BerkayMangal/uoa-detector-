"""Unusual Whales DealerPositioningProvider implementation.

Phase 3.3.3.4: implements ``DealerPositioningProvider`` Protocol
backed by UW's Greek-Exposure (GEX) endpoint. UW publishes daily-
estimated dealer net gamma per (ticker, strike); we surface the
nearest snapshot to the requested ``at`` timestamp.

The provider is pure data-shipping: no business logic. Module 21
(Dealer Gamma Overlay) consumes the ``DealerPositioning`` DTO and
applies its own scoring (gamma_score, GAMMA_ACCELERATION_RISK
flag) using profile-tunable thresholds.

UW endpoint shape (documented at fetch time, kept centralised here
for one-place schema-evolution fix):

  GET /api/stock/{ticker}/greek-exposure-strike
    → {
        "data": [
          {
            "strike": "150.0",
            "as_of": "2024-01-15T15:30:00Z",
            "net_gamma": "-12345678.0",
            "flow_direction": "accumulating"
          },
          ...
        ]
      }

decision (UW gamma sign convention):
  UW publishes ``net_gamma`` as signed dollars per 1% spot move
  (negative = dealers short gamma). Same convention as
  ``DealerPositioning.net_gamma_dollars``. No transformation
  needed, only field rename.

decision (nearest-strike fallback):
  UW returns gamma per discrete strike. If the requested strike
  isn't on UW's list, we look for the nearest strike within
  $0.01 (treating Decimal equality as a soft match). If nothing
  matches, return None — Module 21 falls back to a neutral score
  via NoOpDealerPositioningProvider semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.dealer_positioning import DealerPositioning
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


class UnusualWhalesDealerGammaProvider:
    """``DealerPositioningProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=settings.cache_ttl.dealer_gamma_seconds,
        )

    async def net_gamma_at(
        self,
        ticker: str,
        strike: Decimal,
        at: datetime,
    ) -> DealerPositioning | None:
        """Return the dealer-positioning snapshot at ``at``, or None."""
        del at  # UW returns daily snapshots; date is not part of the key
        key = ticker.upper()
        rows = await self._cache.get_or_fetch(
            key,
            loader=lambda: self._fetch(ticker),
        )
        return self._select_strike(rows, ticker=ticker, strike=strike)

    async def _fetch(self, ticker: str) -> list[dict[str, Any]]:
        path = f"/api/stock/{ticker.upper()}/greek-exposure-strike"
        resp = await self._client.request_json(path)
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    def _select_strike(
        self,
        rows: list[dict[str, Any]],
        *,
        ticker: str,
        strike: Decimal,
    ) -> DealerPositioning | None:
        for row in rows:
            try:
                row_strike = Decimal(str(row["strike"]))
            except (KeyError, ValueError, ArithmeticError):
                continue
            if abs(row_strike - strike) < Decimal("0.01"):
                return _row_to_dealer_positioning(row, ticker=ticker)
        return None


def _row_to_dealer_positioning(
    row: dict[str, Any], *, ticker: str,
) -> DealerPositioning | None:
    """Map one UW gamma-strike row to a DealerPositioning DTO."""
    try:
        strike = Decimal(str(row["strike"]))
        as_of_raw = str(row["as_of"])
        as_of = _parse_iso_utc(as_of_raw)
        net_gamma = Decimal(str(row["net_gamma"]))
        flow_direction_raw = str(row.get("flow_direction", "neutral")).lower()
    except (KeyError, ValueError, ArithmeticError):
        return None
    if flow_direction_raw not in ("accumulating", "distributing", "neutral"):
        flow_direction_raw = "neutral"
    return DealerPositioning(
        ticker=ticker.upper(),
        strike=strike,
        as_of=as_of,
        net_gamma_dollars=net_gamma,
        flow_direction=flow_direction_raw,
    )


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
