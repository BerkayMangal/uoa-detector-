"""Side-premium arithmetic over OBSERVED ALERTS, never a reconstruction of trades."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from webapp.alpha_lab.settings import LabSettings


@dataclass(frozen=True)
class ObservedAlert:
    alert_id: str
    observed_at: datetime
    payload: dict[str, Any]  # original JSON at the vendor/persistence boundary


def utc(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at.astimezone(UTC) if at.tzinfo is not None else None
    except ValueError:
        return None


def amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        return None
    try:
        parsed = Decimal(str(value))
        return (
            parsed if parsed.is_finite() and parsed >= 0 and math.isfinite(float(parsed)) else None
        )
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class FlowStats:
    alerts: int
    up_usd: float
    down_usd: float
    unassigned_usd: float
    multileg_usd: float
    net_usd: float
    contracts: int
    multileg: int
    invalid: int
    conflicts: int
    unknown_flags: int
    qualified: bool
    clean_control: bool
    source_note: str = "UW uyarı toplamları; ham işlemler veya bağımsız bahisler değildir."


_RELEVANT = (
    "created_at",
    "option_chain",
    "type",
    "total_premium",
    "total_ask_side_prem",
    "total_bid_side_prem",
    "has_multileg",
)


def measure(observations: Sequence[ObservedAlert], now: datetime, cfg: LabSettings) -> FlowStats:
    now = utc(now)
    cutoff = now - timedelta(seconds=cfg.flow_window_seconds)
    first: dict[str, ObservedAlert] = {}
    conflicts: set[str] = set()
    for obs in sorted(observations, key=lambda o: (utc(o.observed_at), o.alert_id)):
        if utc(obs.observed_at) > now:
            continue
        prev = first.get(obs.alert_id)
        if prev is None:
            first[obs.alert_id] = obs
        elif tuple(prev.payload.get(k) for k in _RELEVANT) != tuple(
            obs.payload.get(k) for k in _RELEVANT
        ):
            conflicts.add(obs.alert_id)
    up = down = unknown = multi_prem = Decimal(0)
    contracts: set[str] = set()
    multi = invalid = flags = seen = conflicted = 0
    for identifier, obs in first.items():
        p = obs.payload
        at = timestamp(p.get("created_at"))
        if at is not None and at < cutoff:
            continue
        seen += 1
        if at is None or at > now or at > utc(obs.observed_at):
            invalid += 1
            continue
        if identifier in conflicts:
            conflicted += 1
            continue
        total = amount(p.get("total_premium"))
        ask = amount(p.get("total_ask_side_prem"))
        bid = amount(p.get("total_bid_side_prem"))
        if (
            total is None
            or total <= 0
            or ask is None
            or bid is None
            or ask + bid > total
            or p.get("type") not in ("call", "put")
        ):
            invalid += 1
            continue
        if p.get("has_multileg") is True:
            multi += 1
            multi_prem += total
            continue
        if p.get("has_multileg") is not False:
            flags += 1
            unknown += total
            continue
        chain = p.get("option_chain")
        if not isinstance(chain, str):
            invalid += 1
            continue
        match = re.fullmatch(r"[A-Z][A-Z0-9.]*([0-9]{6})([CP])([0-9]{8})", chain)
        if match is None or match.group(2) != ("C" if p["type"] == "call" else "P"):
            invalid += 1
            continue
        try:
            datetime.strptime(match.group(1), "%y%m%d")
        except ValueError:
            invalid += 1
            continue
        unknown += total - ask - bid
        if p["type"] == "call":
            up += ask
            down += bid
        else:
            up += bid
            down += ask
        if ask + bid > 0:
            contracts.add(chain)
    known = up + down
    gross = known + unknown
    qualified = (
        up >= Decimal(str(cfg.min_directional_premium_usd))
        and known > 0
        and up / known >= Decimal(str(cfg.dominance_min))
        and gross > 0
        and known / gross >= Decimal(str(cfg.side_known_min))
        and len(contracts) >= cfg.min_contracts
    )
    # A polluted tape cannot silently become either a treated or a clean control.
    clean = not (multi or invalid or flags or conflicted) and (
        gross == 0 or known / gross >= Decimal(str(cfg.side_known_min))
    )
    return FlowStats(
        seen,
        float(up),
        float(down),
        float(unknown),
        float(multi_prem),
        float(up - down),
        len(contracts),
        multi,
        invalid,
        conflicted,
        flags,
        bool(qualified and clean),
        bool(clean),
    )


@dataclass(frozen=True)
class PollSample:
    at: datetime
    saturated: bool
    oldest: datetime | None


def coverage(polls: Sequence[PollSample], now: datetime, cfg: LabSettings) -> tuple[bool, str]:
    points = sorted((p for p in polls if utc(p.at) <= now), key=lambda p: utc(p.at))
    cutoff = now - timedelta(seconds=cfg.flow_window_seconds)
    if not points or utc(points[0].at) > cutoff:
        return False, "capture_warmup"
    if (now - utc(points[-1].at)).total_seconds() > cfg.poll_max_age_seconds:
        return False, "poll_stale"
    for before, after in pairwise(points):
        if (utc(after.at) - utc(before.at)).total_seconds() > cfg.poll_max_age_seconds:
            return False, "poll_gap"
        if after.saturated and (after.oldest is None or after.oldest > utc(before.at)):
            return False, "saturated_page_gap"
    return True, "observed_alerts_only"
