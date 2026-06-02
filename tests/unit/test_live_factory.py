"""Phase 3.3.5.3 tests for ``live.factory``.

Pins:
  - parse_feeds_arg: comma-split, lowercase, dedup, strip whitespace
  - parse_feeds_arg: empty / whitespace-only → FeedConfigurationError
  - parse_feeds_arg: unknown name → FeedConfigurationError listing supported
  - build_live_sources: UW only path
  - build_live_sources: ThetaData only path
  - build_live_sources: both feeds wired
  - build_live_sources: missing UW key → FeedConfigurationError
  - build_live_sources: missing ThetaData key → FeedConfigurationError
  - build_live_sources: ThetaData without subscriptions → error
  - build_live_sources: ThetaData without context resolver → error
  - build_live_sources: empty tickers → error
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import (
    ThetaDataSettings,
    UnusualWhalesSettings,
)
from uoa_detector.config.credentials import Credentials
from uoa_detector.live.factory import (
    SUPPORTED_FEEDS,
    FeedConfigurationError,
    LiveSourcesBundle,
    build_live_sources,
    parse_feeds_arg,
)
from uoa_detector.sources.thetadata.historical import (
    ContextSnapshot,
    ContractSpec,
)
from uoa_detector.sources.thetadata.live import (
    LiveSubscription,
    ThetaDataLiveSource,
)
from uoa_detector.sources.unusual_whales.live import UnusualWhalesLiveSource

# ---------------------------------------------------------------------------
# parse_feeds_arg
# ---------------------------------------------------------------------------


def test_parse_feeds_single() -> None:
    assert parse_feeds_arg("thetadata") == ("thetadata",)


def test_parse_feeds_both() -> None:
    assert parse_feeds_arg("thetadata,unusual_whales") == (
        "thetadata", "unusual_whales",
    )


def test_parse_feeds_lowercases_and_strips() -> None:
    assert parse_feeds_arg("  ThetaData ,  Unusual_Whales  ") == (
        "thetadata", "unusual_whales",
    )


def test_parse_feeds_dedupes_preserving_order() -> None:
    assert parse_feeds_arg("unusual_whales,thetadata,unusual_whales") == (
        "unusual_whales", "thetadata",
    )


def test_parse_feeds_empty_raises() -> None:
    with pytest.raises(FeedConfigurationError, match="empty"):
        parse_feeds_arg("")


def test_parse_feeds_whitespace_only_raises() -> None:
    with pytest.raises(FeedConfigurationError, match="empty"):
        parse_feeds_arg("   ,  , ")


def test_parse_feeds_unknown_raises_with_supported_list() -> None:
    with pytest.raises(FeedConfigurationError, match="unknown feed"):
        parse_feeds_arg("polygon")


def test_supported_feeds_includes_both() -> None:
    assert "thetadata" in SUPPORTED_FEEDS
    assert "unusual_whales" in SUPPORTED_FEEDS


# ---------------------------------------------------------------------------
# build_live_sources — fixtures
# ---------------------------------------------------------------------------


def _td_settings() -> ThetaDataSettings:
    return ThetaDataSettings()


def _uw_settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings()


def _td_context(_c: ContractSpec) -> ContextSnapshot:
    return ContextSnapshot(spot_price=Decimal("100.0"))


def _td_sub() -> LiveSubscription:
    return LiveSubscription(
        ticker="AAPL",
        expiry=date(2024, 2, 16),
        strike_dollars=Decimal("150.00"),
        right="C",
    )


# ---------------------------------------------------------------------------
# build_live_sources — happy paths
# ---------------------------------------------------------------------------


def test_build_uw_only() -> None:
    creds = Credentials(unusual_whales_api_key=SecretStr("uw_test"))
    bundle = build_live_sources(
        feeds=("unusual_whales",),
        credentials=creds,
        thetadata_settings=_td_settings(),
        unusual_whales_settings=_uw_settings(),
        tickers=["AAPL", "MSFT"],
    )
    assert isinstance(bundle, LiveSourcesBundle)
    assert len(bundle.sources) == 1
    assert isinstance(bundle.sources[0], UnusualWhalesLiveSource)
    assert bundle.feeds_resolved == ("unusual_whales",)


def test_build_thetadata_only() -> None:
    creds = Credentials(thetadata_api_key=SecretStr("td_test"))
    bundle = build_live_sources(
        feeds=("thetadata",),
        credentials=creds,
        thetadata_settings=_td_settings(),
        unusual_whales_settings=_uw_settings(),
        tickers=["AAPL"],
        thetadata_subscriptions=[_td_sub()],
        thetadata_context_resolver=_td_context,
    )
    assert len(bundle.sources) == 1
    assert isinstance(bundle.sources[0], ThetaDataLiveSource)


def test_build_both_feeds() -> None:
    creds = Credentials(
        thetadata_api_key=SecretStr("td_test"),
        unusual_whales_api_key=SecretStr("uw_test"),
    )
    bundle = build_live_sources(
        feeds=("thetadata", "unusual_whales"),
        credentials=creds,
        thetadata_settings=_td_settings(),
        unusual_whales_settings=_uw_settings(),
        tickers=["AAPL"],
        thetadata_subscriptions=[_td_sub()],
        thetadata_context_resolver=_td_context,
    )
    assert len(bundle.sources) == 2
    types = {type(s).__name__ for s in bundle.sources}
    assert types == {"ThetaDataLiveSource", "UnusualWhalesLiveSource"}


# ---------------------------------------------------------------------------
# build_live_sources — error paths
# ---------------------------------------------------------------------------


def test_missing_uw_key_fails_fast() -> None:
    # _env_file=None isolates from the developer's .env (which carries a
    # real key locally); the docstring's documented clean-slate pattern.
    creds = Credentials(_env_file=None)  # no UW key
    with pytest.raises(
        FeedConfigurationError,
        match="UNUSUAL_WHALES_API_KEY is not set",
    ):
        build_live_sources(
            feeds=("unusual_whales",),
            credentials=creds,
            thetadata_settings=_td_settings(),
            unusual_whales_settings=_uw_settings(),
            tickers=["AAPL"],
        )


def test_missing_thetadata_key_fails_fast() -> None:
    # _env_file=None isolates from the developer's .env (see above).
    creds = Credentials(_env_file=None)  # no TD key
    with pytest.raises(
        FeedConfigurationError,
        match="THETADATA_API_KEY is not set",
    ):
        build_live_sources(
            feeds=("thetadata",),
            credentials=creds,
            thetadata_settings=_td_settings(),
            unusual_whales_settings=_uw_settings(),
            tickers=["AAPL"],
            thetadata_subscriptions=[_td_sub()],
            thetadata_context_resolver=_td_context,
        )


def test_thetadata_without_subscriptions_fails() -> None:
    creds = Credentials(thetadata_api_key=SecretStr("td_test"))
    with pytest.raises(
        FeedConfigurationError,
        match="thetadata_subscriptions",
    ):
        build_live_sources(
            feeds=("thetadata",),
            credentials=creds,
            thetadata_settings=_td_settings(),
            unusual_whales_settings=_uw_settings(),
            tickers=["AAPL"],
            thetadata_subscriptions=None,
        )


def test_thetadata_with_empty_subscriptions_fails() -> None:
    creds = Credentials(thetadata_api_key=SecretStr("td_test"))
    with pytest.raises(
        FeedConfigurationError,
        match="thetadata_subscriptions is empty",
    ):
        build_live_sources(
            feeds=("thetadata",),
            credentials=creds,
            thetadata_settings=_td_settings(),
            unusual_whales_settings=_uw_settings(),
            tickers=["AAPL"],
            thetadata_subscriptions=[],
            thetadata_context_resolver=_td_context,
        )


def test_thetadata_without_context_resolver_fails() -> None:
    creds = Credentials(thetadata_api_key=SecretStr("td_test"))
    with pytest.raises(
        FeedConfigurationError,
        match="thetadata_context_resolver",
    ):
        build_live_sources(
            feeds=("thetadata",),
            credentials=creds,
            thetadata_settings=_td_settings(),
            unusual_whales_settings=_uw_settings(),
            tickers=["AAPL"],
            thetadata_subscriptions=[_td_sub()],
            thetadata_context_resolver=None,
        )


def test_empty_tickers_fails() -> None:
    creds = Credentials(unusual_whales_api_key=SecretStr("uw_test"))
    with pytest.raises(FeedConfigurationError, match="tickers list is empty"):
        build_live_sources(
            feeds=("unusual_whales",),
            credentials=creds,
            thetadata_settings=_td_settings(),
            unusual_whales_settings=_uw_settings(),
            tickers=[],
        )


def test_tickers_uppercased_for_uw_subs() -> None:
    """Ticker normalisation flows through to UW subscriptions."""
    creds = Credentials(unusual_whales_api_key=SecretStr("uw_test"))
    bundle = build_live_sources(
        feeds=("unusual_whales",),
        credentials=creds,
        thetadata_settings=_td_settings(),
        unusual_whales_settings=_uw_settings(),
        tickers=["aapl", "msft"],
    )
    uw_source = bundle.sources[0]
    # Inspect the subscriptions list (private attribute)
    subs = uw_source._subscriptions  # type: ignore[attr-defined]
    assert {s.ticker for s in subs} == {"AAPL", "MSFT"}
