"""Frozen as-of API evidence; missing/degraded telemetry never supplies a vote."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from webapp.alpha_lab import db as lab_db
from webapp.alpha_lab.flow import ObservedAlert, measure, timestamp, utc
from webapp.alpha_lab.settings import LabSettings, load_settings
from webapp.board.db import session_factory
from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry
from webapp.calibration.settings import calendar
from webapp.live_alpha.store import LiveNewsCheck
from webapp.tournament.settings import settings


@lru_cache(maxsize=1)
def flow_settings() -> LabSettings:
    return load_settings().model_copy(
        update={"flow_window_seconds": settings().api_max_age_seconds}
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        n = float(value)
    except (ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def capture(engine: Engine, ticker: str, day: date, now: datetime) -> dict[str, Any]:
    # Cached JSON adapter only: no network and no provider objects.
    now = utc(now)
    s = settings()
    cutoff = now - timedelta(seconds=s.api_max_age_seconds)
    zone = ZoneInfo(calendar().timezone)
    data: dict[str, Any] = {
        "observed_at": now.isoformat(),
        "source_time_basis": "print-event proxy; exact vendor enrichment clock unavailable",
        "flow": None,
        "modules": {},
        "news": None,
        "arms": ["price"],
    }
    with session_factory(engine)() as session:
        if inspect(engine).has_table("alfa_lab_alert"):
            rows = list(
                session.scalars(
                    select(lab_db.Alert)
                    .where(
                        lab_db.Alert.ticker == ticker,
                        lab_db.Alert.observed_at <= now,
                        lab_db.Alert.observed_at >= cutoff,
                    )
                    .order_by(lab_db.Alert.observed_at)
                    .limit(s.api_record_limit + 1)
                )
            )
            if len(rows) <= s.api_record_limit:
                observed = []
                for row in rows:
                    payload = json.loads(row.payload_json)
                    source = timestamp(payload.get("created_at"))
                    if source is not None and source.astimezone(zone).date() == day:
                        observed.append(ObservedAlert(row.alert_id, utc(row.observed_at), payload))
                data["flow"] = asdict(measure(observed, now, flow_settings())) if observed else None
            else:
                data["flow_limit_reached"] = True
        if inspect(engine).has_table("alfa_print_meta") and inspect(engine).has_table(
            "alfa_stage_telemetry"
        ):
            stmt = (
                select(AlfaPrintMeta)
                .where(
                    AlfaPrintMeta.ticker == ticker,
                    AlfaPrintMeta.option_type == "call",
                    AlfaPrintMeta.print_ts <= now,
                    AlfaPrintMeta.print_ts >= cutoff,
                    AlfaPrintMeta.written_at <= now,
                )
                .order_by(AlfaPrintMeta.print_ts.desc(), AlfaPrintMeta.event_id.desc())
                .limit(s.api_record_limit + 1)
            )
            prints = list(session.scalars(stmt))
            latest = (
                next((p for p in prints if utc(p.print_ts).astimezone(zone).date() == day), None)
                if len(prints) <= s.api_record_limit
                else None
            )
            if latest is not None:
                for stage in session.scalars(
                    select(AlfaStageTelemetry).where(
                        AlfaStageTelemetry.run_id == latest.run_id,
                        AlfaStageTelemetry.event_id == latest.event_id,
                        AlfaStageTelemetry.written_at <= now,
                    )
                ):
                    data["modules"][stage.stage_name] = {
                        "branch": stage.branch,
                        "degraded": stage.degraded,
                        "metadata": json.loads(stage.metadata_json or "{}"),
                        "event_at": utc(latest.print_ts).isoformat(),
                        "available_at": utc(stage.written_at).isoformat(),
                        "profile_hash": stage.profile_content_hash,
                    }
        if inspect(engine).has_table("alfa_live_news_check"):
            check = session.scalar(
                select(LiveNewsCheck)
                .where(
                    LiveNewsCheck.ticker == ticker,
                    LiveNewsCheck.checked_at <= now,
                    LiveNewsCheck.checked_at >= cutoff,
                )
                .order_by(LiveNewsCheck.checked_at.desc())
                .limit(1)
            )
            if check is not None and utc(check.checked_at).astimezone(zone).date() == day:
                data["news"] = {
                    "state": check.state,
                    "checked_at": utc(check.checked_at).isoformat(),
                }
    data["arms"] = arms(data)
    return data


def arms(data: dict[str, Any]) -> list[str]:
    s = settings()

    def module(name: str) -> dict[str, Any]:
        row = data.get("modules", {}).get(name, {})
        if row.get("degraded") or row.get("branch") in (
            None,
            "timeout",
            "provider_error",
            "preset_skip",
            "no_data",
            "no_current_data",
        ):
            return {}
        return dict(row.get("metadata", {})) | {"branch": row.get("branch")}

    gamma = module("m21_dealer_gamma")
    dark = module("m26_dark_pool")
    oi = module("m27_opening_closing")
    iv = module("m24_iv_exhaustion")
    peer = module("m25_sector_peer")
    event = module("m22_event_calendar")
    has_gamma = (
        gamma.get("provider_returned") == "yes"
        and gamma.get("branch") == "full_short_and_proximate"
    )
    has_dark = (
        dark.get("branch") == "confirmed_match"
        and dark.get("provider_returned") == "yes"
        and dark.get("delay_warning_fired") == "no"
    )
    has_oi = (
        oi.get("branch") in ("strong_opening", "moderate_opening", "new_strike")
        and oi.get("provider_returned") == "yes"
        and oi.get("stale_warning_fired") == "no"
    )
    rank = _number(iv.get("iv_rank"))
    alignment = _number(peer.get("alignment_pct"))
    has_context = (
        rank is not None
        and rank <= s.iv_max_rank
        and iv.get("iv_provider_returned") == "yes"
        and iv.get("post_earnings_penalty_emitted") == "no"
        and alignment is not None
        and alignment >= s.peer_min_alignment
        and event.get("branch") in ("no_catalyst", "pre_event_dte_survives")
        and event.get("provider_returned") in ("yes", "empty")
        and (data.get("news") or {}).get("state") in ("checked_found", "checked_none")
    )
    result = ["price"]
    if (data.get("flow") or {}).get("qualified") is True:
        result.append("flow")
        if has_gamma:
            result.append("flow_gamma")
        if has_dark:
            result.append("flow_dark")
        if has_oi:
            result.append("flow_oi")
        if has_gamma and has_dark and has_oi and has_context:
            result.append("full")
    return result
