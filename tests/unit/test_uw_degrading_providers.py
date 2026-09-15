"""Phase 5.0.5 tests: live-worker-only degrading UW provider wrappers.

Contract: ``docs/phase-5.0-merge-acceptance.md`` §3.8.3 and §5.

Pins:
  - every wrapper method maps RateLimit, DailyLimit, Transient and BreakerOpen
    to that Protocol method's documented no-data return, increments
    ``errors`` and logs one WARNING naming the provider, the method and the
    exception class, never the exception message;
  - a normal value passes through as the same object, uncounted and unlogged;
  - AuthError, NotFound raised to the wrapper, the UW base error, ValueError,
    TypeError and a plain RuntimeError all propagate uncounted;
  - a real UW provider's own NotFound -> no-data result passes through
    uncounted;
  - ``build_live_stage_pipeline(..., degrade_transient_errors=True)`` wraps
    every provider except M23's around the real UW provider and keeps one
    shared catalyst wrapper for M22 and M24;
  - pipeline level: a client failing with HTTP 503 still yields a
    ``PipelineResult`` with degradation on; with the builder default the
    same error propagates (screener behaviour unchanged).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from tests.conftest import build_print
from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.pipeline.orchestrator import Pipeline, PipelineResult
from uoa_detector.pipeline.stages import build_live_stage_pipeline
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
from uoa_detector.providers.catalyst_calendar import CatalystCalendarProvider
from uoa_detector.providers.dark_pool import DarkPoolPrintProvider
from uoa_detector.providers.dealer_positioning import DealerPositioningProvider
from uoa_detector.providers.iv_history import IVHistoryProvider
from uoa_detector.providers.open_interest import OpenInterestProvider
from uoa_detector.providers.sector_map import PeerFlowProvider, SectorMapProvider
from uoa_detector.sources.synthetic import SyntheticRawFlowSource, to_raw_print
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
    UnusualWhalesError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)
from uoa_detector.sources.unusual_whales.providers.degrading import (
    DegradingCatalystCalendarProvider,
    DegradingDarkPoolPrintProvider,
    DegradingDealerPositioningProvider,
    DegradingIVHistoryProvider,
    DegradingOpenInterestProvider,
    DegradingPeerFlowProvider,
    DegradingSectorMapProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)

_LOGGER = "uoa_detector.sources.unusual_whales.providers.degrading"
# 11:00 EDT on Monday 2026-09-14: inside the regular session.
_TS = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
_EXPIRY = date(2026, 9, 18)
_STRIKE = Decimal("600")
_WINDOW = timedelta(minutes=60)
# Stands in for a response-body excerpt; must never reach the log line.
_BODY_MARKER = "body-excerpt-not-for-logs"


# ---------------------------------------------------------------------------
# Scripted inner provider
# ---------------------------------------------------------------------------


class _ScriptedInner:
    """Implements every wrapped Protocol method: raises ``error`` or returns ``value``."""

    def __init__(self, *, value: object = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def _answer(self, method: str, args: tuple[object, ...]) -> Any:
        self.calls.append((method, args))
        if self.error is not None:
            raise self.error
        return self.value

    async def net_gamma_at(self, ticker: str, strike: Decimal, at: datetime) -> Any:
        return await self._answer("net_gamma_at", (ticker, strike, at))

    async def aggregate_for_ticker(self, ticker: str, at: datetime) -> Any:
        return await self._answer("aggregate_for_ticker", (ticker, at))

    async def next_catalyst(self, ticker: str, after: datetime) -> Any:
        return await self._answer("next_catalyst", (ticker, after))

    async def catalysts_in_window(
        self, ticker: str, window_start: datetime, window_end: datetime,
    ) -> Any:
        return await self._answer("catalysts_in_window", (ticker, window_start, window_end))

    async def iv_rank_at(
        self, ticker: str, strike: Decimal, expiry: date, option_type: str, at: datetime,
    ) -> Any:
        return await self._answer("iv_rank_at", (ticker, strike, expiry, option_type, at))

    async def sector_of(self, ticker: str) -> Any:
        return await self._answer("sector_of", (ticker,))

    async def peers_of(self, ticker: str) -> Any:
        return await self._answer("peers_of", (ticker,))

    async def recent_flow(self, tickers: object, before: datetime, window: timedelta) -> Any:
        return await self._answer("recent_flow", (tickers, before, window))

    async def recent_prints(self, ticker: str, before: datetime, window: timedelta) -> Any:
        return await self._answer("recent_prints", (ticker, before, window))

    async def at(
        self, ticker: str, strike: Decimal, expiry: date, option_type: str, when: datetime,
    ) -> Any:
        return await self._answer("at", (ticker, strike, expiry, option_type, when))

    async def next_day(
        self, ticker: str, strike: Decimal, expiry: date, option_type: str, trade_date: date,
    ) -> Any:
        return await self._answer("next_day", (ticker, strike, expiry, option_type, trade_date))


# (wrapper class, method, positional args, documented no-data return)
_METHODS = [
    pytest.param(
        DegradingDealerPositioningProvider, "net_gamma_at", ("SPY", _STRIKE, _TS), None,
        id="m21-net_gamma_at",
    ),
    pytest.param(
        DegradingDealerPositioningProvider, "aggregate_for_ticker", ("SPY", _TS), None,
        id="m21-aggregate_for_ticker",
    ),
    pytest.param(
        DegradingCatalystCalendarProvider, "next_catalyst", ("SPY", _TS), None,
        id="m22m24-next_catalyst",
    ),
    pytest.param(
        DegradingCatalystCalendarProvider, "catalysts_in_window", ("SPY", _TS - _WINDOW, _TS), (),
        id="m22m24-catalysts_in_window",
    ),
    pytest.param(
        DegradingIVHistoryProvider, "iv_rank_at", ("SPY", _STRIKE, _EXPIRY, "call", _TS), None,
        id="m24-iv_rank_at",
    ),
    pytest.param(
        DegradingSectorMapProvider, "sector_of", ("SPY",), None,
        id="m25-sector_of",
    ),
    pytest.param(
        DegradingSectorMapProvider, "peers_of", ("SPY",), (),
        id="m25-peers_of",
    ),
    pytest.param(
        DegradingPeerFlowProvider, "recent_flow", (("QQQ", "IWM"), _TS, _WINDOW), (),
        id="m25-recent_flow",
    ),
    pytest.param(
        DegradingDarkPoolPrintProvider, "recent_prints", ("SPY", _TS, _WINDOW), (),
        id="m26-recent_prints",
    ),
    pytest.param(
        DegradingOpenInterestProvider, "at", ("SPY", _STRIKE, _EXPIRY, "call", _TS), None,
        id="m27-at",
    ),
    pytest.param(
        DegradingOpenInterestProvider, "next_day", ("SPY", _STRIKE, _EXPIRY, "call", _EXPIRY),
        None,
        id="m27-next_day",
    ),
]

_DEGRADABLE_ERRORS = [
    pytest.param(
        lambda: UnusualWhalesRateLimitError(
            f"UnusualWhales GET /x failed after 3 attempts (HTTP 429): {_BODY_MARKER}",
        ),
        id="rate_limit",
    ),
    pytest.param(
        lambda: UnusualWhalesDailyLimitError(
            f"UnusualWhales GET /x rate-limited (HTTP 429): daily_request_limit {_BODY_MARKER}",
        ),
        id="daily_limit",
    ),
    pytest.param(
        lambda: UnusualWhalesTransientError(f"UnusualWhales GET /x (HTTP 503): {_BODY_MARKER}"),
        id="transient",
    ),
    pytest.param(
        lambda: CircuitBreakerOpenError(f"circuit breaker is open {_BODY_MARKER}"),
        id="breaker_open",
    ),
]

_LOUD_ERRORS = [
    pytest.param(lambda: UnusualWhalesAuthError("HTTP 401"), id="auth"),
    pytest.param(
        lambda: UnusualWhalesNotFoundError("HTTP 404", status_code=404), id="not_found",
    ),
    pytest.param(lambda: UnusualWhalesError("unclassified UW error"), id="uw_base"),
    pytest.param(lambda: ValueError("naive datetime"), id="value_error"),
    pytest.param(lambda: TypeError("wrong argument"), id="type_error"),
    pytest.param(lambda: RuntimeError("bug"), id="runtime_error"),
]


def _degrading_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


# ---------------------------------------------------------------------------
# Per-wrapper, per-method behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_error", _DEGRADABLE_ERRORS)
@pytest.mark.parametrize(("wrapper_cls", "method", "args", "no_data"), _METHODS)
async def test_degradable_error_returns_no_data_counts_and_warns(
    wrapper_cls: type[Any],
    method: str,
    args: tuple[object, ...],
    no_data: object,
    make_error: Callable[[], Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = make_error()
    inner = _ScriptedInner(error=error)
    wrapper = wrapper_cls(inner)
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    result = await getattr(wrapper, method)(*args)

    assert result == no_data
    assert type(result) is type(no_data)
    assert wrapper.errors == 1
    assert inner.calls == [(method, args)]  # delegated with the same arguments
    records = _degrading_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "_ScriptedInner" in message
    assert method in message
    assert type(error).__name__ in message
    assert _BODY_MARKER not in caplog.text

    # The counter is cumulative across calls.
    await getattr(wrapper, method)(*args)
    assert wrapper.errors == 2


@pytest.mark.parametrize(("wrapper_cls", "method", "args", "no_data"), _METHODS)
async def test_normal_value_passes_through_unchanged(
    wrapper_cls: type[Any],
    method: str,
    args: tuple[object, ...],
    no_data: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del no_data
    sentinel = object()
    inner = _ScriptedInner(value=sentinel)
    wrapper = wrapper_cls(inner)
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    result = await getattr(wrapper, method)(*args)

    assert result is sentinel
    assert wrapper.errors == 0
    assert inner.calls == [(method, args)]
    assert _degrading_records(caplog) == []


@pytest.mark.parametrize("make_error", _LOUD_ERRORS)
@pytest.mark.parametrize(("wrapper_cls", "method", "args", "no_data"), _METHODS)
async def test_non_degradable_error_propagates_uncounted(
    wrapper_cls: type[Any],
    method: str,
    args: tuple[object, ...],
    no_data: object,
    make_error: Callable[[], Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    del no_data
    error = make_error()
    wrapper = wrapper_cls(_ScriptedInner(error=error))
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    with pytest.raises(type(error)) as info:
        await getattr(wrapper, method)(*args)

    assert info.value is error
    assert wrapper.errors == 0
    assert _degrading_records(caplog) == []


@pytest.mark.parametrize(
    ("wrapper_cls", "protocol"),
    [
        (DegradingDealerPositioningProvider, DealerPositioningProvider),
        (DegradingCatalystCalendarProvider, CatalystCalendarProvider),
        (DegradingIVHistoryProvider, IVHistoryProvider),
        (DegradingSectorMapProvider, SectorMapProvider),
        (DegradingPeerFlowProvider, PeerFlowProvider),
        (DegradingDarkPoolPrintProvider, DarkPoolPrintProvider),
        (DegradingOpenInterestProvider, OpenInterestProvider),
    ],
)
def test_wrapper_conforms_to_its_protocol_and_exposes_inner(
    wrapper_cls: type[Any], protocol: type[Any],
) -> None:
    inner = _ScriptedInner()
    wrapper = wrapper_cls(inner)
    assert isinstance(wrapper, protocol)
    assert wrapper.inner is inner
    assert wrapper.errors == 0


# ---------------------------------------------------------------------------
# Real UW providers: their own NotFound -> no-data passes through uncounted
# ---------------------------------------------------------------------------


class _NotFoundClient:
    """Every request is an input miss (HTTP 404)."""

    def __init__(self) -> None:
        self.calls = 0

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del params
        self.calls += 1
        msg = f"UnusualWhales {method} {path} (HTTP 404)"
        raise UnusualWhalesNotFoundError(msg, status_code=404)


def _real(provider_cls: type[Any], wrapper_cls: type[Any]) -> Callable[[_NotFoundClient], Any]:
    def _build(client: _NotFoundClient) -> Any:
        return wrapper_cls(provider_cls(client=client, settings=UnusualWhalesSettings()))

    return _build


@pytest.mark.parametrize(
    ("build", "method", "args", "no_data"),
    [
        pytest.param(
            _real(UnusualWhalesDealerGammaProvider, DegradingDealerPositioningProvider),
            "aggregate_for_ticker", ("SPY", _TS), None, id="m21",
        ),
        pytest.param(
            _real(UnusualWhalesCatalystCalendarProvider, DegradingCatalystCalendarProvider),
            "catalysts_in_window", ("SPY", _TS - _WINDOW, _TS), (), id="m22m24",
        ),
        pytest.param(
            _real(UnusualWhalesIVHistoryProvider, DegradingIVHistoryProvider),
            "iv_rank_at", ("SPY", _STRIKE, _EXPIRY, "call", _TS), None, id="m24",
        ),
        pytest.param(
            _real(UnusualWhalesSectorMapProvider, DegradingSectorMapProvider),
            "peers_of", ("SPY",), (), id="m25-sector",
        ),
        pytest.param(
            _real(UnusualWhalesPeerFlowProvider, DegradingPeerFlowProvider),
            "recent_flow", (("QQQ",), _TS, _WINDOW), (), id="m25-peer-flow",
        ),
        pytest.param(
            _real(UnusualWhalesDarkPoolProvider, DegradingDarkPoolPrintProvider),
            "recent_prints", ("SPY", _TS, _WINDOW), (), id="m26",
        ),
        pytest.param(
            _real(UnusualWhalesOpenInterestProvider, DegradingOpenInterestProvider),
            "at", ("SPY", _STRIKE, _EXPIRY, "call", _TS), None, id="m27",
        ),
    ],
)
async def test_real_provider_not_found_no_data_passes_through(
    build: Callable[[_NotFoundClient], Any],
    method: str,
    args: tuple[object, ...],
    no_data: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _NotFoundClient()
    wrapper = build(client)
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    result = await getattr(wrapper, method)(*args)

    assert result == no_data
    assert client.calls >= 1  # the real provider did reach the client
    assert wrapper.errors == 0
    assert _degrading_records(caplog) == []


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def test_builder_with_degradation_wraps_every_provider_but_m23() -> None:
    client = UnusualWhalesClient(
        api_key=SecretStr("test-key-not-used"), settings=UnusualWhalesSettings(),
    )
    stages = {
        type(s): s
        for s in build_live_stage_pipeline(
            client, load_default_profile(), degrade_transient_errors=True,
        )
    }

    m21 = stages[DealerGammaStage]._provider
    assert isinstance(m21, DegradingDealerPositioningProvider)
    assert isinstance(m21.inner, UnusualWhalesDealerGammaProvider)

    m22 = stages[EventCalendarStage]._provider
    assert isinstance(m22, DegradingCatalystCalendarProvider)
    assert isinstance(m22.inner, UnusualWhalesCatalystCalendarProvider)

    # M23 stays unwrapped: its stage already catches provider errors.
    assert isinstance(
        stages[PriceConfirmationStage]._provider, UnusualWhalesPriceActionProvider,
    )

    m24 = stages[IVExhaustionStage]
    assert isinstance(m24._iv_provider, DegradingIVHistoryProvider)
    assert isinstance(m24._iv_provider.inner, UnusualWhalesIVHistoryProvider)
    # One shared (wrapped) catalyst provider for M22 and M24.
    assert m24._catalyst_provider is m22

    m25 = stages[SectorPeerStage]
    assert isinstance(m25._sector_provider, DegradingSectorMapProvider)
    assert isinstance(m25._sector_provider.inner, UnusualWhalesSectorMapProvider)
    assert isinstance(m25._peer_flow_provider, DegradingPeerFlowProvider)
    assert isinstance(m25._peer_flow_provider.inner, UnusualWhalesPeerFlowProvider)

    m26 = stages[DarkPoolStage]._provider
    assert isinstance(m26, DegradingDarkPoolPrintProvider)
    assert isinstance(m26.inner, UnusualWhalesDarkPoolProvider)

    m27 = stages[OpeningClosingStage]._provider
    assert isinstance(m27, DegradingOpenInterestProvider)
    assert isinstance(m27.inner, UnusualWhalesOpenInterestProvider)


# ---------------------------------------------------------------------------
# Pipeline level
# ---------------------------------------------------------------------------


class _Http503Client:
    """Every request fails as the client does after exhausting 5xx retries."""

    def __init__(self) -> None:
        self.calls = 0

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del params
        self.calls += 1
        msg = f"UnusualWhales {method} {path} failed after 3 attempts (HTTP 503)"
        raise UnusualWhalesTransientError(msg)


def _one_event_pipeline(stages: list[Any]) -> Pipeline:
    raw = to_raw_print(
        build_print(event_id="live-503", ts=_TS, ticker="SPY", strike="600", spot="598"),
        source_id="unusual_whales",
    )
    return Pipeline(
        [SyntheticRawFlowSource("unusual_whales", [raw])],
        stages,
        profile=load_default_profile(),
    )


async def test_pipeline_with_degradation_survives_http_503() -> None:
    client = _Http503Client()
    stages = build_live_stage_pipeline(
        client, load_default_profile(), degrade_transient_errors=True,  # type: ignore[arg-type]
    )
    by_type = {type(s): s for s in stages}

    results = await _one_event_pipeline(stages).run()

    assert len(results) == 1
    assert isinstance(results[0], PipelineResult)
    assert client.calls >= 1  # the providers really hit the failing client
    m21 = by_type[DealerGammaStage]
    assert m21._provider.errors == 1
    assert m21.last_execution_metadata == {"branch": "no_data", "provider_returned": "no"}
    assert results[0].event.gamma_score is None


async def test_pipeline_with_builder_default_still_propagates_http_503() -> None:
    client = _Http503Client()
    stages = build_live_stage_pipeline(client, load_default_profile())  # type: ignore[arg-type]

    with pytest.raises(UnusualWhalesTransientError):
        await _one_event_pipeline(stages).run()

    assert client.calls >= 1
