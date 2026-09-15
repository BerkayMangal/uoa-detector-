"""Degrading Unusual Whales provider wrappers for the live worker — Phase 5.0.5.

Why this exists:
  The Phase 3.9 UW providers map ``UnusualWhalesNotFoundError`` (HTTP
  404/422) to their documented no-data return and let every other client
  error propagate. The frozen Phase 3.4 stages M21, M22, M24, M25, M26 and
  M27 only catch ``TimeoutError`` around their provider call; M23 alone
  catches broadly. In the long-running live worker, one transient UW error
  would therefore abort ``Pipeline.run`` and restart the worker: a 30 s gap,
  a fresh client and a 10-minute flow replay.

What a wrapper does:
  Each class delegates to one provider Protocol implementation. It maps
  exactly the service-health errors in ``DEGRADABLE_ERRORS`` to that
  Protocol method's documented no-data return:

    - ``UnusualWhalesRateLimitError`` (429 after retries; includes its
      ``UnusualWhalesDailyLimitError`` subclass, the daily-quota 429)
    - ``UnusualWhalesTransientError`` (5xx, network error, timeout,
      unparseable body, after retries)
    - ``CircuitBreakerOpenError`` (the client refuses while the breaker
      is open)

  Every catch increments the wrapper's public ``errors`` counter and logs
  one WARNING naming the wrapped provider class, the method and the
  exception class. The exception message is not logged: it can carry a
  response-body excerpt and adds nothing the class name does not say.

What stays loud:
  - ``UnusualWhalesAuthError``: a bad or unentitled key must never look
    like "no data". Its ``UnusualWhalesNotFoundError`` subclass is already
    mapped to no-data inside the UW providers; if one ever reaches a
    wrapper it propagates like any other auth error.
  - Programming errors (``TypeError``, ``ValueError``) and every other
    exception.

Scope:
  Wired only by ``build_live_stage_pipeline(...,
  degrade_transient_errors=True)``, which only the live worker passes. The
  screener and backtests keep propagating (Phase 5.0 contract §3.8). M23's
  price-action provider is not wrapped because its stage already maps
  provider errors to a neutral score. The stages themselves are untouched,
  so a degraded call shows up in the decision record as that stage's
  no-data branch.

No numeric literal lives here (D8).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    from uoa_detector.providers.catalyst_calendar import (
        CatalystCalendarProvider,
        CatalystEvent,
    )
    from uoa_detector.providers.dark_pool import (
        DarkPoolPrint,
        DarkPoolPrintProvider,
    )
    from uoa_detector.providers.dealer_positioning import (
        DealerExposureAggregate,
        DealerPositioning,
        DealerPositioningProvider,
    )
    from uoa_detector.providers.iv_history import (
        IVHistoryProvider,
        IVRankSnapshot,
    )
    from uoa_detector.providers.open_interest import (
        OpenInterestProvider,
        OpenInterestSnapshot,
    )
    from uoa_detector.providers.sector_map import (
        PeerFlowEvent,
        PeerFlowProvider,
        SectorMapProvider,
    )

_logger = logging.getLogger(__name__)

# The service-health errors a wrapper maps to no-data. The daily-quota
# ``UnusualWhalesDailyLimitError`` subclasses ``UnusualWhalesRateLimitError``.
# ``UnusualWhalesAuthError`` (and its NotFound subclass) is deliberately absent.
DEGRADABLE_ERRORS: tuple[type[Exception], ...] = (
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
    CircuitBreakerOpenError,
)


class _DegradingWrapper:
    """Error counter and WARNING shared by every wrapper in this module."""

    def __init__(self, inner: object) -> None:
        self.errors: int = 0
        self._provider_name = type(inner).__name__

    def _record_degraded(self, method: str, exc: Exception) -> None:
        self.errors += 1
        _logger.warning(
            "%s.%s raised %s; returning no-data (degraded errors=%d)",
            self._provider_name,
            method,
            type(exc).__name__,
            self.errors,
        )


class DegradingDealerPositioningProvider(_DegradingWrapper):
    """M21 ``DealerPositioningProvider``; degraded calls return ``None``."""

    def __init__(self, inner: DealerPositioningProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> DealerPositioningProvider:
        """The wrapped provider."""
        return self._inner

    async def net_gamma_at(
        self,
        ticker: str,
        strike: Decimal,
        at: datetime,
    ) -> DealerPositioning | None:
        try:
            return await self._inner.net_gamma_at(ticker, strike, at)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("net_gamma_at", exc)
            return None

    async def aggregate_for_ticker(
        self,
        ticker: str,
        at: datetime,
    ) -> DealerExposureAggregate | None:
        try:
            return await self._inner.aggregate_for_ticker(ticker, at)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("aggregate_for_ticker", exc)
            return None


class DegradingCatalystCalendarProvider(_DegradingWrapper):
    """M22/M24 ``CatalystCalendarProvider``; degraded calls return ``None`` / ``()``."""

    def __init__(self, inner: CatalystCalendarProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> CatalystCalendarProvider:
        """The wrapped provider."""
        return self._inner

    async def next_catalyst(
        self,
        ticker: str,
        after: datetime,
    ) -> CatalystEvent | None:
        try:
            return await self._inner.next_catalyst(ticker, after)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("next_catalyst", exc)
            return None

    async def catalysts_in_window(
        self,
        ticker: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[CatalystEvent, ...]:
        try:
            return await self._inner.catalysts_in_window(
                ticker, window_start, window_end,
            )
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("catalysts_in_window", exc)
            return ()


class DegradingIVHistoryProvider(_DegradingWrapper):
    """M24 ``IVHistoryProvider``; degraded calls return ``None``."""

    def __init__(self, inner: IVHistoryProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> IVHistoryProvider:
        """The wrapped provider."""
        return self._inner

    async def iv_rank_at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        at: datetime,
    ) -> IVRankSnapshot | None:
        try:
            return await self._inner.iv_rank_at(
                ticker, strike, expiry, option_type, at,
            )
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("iv_rank_at", exc)
            return None


class DegradingSectorMapProvider(_DegradingWrapper):
    """M25 ``SectorMapProvider``; degraded calls return ``None`` / ``()``."""

    def __init__(self, inner: SectorMapProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> SectorMapProvider:
        """The wrapped provider."""
        return self._inner

    async def sector_of(self, ticker: str) -> str | None:
        try:
            return await self._inner.sector_of(ticker)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("sector_of", exc)
            return None

    async def peers_of(self, ticker: str) -> Sequence[str]:
        try:
            return await self._inner.peers_of(ticker)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("peers_of", exc)
            return ()


class DegradingPeerFlowProvider(_DegradingWrapper):
    """M25 ``PeerFlowProvider``; degraded calls return ``()``."""

    def __init__(self, inner: PeerFlowProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> PeerFlowProvider:
        """The wrapped provider."""
        return self._inner

    async def recent_flow(
        self,
        tickers: Sequence[str],
        before: datetime,
        window: timedelta,
    ) -> Sequence[PeerFlowEvent]:
        try:
            return await self._inner.recent_flow(tickers, before, window)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("recent_flow", exc)
            return ()


class DegradingDarkPoolPrintProvider(_DegradingWrapper):
    """M26 ``DarkPoolPrintProvider``; degraded calls return ``()``."""

    def __init__(self, inner: DarkPoolPrintProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> DarkPoolPrintProvider:
        """The wrapped provider."""
        return self._inner

    async def recent_prints(
        self,
        ticker: str,
        before: datetime,
        window: timedelta,
    ) -> Sequence[DarkPoolPrint]:
        try:
            return await self._inner.recent_prints(ticker, before, window)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("recent_prints", exc)
            return ()


class DegradingOpenInterestProvider(_DegradingWrapper):
    """M27 ``OpenInterestProvider``; degraded calls return ``None``."""

    def __init__(self, inner: OpenInterestProvider) -> None:
        super().__init__(inner)
        self._inner = inner

    @property
    def inner(self) -> OpenInterestProvider:
        """The wrapped provider."""
        return self._inner

    async def at(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        when: datetime,
    ) -> OpenInterestSnapshot | None:
        try:
            return await self._inner.at(ticker, strike, expiry, option_type, when)
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("at", exc)
            return None

    async def next_day(
        self,
        ticker: str,
        strike: Decimal,
        expiry: date,
        option_type: Literal["call", "put"],
        trade_date: date,
    ) -> OpenInterestSnapshot | None:
        try:
            return await self._inner.next_day(
                ticker, strike, expiry, option_type, trade_date,
            )
        except DEGRADABLE_ERRORS as exc:
            self._record_degraded("next_day", exc)
            return None
