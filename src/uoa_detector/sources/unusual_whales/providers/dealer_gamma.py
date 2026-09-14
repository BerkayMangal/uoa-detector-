"""Unusual Whales DealerPositioningProvider implementation.

Phase 3.3.3.4 introduced this provider. Phase 3.9.5 re-pointed it at
the two live-verified UW endpoints (contract:
``docs/phase-3.9-uw-endpoint-correction-acceptance.md`` §3.2). The
provider is pure data-shipping: Module 21 (Dealer Gamma Overlay)
applies its own scoring (short-gamma threshold, flip proximity) from
the profile.

UW endpoint shapes (verified live 2026-09-14, kept centralised here
for one-place schema-evolution fixes):

  GET /api/stock/{ticker}/spot-exposures?date=YYYY-MM-DD
    -> {"data": [
         {"time": "2026-09-11T19:59:59.000000Z",
          "start_time": "2026-09-11T19:59:00.000000Z",
          "ticker": "AAPL", "price": "332.24",
          "gamma_per_one_percent_move_oi": "3740943365.4",
          "gamma_per_one_percent_move_vol": "...",
          "gamma_per_one_percent_move_dir": "...",
          "charm_per_one_percent_move_*": "...",
          "vanna_per_one_percent_move_*": "..."},
         ...]}
    About 400-560 per-minute rows for the date (10:30Z to 20:00Z).

  GET /api/stock/{ticker}/greek-exposure/strike?date=YYYY-MM-DD
    -> {"data": [
         {"date": "2026-09-11", "strike": "325",
          "call_gex": "329818.0540", "put_gex": "-132708.8386",
          "call_delta": "...", "put_delta": "...",
          "call_charm": "...", "put_charm": "...",
          "call_vanna": "...", "put_vanna": "..."},
         ...]}
    One row per listed strike. ``put_gex`` is published pre-signed
    (<= 0), so the per-strike net is the plain sum.

decision (net gamma from spot-exposures, USD per 1% move):
  ``DealerExposureAggregate.net_gamma_dollars`` and the profile's
  ``short_gamma_threshold`` are in USD per 1% spot move.
  ``gamma_per_one_percent_move_oi`` is the only live field in that
  unit. The aggregate takes it from the latest row whose ``time`` is
  at or before ``at``. Rows are sorted by parsed ``time``; response
  order is not trusted. ``as_of`` is that row's ``time``. When no row
  is at or before ``at`` (an event before the first snapshot of its
  ET date), the aggregate is None.

decision (flip strike from greek-exposure/strike, share gamma):
  ``call_gex + put_gex`` per strike is share gamma (gamma x OI x 100),
  not USD. Dollar gamma per strike is that value times one common
  ``0.01 * spot**2`` factor, so the zero crossing of the cumulative
  curve is the same in either unit. ``_find_flip_strike`` is unchanged:
  the first cumulative sign change walking strikes ascending, not
  interpolated. NotFound or no usable strike rows -> ``flip_strike``
  None; the aggregate is still returned from the spot-exposure row.

decision (net_gamma_at is share gamma, no pipeline consumer):
  ``net_gamma_at`` puts ``call_gex + put_gex`` of the matching strike
  (within ``_STRIKE_MATCH_TOLERANCE``) into ``net_gamma_dollars``. That
  is share gamma, not the USD per 1% move that the
  ``DealerPositioning`` docstring names: UW does not publish a
  per-strike USD/1% figure as of a time. No pipeline stage calls
  ``net_gamma_at``. ``as_of`` is 00:00 ET of the row's date.
  ``flow_direction`` is ``"neutral"`` because UW does not publish one.

decision (ET date key, one request per endpoint per (ticker, ET date)):
  Both endpoints are queried with ``date`` = the America/New_York
  calendar date of ``at``, so an evening ET event stamped after 00:00
  UTC maps to its own session, not to the next UTC day. Each endpoint
  has its own TTLCache keyed ``(TICKER, et_date)`` with the dealer-gamma
  TTL. The whole day is fetched once and the as-of selection runs on
  the cached rows. Strike rows dated after the ET date are dropped, and
  when several dates come back only the newest one on or before the ET
  date is used, so a response that ignored ``date`` cannot leak a later
  snapshot. A row without ``date`` is labelled with the requested date.

decision (errors):
  ``UnusualWhalesNotFoundError`` (HTTP 404/422) is an input miss: no
  spot rows -> None, no strike rows -> flip None. That empty result is
  cached for the TTL like any other response. Every other client error
  (401/403, 429 after retries, 5xx, open breaker) propagates (contract
  §3.9, §4): a bad key must never look like "no data". Malformed rows
  (missing field, non-numeric or non-finite value, bad timestamp) are
  skipped. A naive ``at`` raises ``ValueError``, matching the domain's
  tz-aware timestamp rule, instead of being read in the host timezone.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    DealerPositioning,
)
from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError
from uoa_detector.sources.unusual_whales.providers._cache import TTLCache

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient


# UW's ``date`` query parameter and the per-strike row ``date`` are
# exchange-session (America/New_York) calendar dates.
_ET = ZoneInfo("America/New_York")

# Soft Decimal-equality tolerance when matching a requested strike to a
# UW strike row. A matching tolerance, not a scoring threshold.
_STRIKE_MATCH_TOLERANCE = Decimal("0.01")

_SPOT_EXPOSURES_PATH = "/api/stock/{ticker}/spot-exposures"
_STRIKE_EXPOSURE_PATH = "/api/stock/{ticker}/greek-exposure/strike"
_NET_GAMMA_USD_FIELD = "gamma_per_one_percent_move_oi"

# (strike, call_gex + put_gex, as_of): the row shape _find_flip_strike walks.
_StrikeRow = tuple[Decimal, Decimal, datetime]


@dataclass(frozen=True)
class _SpotSeries:
    """Spot-exposure snapshots of one (ticker, ET date), ascending by time."""

    times: tuple[datetime, ...]
    net_gamma_usd: tuple[Decimal, ...]

    def latest_at_or_before(self, at: datetime) -> tuple[datetime, Decimal] | None:
        """Return ``(time, gamma_per_one_percent_move_oi)`` of the latest
        snapshot whose time is <= ``at``, or None."""
        idx = bisect_right(self.times, at)
        if idx == 0:
            return None
        return self.times[idx - 1], self.net_gamma_usd[idx - 1]


class UnusualWhalesDealerGammaProvider:
    """``DealerPositioningProvider`` Protocol implementation backed by UW."""

    def __init__(
        self,
        *,
        client: UnusualWhalesClient,
        settings: UnusualWhalesSettings,
    ) -> None:
        self._client = client
        ttl_seconds = settings.cache_ttl.dealer_gamma_seconds
        self._spot_cache: TTLCache[_SpotSeries] = TTLCache(
            ttl_seconds=ttl_seconds,
        )
        self._strike_cache: TTLCache[list[_StrikeRow]] = TTLCache(
            ttl_seconds=ttl_seconds,
        )

    async def net_gamma_at(
        self,
        ticker: str,
        strike: Decimal,
        at: datetime,
    ) -> DealerPositioning | None:
        """Return the per-strike share-gamma snapshot for ``at``'s ET date.

        See the module docstring: ``net_gamma_dollars`` here is
        ``call_gex + put_gex`` (share gamma), not USD per 1% move.
        """
        rows = await self._strike_rows(ticker, _et_date(at))
        for row_strike, net_gamma, as_of in rows:
            if abs(row_strike - strike) < _STRIKE_MATCH_TOLERANCE:
                return DealerPositioning(
                    ticker=ticker.upper(),
                    strike=row_strike,
                    as_of=as_of,
                    net_gamma_dollars=net_gamma,
                    flow_direction="neutral",
                )
        return None

    async def aggregate_for_ticker(
        self,
        ticker: str,
        at: datetime,
    ) -> DealerExposureAggregate | None:
        """Return the ticker-aggregate dealer exposure as of ``at``, or None.

        Phase 3.4.1: feeds M21 (Dealer gamma exposure score). Net gamma
        (USD per 1% move) is the latest spot-exposure snapshot at or
        before ``at``; the flip strike comes from the per-strike rows of
        the same ET date. The strike endpoint is not called when no
        spot snapshot qualifies.
        """
        et_date = _et_date(at)
        spot = await self._spot_series(ticker, et_date)
        if spot.latest_at_or_before(at) is None:
            return None
        strikes = await self._strike_rows(ticker, et_date)
        return _aggregate(ticker=ticker, at=at, spot=spot, strikes=strikes)

    async def _spot_series(self, ticker: str, et_date: date) -> _SpotSeries:
        symbol = ticker.upper()
        return await self._spot_cache.get_or_fetch(
            (symbol, et_date),
            loader=lambda: self._load_spot_series(symbol, et_date),
        )

    async def _strike_rows(self, ticker: str, et_date: date) -> list[_StrikeRow]:
        symbol = ticker.upper()
        return await self._strike_cache.get_or_fetch(
            (symbol, et_date),
            loader=lambda: self._load_strike_rows(symbol, et_date),
        )

    async def _load_spot_series(self, symbol: str, et_date: date) -> _SpotSeries:
        rows = await self._fetch_rows(
            _SPOT_EXPOSURES_PATH.format(ticker=symbol), et_date,
        )
        return _decode_spot_rows(rows)

    async def _load_strike_rows(
        self, symbol: str, et_date: date,
    ) -> list[_StrikeRow]:
        rows = await self._fetch_rows(
            _STRIKE_EXPOSURE_PATH.format(ticker=symbol), et_date,
        )
        return _decode_strike_rows(rows, et_date=et_date)

    async def _fetch_rows(self, path: str, et_date: date) -> list[dict[str, Any]]:
        """GET ``path?date=<et_date>``; NotFound (404/422) -> no rows."""
        try:
            resp = await self._client.request_json(
                path, params={"date": et_date.isoformat()},
            )
        except UnusualWhalesNotFoundError:
            return []
        data = resp.get("data", [])
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]


def _et_date(at: datetime) -> date:
    """America/New_York calendar date of a tz-aware ``at``."""
    if at.tzinfo is None:
        msg = "dealer gamma 'at' must be tz-aware (UTC)"
        raise ValueError(msg)
    return at.astimezone(_ET).date()


def _aggregate(
    *,
    ticker: str,
    at: datetime,
    spot: _SpotSeries,
    strikes: list[_StrikeRow],
) -> DealerExposureAggregate | None:
    """Build the aggregate from decoded rows: net gamma from the latest
    spot snapshot at or before ``at``, flip strike from the strike rows."""
    latest = spot.latest_at_or_before(at)
    if latest is None:
        return None
    as_of, net_gamma_usd = latest
    return DealerExposureAggregate(
        ticker=ticker.upper(),
        as_of=as_of,
        net_gamma_dollars=net_gamma_usd,
        flip_strike=_find_flip_strike(strikes),
    )


def _decode_spot_rows(rows: Iterable[Mapping[str, Any]]) -> _SpotSeries:
    """Decode spot-exposure rows into a time-sorted series.

    Rows with a missing/unparseable ``time`` or a missing, non-numeric
    or non-finite ``gamma_per_one_percent_move_oi`` are skipped.
    """
    points: list[tuple[datetime, Decimal]] = []
    for row in rows:
        try:
            ts = _parse_iso_utc(str(row["time"]))
            net_gamma = Decimal(str(row[_NET_GAMMA_USD_FIELD]))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if not net_gamma.is_finite():
            continue
        points.append((ts, net_gamma))
    points.sort(key=lambda p: p[0])
    return _SpotSeries(
        times=tuple(p[0] for p in points),
        net_gamma_usd=tuple(p[1] for p in points),
    )


def _decode_strike_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    et_date: date,
) -> list[_StrikeRow]:
    """Decode greek-exposure/strike rows into strike-ascending rows.

    Per row: ``(strike, call_gex + put_gex, 00:00 ET of the row date)``.
    Rows dated after ``et_date`` are dropped. Of the remaining dates only
    the newest is kept. Malformed rows are skipped.
    """
    decoded: list[tuple[date, Decimal, Decimal]] = []
    for row in rows:
        try:
            strike = Decimal(str(row["strike"]))
            net_gamma = Decimal(str(row["call_gex"])) + Decimal(str(row["put_gex"]))
            raw_date = row.get("date")
            row_date = (
                et_date if raw_date is None else date.fromisoformat(str(raw_date))
            )
        except (KeyError, ValueError, ArithmeticError):
            continue
        if not (strike.is_finite() and net_gamma.is_finite()):
            continue
        if row_date > et_date:
            continue
        decoded.append((row_date, strike, net_gamma))
    if not decoded:
        return []
    snapshot_date = max(d[0] for d in decoded)
    as_of = datetime.combine(snapshot_date, time(0, 0), tzinfo=_ET).astimezone(UTC)
    result = [
        (strike, net_gamma, as_of)
        for row_date, strike, net_gamma in decoded
        if row_date == snapshot_date
    ]
    result.sort(key=lambda r: r[0])
    return result


def _parse_iso_utc(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _find_flip_strike(
    decoded: list[tuple[Decimal, Decimal, datetime]],
) -> Decimal | None:
    """Identify the strike where cumulative gamma crosses zero.

    Walks the sorted-by-strike rows, accumulating ``net_gamma``.
    The first strike where the running cumulative changes sign
    (vs the prior cumulative) is the flip. Returns None when the
    cumulative never crosses zero (curve is monotonically positive
    or monotonically negative across all listed strikes).

    Phase 3.4.1: This matches the UW GEX semantic — the
    'zero-gamma' strike identifies the dealer-positioning balance
    point for M21's spot-to-flip distance computation.
    """
    if len(decoded) < 2:
        return None
    cumulative = Decimal("0")
    prev_cumulative = Decimal("0")
    for strike, net, _as_of in decoded:
        prev_cumulative = cumulative
        cumulative += net
        # Sign change between prev and current (one positive, one
        # negative or zero — treat zero-crossing strictly as a flip)
        if (
            (prev_cumulative > 0 and cumulative < 0)
            or (prev_cumulative < 0 and cumulative > 0)
            or (prev_cumulative != 0 and cumulative == 0)
        ):
            return strike
    return None
