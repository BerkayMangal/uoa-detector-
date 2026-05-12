"""Unusual Whales IVHistoryProvider implementation.

Phase 3.3.3.4: implements ``IVHistoryProvider`` Protocol backed by
UW's IV-rank endpoint. UW exposes per-contract IV history with
rank/percentile vs the trailing 252-day distribution; we surface
the snapshot nearest the requested ``at`` timestamp.

The provider is pure data-shipping. Module 24 (IV Exhaustion
Filter) consumes the typed snapshot and applies penalty rules
(e.g. 'IV expanded > 30% intraday → penalise late entry') with
profile-tunable thresholds.

UW endpoint shape (Phase 3.3.9.2 — migrated; the per-contract IV
endpoint was retired and IV is now published at ticker granularity
because UW's underlying source is the same series for every strike
on a given expiry):

  GET /api/stock/{ticker}/iv-rank
    → {
        "data": [
          {
            "date": "2026-05-11",
            "updated_at": "2026-05-11T22:35:03.362289Z",
            "volatility": "0.2263",
            "iv_rank_1y": "34.6915",
            "close": "293.32"
          },
          ...
        ]
      }

decision (Phase 3.3.9.2 — IVRankSnapshot DTO unchanged):
  Phase 3.4 stage code is frozen (M24 only consumes
  ``implied_volatility`` and ``iv_rank_252d`` from the DTO). Map
  the new UW schema:
    - ``implied_volatility = volatility`` (string → float)
    - ``iv_rank_252d = iv_rank_1y`` (semantic equivalent;
      252 trading days ≈ 1 calendar year, same percentile basis)
    - ``iv_percentile_252d`` → fall back to ``iv_rank_1y`` value
      (UW no longer publishes a separate percentile; close enough
      for the legacy field's "rank percentile" semantic)
    - ``iv_change_intraday_pct`` → ``None`` (no longer published;
      M24 doesn't consume this field)
    - ``as_of`` → ``updated_at`` (preferred; intraday timestamp)
      or fall back to ``date`` cast to 21:00 UTC (US session close)

decision (cache key is now ticker, not OCC symbol):
  Same IV-rank series applies to every contract on the same
  underlying. Caching by OCC symbol would inflate the cache
  (one entry per strike) for identical data; the new key is
  ``ticker.upper()``. Legacy fixture compat is preserved via
  ``_row_*`` helpers that accept either schema.

decision (nearest-snapshot selection):
  Unchanged from Phase 3.3.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as _date
from datetime import time as _time
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
        key = ticker.upper()
        rows = await self._cache.get_or_fetch(
            key,
            loader=lambda: self._fetch(ticker),
        )
        return self._select_nearest(
            rows,
            ticker=ticker,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            at=at,
        )

    async def _fetch(self, ticker: str) -> list[dict[str, Any]]:
        path = f"/api/stock/{ticker.upper()}/iv-rank"
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
                row_at = _row_as_of(row)
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
    """Phase 3.3.9.2: maps both legacy and new UW row schemas."""
    try:
        as_of = _row_as_of(row)
        iv = _row_iv(row)
    except (KeyError, ValueError, TypeError):
        return None
    rank = _row_rank(row)
    percentile = _row_percentile(row, rank)
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


def _row_as_of(row: dict[str, Any]) -> datetime:
    """Phase 3.3.9.2: prefer ``updated_at``, fall back to ``date`` at
    21:00 UTC, legacy ``as_of`` ISO string also tolerated.
    """
    if "updated_at" in row:
        return _parse_iso_utc(str(row["updated_at"]))
    if "as_of" in row:
        return _parse_iso_utc(str(row["as_of"]))
    date_str = str(row["date"])
    day = _date.fromisoformat(date_str)
    return datetime.combine(day, _time(hour=21, minute=0, tzinfo=UTC))


def _row_iv(row: dict[str, Any]) -> float:
    """Phase 3.3.9.2: prefer ``volatility``, fall back to legacy
    ``implied_volatility``. Both string-or-float tolerated.
    """
    raw = row["volatility"] if "volatility" in row else row["implied_volatility"]
    return float(raw)


def _row_rank(row: dict[str, Any]) -> float | None:
    """Phase 3.3.9.2: prefer ``iv_rank_1y`` (new), fall back to
    legacy ``iv_rank_252d``. Returns None if neither present.
    """
    if "iv_rank_1y" in row:
        return _opt_float_or_str(row.get("iv_rank_1y"))
    return _opt_float_or_str(row.get("iv_rank_252d"))


def _row_percentile(row: dict[str, Any], rank: float | None) -> float | None:
    """Phase 3.3.9.2: new endpoint doesn't publish percentile; fall
    back to rank value (same percentile-of-distribution semantic).
    """
    if "iv_percentile_252d" in row:
        return _opt_float_or_str(row.get("iv_percentile_252d"))
    return rank


def _opt_float_or_str(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


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
