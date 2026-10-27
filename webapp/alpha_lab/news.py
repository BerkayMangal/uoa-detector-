"""Conservative headline annotations, not a directional or earnings-surprise model."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.model import NewsItem
from webapp.alpha_lab.flow import ObservedAlert, measure, timestamp, utc
from webapp.alpha_lab.settings import LabSettings
from webapp.live_alpha.inputs import SpotRead

# Text definitions, not fitted scores. Changing them requires a new classifier version.
VERSION = "headline_rules_v1"
POSITIVE = (
    r"\braises?\s+(?:its\s+)?(?:guidance|forecast)",
    r"\b(?:wins?|awarded)\b.*\bcontract\b",
    r"\bupgrade[ds]?\b",
    r"\bbeats?\b.*\b(?:estimates|expectations)\b",
)
NEGATIVE = (
    r"\b(?:cuts?|lowers?|withdraws?)\s+(?:its\s+)?(?:guidance|forecast)",
    r"\bdowngrade[ds]?\b",
    r"\b(?:investigation|fraud|bankruptcy|recall)\b",
    r"\bmiss(?:es|ed)?\b.*\b(?:estimates|expectations)\b",
)
NEGATION = re.compile(
    r"\b(?:not|no|denies?|denied|rumou?r|may|might|could|expected to|seeks? to)\b", re.I
)


@dataclass(frozen=True)
class NewsEvidence:
    ref: str
    source: str
    provider_created_at: str
    first_seen_at: str
    label: str
    flow_order: str = "not_observed"
    price_change_pct: float | None = None
    price_before_at: str | None = None
    price_after_at: str | None = None


@dataclass(frozen=True)
class CapturedPrice:
    observed_at: datetime
    source_at: datetime
    value: float
    session_day: str


@dataclass(frozen=True)
class NewsAudit:
    state: str
    positive: int
    negative: int
    mixed: int
    unclassified: int
    items: tuple[str, ...]
    classifier_version: str = VERSION
    limitation: str = "Yalnız başlık; beklenti/sürpriz ölçümü veya doğrulanmış haber yönü değil."
    evidence: tuple[NewsEvidence, ...] = ()
    checked_at: str | None = None
    detail: str = ""


def annotate(
    items: Sequence[NewsItem], checked_state: str, now: datetime, window_hours: float
) -> NewsAudit:
    counts = {"positive": 0, "negative": 0, "mixed": 0, "unclassified": 0}
    seen: set[str] = set()
    headlines: list[str] = []
    evidence: list[NewsEvidence] = []
    for item in sorted(items, key=lambda x: utc(x.first_seen_at)):
        if not (
            now - timedelta(hours=window_hours) <= utc(item.provider_created_at) <= now
            and utc(item.first_seen_at) <= now
        ):
            continue
        text = item.headline.casefold()
        # Ignore punctuation/source syndication, but do not pretend paraphrases match.
        key = re.sub(r"[^\w]+", "", text)
        if key in seen:
            continue
        seen.add(key)
        pos = any(re.search(pattern, text) for pattern in POSITIVE)
        neg = any(re.search(pattern, text) for pattern in NEGATIVE)
        tag = (
            "unclassified"
            if NEGATION.search(text)
            else (
                "mixed"
                if pos and neg
                else "positive"
                if pos
                else "negative"
                if neg
                else "unclassified"
            )
        )
        counts[tag] += 1
        headlines.append(f"{tag}: {item.headline}")
        evidence.append(
            NewsEvidence(
                item.ref,
                item.source,
                utc(item.provider_created_at).isoformat(),
                utc(item.first_seen_at).isoformat(),
                tag,
            )
        )
    # Retrieval failures never turn into 'no news'. Historical items remain auditable.
    state = checked_state
    if checked_state in ("checked_found", "checked_none"):
        state = (
            "mixed"
            if counts["mixed"] or (counts["positive"] and counts["negative"])
            else (
                "negative"
                if counts["negative"]
                else "positive"
                if counts["positive"]
                else "unclassified"
                if headlines
                else "checked_none"
            )
        )
    return NewsAudit(
        state,
        counts["positive"],
        counts["negative"],
        counts["mixed"],
        counts["unclassified"],
        tuple(headlines),
        evidence=tuple(evidence),
    )


def with_context(
    audit: NewsAudit,
    alerts: Sequence[ObservedAlert],
    history: Sequence[CapturedPrice],
    spot: SpotRead | None,
    now: datetime,
    cfg: LabSettings,
) -> NewsAudit:
    """Observed ordering and cached-price movement, never 'already priced' inference."""
    versions: dict[str, list[ObservedAlert]] = defaultdict(list)
    for alert in alerts:
        if utc(alert.observed_at) <= now:
            versions[alert.alert_id].append(alert)
    first_observations = []
    for group in versions.values():
        flow = measure(group, now, cfg)
        if flow.clean_control and flow.net_usd > 0:
            first_observations.append(min(utc(a.observed_at) for a in group))
    # A later conflicting revision invalidates that alert's directional timing
    # evidence too; examining individual versions would miss the contradiction.
    first_flow = min(first_observations, default=None)
    evidence = []
    for item in audit.evidence:
        seen = timestamp(item.first_seen_at)
        assert seen is not None
        order = (
            "not_observed"
            if first_flow is None
            else (
                "flow_seen_first"
                if first_flow < seen
                else "news_seen_first"
                if first_flow > seen
                else "same_observation"
            )
        )
        day = seen.astimezone(ZoneInfo("America/New_York")).date()
        before = max(
            (
                p
                for p in history
                if utc(p.observed_at) <= seen
                and utc(p.source_at) <= seen
                and p.session_day == day.isoformat()
                and 0 <= (seen - utc(p.source_at)).total_seconds() <= cfg.spot_max_age_seconds
            ),
            key=lambda p: utc(p.source_at),
            default=None,
        )
        change = None
        if (
            before
            and before.value > 0
            and spot
            and spot.spot
            and spot.trade_date == day
            and (
                seen <= utc(spot.fetched_at) <= now
                and (now - utc(spot.fetched_at)).total_seconds() <= cfg.spot_max_age_seconds
            )
        ):
            change = (spot.spot / before.value - 1) * 100
        evidence.append(
            replace(
                item,
                flow_order=order,
                price_change_pct=change,
                price_before_at=utc(before.source_at).isoformat()
                if before and change is not None
                else None,
                price_after_at=utc(spot.fetched_at).isoformat()
                if spot and change is not None
                else None,
            )
        )
    return replace(audit, evidence=tuple(evidence))
