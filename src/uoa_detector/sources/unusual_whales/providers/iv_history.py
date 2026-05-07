"""Unusual Whales IVHistoryProvider implementation.

Phase 3.3.3.4: implements ``IVHistoryProvider`` Protocol backed by
UW's IV-rank endpoint. UW exposes per-contract IV history with
rank/percentile vs the trailing 252-day distribution; we surface
the snapshot nearest the requested ``at`` timestamp.

The provider is pure data-shipping. Module 24 (IV Exhaustion
Filter) consumes the typed snapshot and applies penalty rules
(e.g. 'IV expanded > 30% intraday → penalise late entry') with
profile-tunable thresholds.

UW endpoint shape (centralised here for schema-evolution fixes):

  GET /api/option-contract/{symbol}/iv-rank
    → {
        "data": [
          {
            "as_of": "2024-01-15T15:30:00Z",
            "implied_volatility": 0.4231,
            "iv_rank_252d": 67.5,
            "iv_percentile_252d": 72.1,
            "iv_change_intraday_pct": 12.3
          },
          ...
        ]
      }

decision (per-contract OCC symbol as cache key):
  UW's IV-rank endpoint is keyed by full OCC symbol
  (ticker+expiry+right+strike). We build the symbol once per call
  and use it as the cache key. Same call → same symbol → cache
  hit during TTL window.

decision (nearest-snapshot selection):
  UW returns snapshots at minute granularity; we pick the one
  closest in time to ``at``, choosing the most recent one if
  ``at`` falls between two snapshots. None if the response is
  empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from uoa_detector.providers.iv_history import IVRankSnapshot
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from datetime import date

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


class UnusualWhalesIVHistoryProvider:
    """``IVHistoryProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        self._cache: TTLCache[list[dict[str, Any]]] = TTLCache(
            ttl_seconds=settings.cache_ttl.iv_history_seconds,
        )

    async def iv_rank_at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> IVRankSnapshot | None:
        """Return the IV-rank snapshot nearest to ``at``."""
        symbol = _occ_symbol(ticker, expiry, option_type, strike)
        rows = await self._cache.get_or_fetch(
            symbol,
            loader=lambda: self._fetch(symbol),
        )
        return self._select_nearest(
            rows,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            at=at,
        )

    async def _fetch(self, symbol: str) -> list[dict[str, Any]]:
        path = f"/api/option-contract/{symbol}/iv-rank"
        resp = await self._client.request_json(path)
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    def _select_nearest(
        self,
        rows: list[dict[str, Any]],
        *,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> IVRankSnapshot | None:
        if not rows:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for row in rows:
            try:
                row_at = _parse_iso_utc(str(row["as_of"]))
            except (KeyError, ValueError):
                continue
            delta = abs((row_at - at).total_seconds())
            if best is None or delta < best[0]:
                best = (delta, row)
        if best is None:
            return None
        return _row_to_iv_snapshot(
            best[1],
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
        )


def _row_to_iv_snapshot(
    row: dict[str, Any],
    *,
    ticker: str,
    strike: Decimal,
    expiry: date,
    option_type: Literal["call", "put"],
) -> IVRankSnapshot | None:
    try:
        as_of = _parse_iso_utc(str(row["as_of"]))
        iv = float(row["implied_volatility"])
    except (KeyError, ValueError, TypeError):
        return None
    rank = _opt_float(row.get("iv_rank_252d"))
    percentile = _opt_float(row.get("iv_percentile_252d"))
    change_intraday = _opt_float(row.get("iv_change_intraday_pct"))
    return IVRankSnapshot(
        ticker=ticker.upper(),
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        as_of=as_of,
        implied_volatility=iv,
        iv_rank_252d=rank,
        iv_percentile_252d=percentile,
        iv_change_intraday_pct=change_intraday,
    )


def _occ_symbol(
    ticker: str, expiry: date, option_type: Literal["call", "put"],
    strike: Decimal,
) -> str:
    """Build OCC-style symbol: TICKERYYMMDD[C|P]STRIKE.

    Strike is encoded as 1/1000-dollar units padded to 8 digits.
    Example: AAPL 2024-02-16 C 150.00 → AAPL240216C00150000.
    """
    yymmdd = f"{expiry.year % 100:02d}{expiry.month:02d}{expiry.day:02d}"
    cp = "C" if option_type == "call" else "P"
    strike_int = int(strike * Decimal(1000))
    return f"{ticker.upper()}{yymmdd}{cp}{strike_int:08d}"


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _opt_float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None
