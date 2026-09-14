"""Unusual Whales IVHistoryProvider implementation.

Phase 3.3.3.4 introduced the provider. Phase 3.9.6 moves it onto the
live-verified ticker-level endpoint (contract:
``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.3). The
per-contract ``/api/option-contract/{symbol}/iv-rank`` path used before
returned HTTP 404 live on 2026-09-14.

The provider is pure data-shipping. Module 24 (IV Exhaustion Filter)
consumes the typed snapshot and applies its profile-tunable rules.

UW endpoint shape (live capture, 2026-09-14):

  GET /api/stock/{ticker}/iv-rank?date=YYYY-MM-DD
    → {
        "data": [
          {
            "close": "326.57",
            "date": "2026-09-10",
            "updated_at": "2026-09-10T22:35:01.459306Z",
            "volatility": "0.258",
            "iv_rank_1y": "51.5671"
          },
          ...
        ]
      }

  One row per trading day, ascending by date. ``date=D`` returns rows
  dated on or before D. Values are strings. ``updated_at`` is the
  publication time and lands after the 16:00 ET close.

decision (as-of row selection, no look-ahead):
  ``D`` is the ET date of ``at``. A row dated before ``D`` is eligible.
  A row dated ``D`` is eligible only once ``updated_at <= at``, because
  the day's row is published after the close and an intraday event
  must not see it. A row dated after ``D`` is never eligible. The
  latest eligible row by ``(date, updated_at)`` wins, so the result
  does not depend on response order.

decision (field mapping):
  - ``iv_rank_252d = float(iv_rank_1y)`` with no scaling. Live values
    are on the 0..100 scale (``"40.3312"``). The vendored spec example
    ``"0.65"`` is wrong against live.
  - ``implied_volatility = float(volatility)``.
  - ``as_of = updated_at`` (UTC).
  - ``iv_percentile_252d`` and ``iv_change_intraday_pct`` are ``None``.
    UW publishes neither.
  - ``strike``, ``expiry`` and ``option_type`` are echoed from the call
    for identity. The series is per underlying, not per contract.

decision (malformed rows):
  A row whose ``date`` or ``updated_at`` does not parse is skipped.
  Without them neither eligibility nor ``as_of`` can be set without
  inventing a timestamp. A row whose ``volatility`` does not parse is
  skipped, so the next latest eligible row is used. An unparseable
  ``iv_rank_1y`` keeps the row with ``iv_rank_252d = None``, which M24
  treats as no IV history.

decision (cache key ``(ticker, ET date)``):
  The request carries ``date``, so the response depends only on the
  ticker and the ET date of ``at``. Calls and puts on any contract of
  the same underlying and day share one fetch. Row selection runs per
  call after the cache, so two events on the same day at different
  times can see different rows.

decision (not found):
  HTTP 404/422 (``UnusualWhalesNotFoundError``) means no series for
  this ticker and date, and maps to ``None``. The empty result is cached
  for the TTL like any other response. Auth errors, 5xx after retries
  and an open breaker still raise, so a bad key never reads as no data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from uoa_detector.providers.iv_history import IVRankSnapshot
from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from decimal import Decimal

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


# US equity session timezone. The ET date of ``at`` drives the request
# ``date`` param, the cache key and the same-day publication rule.
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class _IVRow:
    """One parsed, usable row of the ``/iv-rank`` series."""

    day: date
    updated_at: datetime
    implied_volatility: float
    iv_rank_1y: float | None


class UnusualWhalesIVHistoryProvider:
    """``IVHistoryProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        self._cache: TTLCache[tuple[_IVRow, ...]] = TTLCache(
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
        """Return the latest IV-rank row knowable at ``at``, or ``None``."""
        at_utc = _as_utc(at)
        symbol = ticker.upper()
        et_date = at_utc.astimezone(_ET).date()
        rows = await self._cache.get_or_fetch(
            (symbol, et_date),
            loader=lambda: self._fetch(symbol, et_date),
        )
        row = _select_as_of(rows, et_date=et_date, at=at_utc)
        if row is None:
            return None
        return IVRankSnapshot(
            ticker=symbol,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            as_of=row.updated_at,
            implied_volatility=row.implied_volatility,
            iv_rank_252d=row.iv_rank_1y,
            iv_percentile_252d=None,
            iv_change_intraday_pct=None,
        )

    async def _fetch(self, ticker: str, et_date: date) -> tuple[_IVRow, ...]:
        path = f"/api/stock/{ticker}/iv-rank"
        try:
            resp = await self._client.request_json(
                path, params={"date": et_date.isoformat()},
            )
        except UnusualWhalesNotFoundError:
            return ()
        data = resp.get("data", [])
        if not isinstance(data, list):
            return ()
        parsed = (_parse_row(item) for item in data if isinstance(item, dict))
        return tuple(row for row in parsed if row is not None)


def _select_as_of(
    rows: tuple[_IVRow, ...],
    *,
    et_date: date,
    at: datetime,
) -> _IVRow | None:
    """Latest row by ``(date, updated_at)`` that was knowable at ``at``."""
    best: _IVRow | None = None
    for row in rows:
        if row.day > et_date:
            continue
        if row.day == et_date and row.updated_at > at:
            continue
        if best is None or (row.day, row.updated_at) > (best.day, best.updated_at):
            best = row
    return best


def _parse_row(raw: dict[str, Any]) -> _IVRow | None:
    day = _parse_date(raw.get("date"))
    updated_at = _parse_utc(raw.get("updated_at"))
    implied_volatility = _parse_float(raw.get("volatility"))
    if day is None or updated_at is None or implied_volatility is None:
        return None
    return _IVRow(
        day=day,
        updated_at=updated_at,
        implied_volatility=implied_volatility,
        iv_rank_1y=_parse_float(raw.get("iv_rank_1y")),
    )


def _as_utc(at: datetime) -> datetime:
    """Normalise ``at`` to UTC; a naive value is taken as UTC."""
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_utc(parsed)


def _parse_float(value: object) -> float | None:
    """Parse a UW numeric field (live: a string); ``None`` if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None
