"""Prefix-causal features; unknown coverage never becomes zero flow."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from statistics import median
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.flow import ObservedAlert, measure, timestamp
from webapp.alpha_lab.settings import load_settings
from webapp.event_calibration.model import Alert, Dataset, Decision, Poll, settings
from webapp.event_calibration.series import Series


@dataclass(frozen=True)
class Sample:
    decision: Decision
    day: date
    bucket: int
    close: datetime
    up: float | None
    dominance: float | None
    ratio: float | None
    percentile: float | None
    acceptance: float | None
    history: int
    flow_reason: str
    market: float | None = None


def session(at: datetime) -> tuple[date, int, datetime] | None:
    cal = load_live_alpha_settings().calendar
    local = at.astimezone(ZoneInfo(cal.timezone))
    day = local.date()
    if not is_session_day(day, cal):
        return None
    opening = datetime.combine(day, cal.regular_open, local.tzinfo)
    closing = datetime.combine(day, close_time(day, cal), local.tzinfo)
    minutes = int((local - opening).total_seconds() // 60)
    if minutes < settings().opening_delay_minutes or local >= closing:
        return None
    return day, minutes // settings().bucket_minutes, closing


def build(data: Dataset) -> tuple[Sample, ...]:
    cfg = settings()
    first: dict[tuple[str, date, int], tuple[Decision, datetime]] = {}
    for decision in sorted(data.decisions, key=lambda d: (d.at, d.key)):
        s = session(decision.at)
        if s is None or s[0] not in data.sessions:
            continue
        day, bucket, closing = s
        first.setdefault((decision.ticker, day, bucket), (decision, closing))
    histories: dict[tuple[str, int], list[tuple[date, float]]] = defaultdict(list)
    result = []
    lab = load_settings()
    series = Series.make(data)
    poll_groups: dict[tuple[str, date], list[Poll]] = defaultdict(list)
    alert_groups: dict[tuple[str, date], list[Alert]] = defaultdict(list)
    for p in data.polls:
        poll_groups[p.ticker, p.at.astimezone(ZoneInfo("America/New_York")).date()].append(p)
    for a in data.alerts:
        alert_groups[a.ticker, a.known_at.astimezone(ZoneInfo("America/New_York")).date()].append(a)
    for (ticker, day, bucket), (decision, closing) in sorted(
        first.items(), key=lambda pair: (pair[1][0].at, pair[0])
    ):
        at = decision.at
        cutoff = at - timedelta(seconds=cfg.flow_window_seconds)
        all_polls = sorted((p for p in poll_groups[ticker, day] if p.at <= at), key=lambda p: p.at)
        before = [p for p in all_polls if p.at <= cutoff]
        polls = ([before[-1]] if before else []) + [p for p in all_polls if cutoff < p.at <= at]
        complete = decision.usable and bool(
            before
            and polls
            and (at - polls[-1].at).total_seconds() <= cfg.poll_gap_seconds
            and not any(p.saturated for p in polls)
            and all(
                (b.at - a.at).total_seconds() <= cfg.poll_gap_seconds for a, b in pairwise(polls)
            )
        )
        observations = [
            ObservedAlert(o.key, o.known_at, o.payload)
            for o in alert_groups[ticker, day]
            if o.known_at <= at
            and (
                (native := timestamp(o.payload.get("created_at"))) is None or cutoff <= native <= at
            )
        ]
        stats = measure(observations, at, lab)
        total = stats.up_usd + stats.down_usd + stats.unassigned_usd
        clean = (
            complete
            and not (stats.invalid or stats.conflicts or stats.unknown_flags or stats.multileg)
            and (total == 0 or (stats.up_usd + stats.down_usd) / total >= lab.side_known_min)
        )
        up = stats.up_usd if clean else None
        denom = stats.up_usd + stats.down_usd
        dominance = stats.up_usd / denom if clean and denom > 0 else None
        previous = [value for d, value in histories[ticker, bucket] if d < day][
            -cfg.normalization_sessions :
        ]
        n = len(previous)
        ratio = percentile = None
        if up is not None and n >= cfg.minimum_history_sessions:
            centre = median(previous)
            ratio = up / centre if centre > 0 else None
            percentile = sum(v <= up for v in previous) / n
        current = series.known(ticker, at)
        prior = series.known(ticker, at - timedelta(minutes=cfg.price_acceptance_minutes))
        acceptance = (
            current.value / prior.value - 1
            if current and prior and current.at > prior.at and current.at.date() == prior.at.date()
            else None
        )
        market_now = series.known("SPY", at)
        market_prior = series.known("SPY", at - timedelta(minutes=cfg.price_acceptance_minutes))
        market = (
            decision.market.value
            if decision.market
            else market_now.value / market_prior.value - 1
            if market_now
            and market_prior
            and market_now.at > market_prior.at
            and market_now.at.date() == market_prior.at.date()
            else None
        )
        result.append(
            Sample(
                decision,
                day,
                bucket,
                closing,
                up,
                dominance,
                ratio,
                percentile,
                acceptance,
                n,
                "observed_alerts_only" if clean else "incomplete_or_ambiguous_flow",
                market,
            )
        )
        if up is not None:
            histories[ticker, bucket].append((day, up))
    return tuple(result)
