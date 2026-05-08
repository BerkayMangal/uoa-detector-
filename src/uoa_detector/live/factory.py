"""Live source factory.

Phase 3.3.5.3: turns the CLI's ``--feeds thetadata,unusual_whales``
string into a concrete list of ``RawFlowSource`` instances, wiring
each adapter with its credentials, profile settings, and ticker
subscriptions.

Recognised feed names:
  - 'thetadata'        → ThetaDataLiveSource (Phase 3.3.2.5)
  - 'unusual_whales'   → UnusualWhalesLiveSource (Phase 3.3.3.3)

Both feeds are independently optional. Operator can run with one,
the other, or both. If neither is requested → ValueError. If a
requested feed's credential is missing → ValueError (fail-fast at
startup; do not silently disable).

decision (parse comma-separated string in factory, not CLI):
  Centralises validation. CLI just passes the raw string; factory
  splits, normalises (lowercase, strip), and validates. Errors
  surface with a consistent message regardless of how the factory
  is invoked (CLI, test, future programmatic caller).

decision (fail-fast on missing credential):
  Operator running ``--feeds thetadata,unusual_whales`` without
  UNUSUAL_WHALES_API_KEY in env should see a clear startup error,
  not silent UW exclusion. The factory raises with a message that
  names the missing env var. Tests pin this behaviour.

decision (one ContractSpec subscription set per feed → ticker-only):
  Live flow streams subscribe at the ticker level, not contract
  level (UW supports per-ticker ``flow_alerts``; ThetaData supports
  per-contract subscriptions but the live source iterates every
  contract for the requested tickers). Phase 3.3.5.3 takes a
  flat list of tickers and translates per-feed.

decision (ThetaData ticker subscription requires contract enumeration):
  ThetaDataLiveSource takes ``Subscription(ticker, expiry, strike,
  right)`` per contract. Phase 3.3.5.3's caller (CLI in 3.3.5.4)
  passes a contract enumerator (Phase 3.3.4.5's
  ThetaDataContractLister) and the factory builds the
  Subscription set per ticker.

  For 3.3.5.3 we accept a pre-built ``thetadata_subscriptions``
  list — the caller wires it. This keeps the factory testable
  without an HTTP fixture; the CLI is the integration point that
  combines lister + factory.

decision (ws_url override per feed):
  The factory accepts ``thetadata_ws_url`` and
  ``unusual_whales_ws_url`` constructor params. Default to each
  adapter's documented production URL. Operators sandbox-testing
  against a local proxy override here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from uoa_detector.sources.thetadata.live import (
    LiveSubscription as _ThetaDataSubscription,
)
from uoa_detector.sources.thetadata.live import (
    ThetaDataLiveSource,
)
from uoa_detector.sources.unusual_whales.live import (
    FlowSubscription as _UnusualWhalesSubscription,
)
from uoa_detector.sources.unusual_whales.live import (
    UnusualWhalesLiveSource,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from uoa_detector.calibration.profile import (
        ThetaDataSettings,
        UnusualWhalesSettings,
    )
    from uoa_detector.config.credentials import Credentials
    from uoa_detector.sources.thetadata.historical import (
        ContextSnapshot,
        ContractSpec,
    )


_logger = logging.getLogger(__name__)


# Feed name aliases. Lowercase; trim before lookup.
SUPPORTED_FEEDS: frozenset[str] = frozenset({
    "thetadata",
    "unusual_whales",
})


class FeedConfigurationError(ValueError):
    """Raised when --feeds is invalid (unknown name, empty, missing key)."""


@dataclass(frozen=True)
class LiveSourcesBundle:
    """The result of build_live_sources — typed for downstream wiring.

    ``sources`` is the list LiveObserver / Pipeline consume.
    ``feeds_resolved`` is the canonical normalised set (for logging).
    """

    sources: list[object] = field(default_factory=list)
    feeds_resolved: tuple[str, ...] = ()


def parse_feeds_arg(raw: str) -> tuple[str, ...]:
    """Parse the ``--feeds`` CLI string.

    Splits on comma, lowercases, strips whitespace, deduplicates
    while preserving caller order. Empty result raises.
    """
    if raw is None or raw.strip() == "":
        msg = "--feeds is empty; specify at least one feed"
        raise FeedConfigurationError(msg)
    parts = [p.strip().lower() for p in raw.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        msg = "--feeds is empty after normalisation"
        raise FeedConfigurationError(msg)
    seen: set[str] = set()
    deduped: list[str] = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)
    unknown = [p for p in deduped if p not in SUPPORTED_FEEDS]
    if unknown:
        msg = (
            f"unknown feed(s): {unknown}. Supported: "
            f"{sorted(SUPPORTED_FEEDS)}"
        )
        raise FeedConfigurationError(msg)
    return tuple(deduped)


def build_live_sources(
    *,
    feeds: tuple[str, ...],
    credentials: Credentials,
    thetadata_settings: ThetaDataSettings,
    unusual_whales_settings: UnusualWhalesSettings,
    tickers: Iterable[str],
    thetadata_subscriptions: Iterable[_ThetaDataSubscription] | None = None,
    thetadata_context_resolver: (
        Callable[[ContractSpec], ContextSnapshot] | None
    ) = None,
    thetadata_ws_url: str = "ws://127.0.0.1:25520/v2/ws",
    unusual_whales_ws_url: str = "wss://api.unusualwhales.com/v1/ws",
) -> LiveSourcesBundle:
    """Build a list of ``RawFlowSource`` instances ready for Pipeline.

    Each requested feed:
      - 'unusual_whales': one source instance, subscribed per ticker
      - 'thetadata':      one source instance, subscribed per contract
                          (caller supplies pre-built subscription set)

    Raises FeedConfigurationError if a feed's credential is missing
    or if a required subscription set is absent.
    """
    sources: list[object] = []
    tickers_tuple = tuple(t.upper() for t in tickers)
    if not tickers_tuple:
        msg = "tickers list is empty; live observer needs at least one ticker"
        raise FeedConfigurationError(msg)

    for feed in feeds:
        if feed == "unusual_whales":
            api_key = credentials.unusual_whales_api_key
            if api_key is None:
                msg = (
                    "feed 'unusual_whales' requested but "
                    "UNUSUAL_WHALES_API_KEY is not set"
                )
                raise FeedConfigurationError(msg)
            uw_subs = [
                _UnusualWhalesSubscription(ticker=t)
                for t in tickers_tuple
            ]
            uw_source = UnusualWhalesLiveSource(
                api_key=api_key,
                settings=unusual_whales_settings,
                subscriptions=uw_subs,
                ws_url=unusual_whales_ws_url,
            )
            sources.append(uw_source)
            _logger.info(
                "live: wired unusual_whales source (%d ticker subs)",
                len(uw_subs),
            )
            continue
        if feed == "thetadata":
            api_key = credentials.thetadata_api_key
            if api_key is None:
                msg = (
                    "feed 'thetadata' requested but "
                    "THETADATA_API_KEY is not set"
                )
                raise FeedConfigurationError(msg)
            if thetadata_subscriptions is None:
                msg = (
                    "feed 'thetadata' requested but no contract "
                    "subscriptions supplied. Caller must enumerate "
                    "contracts (e.g., via ThetaDataContractLister) "
                    "and pass thetadata_subscriptions=[...]."
                )
                raise FeedConfigurationError(msg)
            td_subs = list(thetadata_subscriptions)
            if not td_subs:
                msg = (
                    "feed 'thetadata' requested but "
                    "thetadata_subscriptions is empty"
                )
                raise FeedConfigurationError(msg)
            if thetadata_context_resolver is None:
                msg = (
                    "feed 'thetadata' requested but "
                    "thetadata_context_resolver is None. Caller must "
                    "supply a callable mapping ContractSpec → "
                    "ContextSnapshot (spot, OI, IV per contract)."
                )
                raise FeedConfigurationError(msg)
            td_source = ThetaDataLiveSource(
                api_key=api_key,
                settings=thetadata_settings,
                subscriptions=td_subs,
                get_context_for_contract=thetadata_context_resolver,
                ws_url=thetadata_ws_url,
            )
            sources.append(td_source)
            _logger.info(
                "live: wired thetadata source (%d contract subs)",
                len(td_subs),
            )
            continue
        # Unreachable: parse_feeds_arg already filtered unknown names.
        msg = f"unknown feed: {feed}"
        raise FeedConfigurationError(msg)

    if not sources:
        msg = "no sources built; check --feeds and credentials"
        raise FeedConfigurationError(msg)

    return LiveSourcesBundle(
        sources=sources,
        feeds_resolved=feeds,
    )
