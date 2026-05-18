"""Unusual Whales DealerPositioningProvider implementation.

Phase 3.3.3.4: implements ``DealerPositioningProvider`` Protocol
backed by UW's Greek-Exposure (GEX) endpoint. UW publishes daily-
estimated dealer net gamma per (ticker, strike); we surface the
nearest snapshot to the requested ``at`` timestamp.

The provider is pure data-shipping: no business logic. Module 21
(Dealer Gamma Overlay) consumes the ``DealerPositioning`` DTO and
applies its own scoring (gamma_score, GAMMA_ACCELERATION_RISK
flag) using profile-tunable thresholds.

UW endpoint shape (Phase 3.3.9.1 — migrated from
``/api/stock/{ticker}/greek-exposure-strike``, dash-separated, which
UW deprecated; new path uses the slash-separated
``greek-exposure/strike`` and a different row schema):

  GET /api/stock/{ticker}/greek-exposure/strike
    → {
        "data": [
          {
            "date": "2026-05-11",
            "strike": "150",
            "call_delta": "...", "put_delta": "...",
            "call_charm": "...", "put_charm": "...",
            "call_vanna": "...", "put_vanna": "...",
            "call_gex": "0.0496", "put_gex": "-0.0080"
          },
          ...
        ]
      }

decision (Phase 3.3.9.1 — DealerPositioning DTO unchanged):
  Phase 3.4 stage code is frozen. To preserve the DTO surface,
  the provider maps the new UW schema into the existing
  ``DealerPositioning(strike, as_of, net_gamma_dollars,
  flow_direction)`` shape:
    - ``net_gamma_dollars = call_gex + put_gex`` per strike
      (signed total dealer gamma for that strike).
    - ``as_of`` derived from the row's ``date`` field, cast to
      21:00 UTC (US session close on DST; non-DST off by 1h,
      acceptable for daily-snapshot semantics — UW updates GEX
      end-of-day).
    - ``flow_direction = "neutral"`` (UW no longer publishes a
      directional flow field on this endpoint; M21 stage doesn't
      use the field for scoring, only for telemetry).
  The unit of ``call_gex + put_gex`` in the new UW response is
  expected to remain USD per 1% spot move (the established GEX
  convention); if backtest reveals a scale mismatch with M21's
  ``short_gamma_threshold = -$50M/1%``, that's a Phase 3.6
  calibration question, not a Phase 3.3.9 path-migration concern.

decision (UW gamma sign convention):
  UW's per-strike ``call_gex`` and ``put_gex`` are signed in the
  same convention as the legacy ``net_gamma`` field. Negative sum
  = dealers short gamma at that strike. No additional sign flip.

decision (nearest-strike fallback):
  Unchanged from Phase 3.3.3. UW returns gamma per discrete
  strike; if the requested strike isn't on UW's list, we look for
  the nearest match within $0.01 (Decimal equality soft match).
  Otherwise return None — Module 21 falls back to neutral via
  NoOp semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as _date
from datetime import time as _time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from uoa_detector.providers.dealer_positioning import (
    DealerExposureAggregate,
    DealerPositioning,
)
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
        """Return the dealer-positioning snapshot as of ``at``, or None.

        Phase 3.5.5 B2: ``at`` selects the UW snapshot as it stood on
        the most recent published date on or before ``at`` — point-in-
        time correct for backtest replay. Live callers pass ``now()``
        and get the latest, exactly as before.
        """
        key = ticker.upper()
        rows = await self._cache.get_or_fetch(
            key,
            loader=lambda: self._fetch(ticker),
        )
        return self._select_strike(
            _rows_as_of(rows, at), ticker=ticker, strike=strike,
        )

    async def aggregate_for_ticker(
        self,
        ticker: str,
        at: datetime,
    ) -> DealerExposureAggregate | None:
        """Return the ticker-aggregate snapshot at ``at``, or None.

        Phase 3.4.1: feeds M21 (Dealer gamma exposure score).

        The UW per-strike feed is summed across all listed strikes
        for ``net_gamma_dollars``. The flip strike is identified
        as the strike at which cumulative gamma (sorted by strike
        ascending) crosses zero. Returns None when no rows are
        published for the ticker.

        Phase 3.5.5 B2: ``at`` selects the snapshot as of the most
        recent published date on or before ``at``.
        """
        key = ticker.upper()
        rows = await self._cache.get_or_fetch(
            key,
            loader=lambda: self._fetch(ticker),
        )
        return self._aggregate(_rows_as_of(rows, at), ticker=ticker)

    def _aggregate(
        self,
        rows: list[dict[str, Any]],
        *,
        ticker: str,
    ) -> DealerExposureAggregate | None:
        """Sum net gamma + identify flip strike from per-strike rows."""
        if not rows:
            return None
        # Decode + sort by strike ascending
        decoded: list[tuple[Decimal, Decimal, datetime]] = []
        for row in rows:
            try:
                strike = Decimal(str(row["strike"]))
                net = _row_net_gamma(row)
                as_of = _row_as_of(row)
            except (KeyError, ValueError, ArithmeticError):
                continue
            decoded.append((strike, net, as_of))
        if not decoded:
            return None
        decoded.sort(key=lambda x: x[0])
        net_total = sum((d[1] for d in decoded), start=Decimal("0"))
        # Use the latest as_of among rows for the aggregate timestamp
        as_of_latest = max(d[2] for d in decoded)
        flip_strike = _find_flip_strike(decoded)
        return DealerExposureAggregate(
            ticker=ticker.upper(),
            as_of=as_of_latest,
            net_gamma_dollars=net_total,
            flip_strike=flip_strike,
        )

    async def _fetch(self, ticker: str) -> list[dict[str, Any]]:
        path = f"/api/stock/{ticker.upper()}/greek-exposure/strike"
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
    """Map one UW gamma-strike row to a DealerPositioning DTO.

    Phase 3.3.9.1: new UW schema (date/call_gex/put_gex/...);
    legacy fields (as_of/net_gamma/flow_direction) no longer
    present, derived per ``_row_*`` helpers below.
    """
    try:
        strike = Decimal(str(row["strike"]))
        as_of = _row_as_of(row)
        net_gamma = _row_net_gamma(row)
    except (KeyError, ValueError, ArithmeticError):
        return None
    flow_direction_raw = str(row.get("flow_direction", "neutral")).lower()
    if flow_direction_raw not in ("accumulating", "distributing", "neutral"):
        flow_direction_raw = "neutral"
    return DealerPositioning(
        ticker=ticker.upper(),
        strike=strike,
        as_of=as_of,
        net_gamma_dollars=net_gamma,
        flow_direction=flow_direction_raw,
    )


def _row_net_gamma(row: dict[str, Any]) -> Decimal:
    """Phase 3.3.9.1: net_gamma = call_gex + put_gex per strike.

    Legacy ``net_gamma`` field still tolerated (Phase 3.3.3 fixture
    compat). Raises KeyError if neither shape present.
    """
    if "net_gamma" in row:
        return Decimal(str(row["net_gamma"]))
    call_gex = Decimal(str(row["call_gex"]))
    put_gex = Decimal(str(row["put_gex"]))
    return call_gex + put_gex


def _rows_as_of(
    rows: list[dict[str, Any]], at: datetime,
) -> list[dict[str, Any]]:
    """Filter to the single most-recent UW snapshot date on or before ``at``.

    Phase 3.5.5 B2: the UW greek-exposure feed returns one (date,
    strike) row per strike per published date. A backtest enriching
    an event at ``at`` must see the snapshot as it stood then. Returns
    the rows of the newest date that is on or before ``at`` — empty if
    no snapshot pre-dates ``at`` (the event is then left un-enriched,
    a neutral M21 score, rather than peeking at future data).
    """
    at_date = at.date()
    dated: list[tuple[_date, dict[str, Any]]] = []
    for row in rows:
        try:
            row_date = _row_as_of(row).date()
        except (KeyError, ValueError, ArithmeticError):
            continue
        if row_date <= at_date:
            dated.append((row_date, row))
    if not dated:
        return []
    latest = max(d for d, _ in dated)
    return [row for d, row in dated if d == latest]


def _row_as_of(row: dict[str, Any]) -> datetime:
    """Phase 3.3.9.1: as_of derived from ``date`` (YYYY-MM-DD), cast to
    21:00 UTC (US session close on DST). Legacy ``as_of`` ISO string
    still tolerated (Phase 3.3.3 fixture compat).
    """
    if "as_of" in row:
        return _parse_iso_utc(str(row["as_of"]))
    date_str = str(row["date"])
    day = _date.fromisoformat(date_str)
    return datetime.combine(day, _time(hour=21, minute=0, tzinfo=UTC))


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
