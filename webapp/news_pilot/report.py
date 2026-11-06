"""Small descriptive summaries; JSON is confined to the artifact boundary."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.news_export import Export, bounds
from webapp.news_research import config, experiment

VERSION = "fixed_news_daily_pilot_v1"
MINIMUM_DAY = date(2026, 11, 4)
seed_path = Path(__file__).resolve().parents[2] / "artifacts/news-pilot-v1/seed.json"


def aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_clock_required")
    return now.astimezone(UTC)


def validate_day(day: date, now: datetime) -> tuple[datetime, datetime]:
    if day < MINIMUM_DAY:
        raise ValueError("before_pilot_source_epoch")
    return bounds(day, aware(now))


def latest_completed_day(now: datetime) -> date | None:
    at = aware(now)
    cal = load_live_alpha_settings().calendar
    day = at.astimezone(ZoneInfo(cal.timezone)).date() - timedelta(days=1)
    while day >= MINIMUM_DAY:
        verified = is_session_day(day, cal)
        if verified is None:
            return None
        if verified:
            return day
        day -= timedelta(days=1)
    return None


def summarize(export: Export, evaluated: experiment.Report) -> dict[str, Any]:
    """Any is restricted to the small persisted JSON summary boundary."""
    data = export.audit.dataset
    if (data is None or evaluated.identity != data.identity
            or evaluated.profile_hash != export.profile_hash
            or evaluated.implementation_hash != export.implementation_hash):
        raise ValueError("pilot_identity_mismatch")
    def stress(item: experiment.Stress) -> dict[str, Any]:
        return {"slippage_bps_per_side": item.slippage_bps_per_side,
                "arms": [asdict(arm) for arm in item.arms],
                "complete_common_sessions": item.complete_common_sessions,
                "eb_paired_days": len(item.paired_days),
                "paired_difference_excess": item.total_paired_excess}
    missing: dict[str, int] = {}
    for unit in evaluated.base.units:
        for reason in unit.missing:
            missing[reason] = missing.get(reason, 0) + 1
    result = {"version": VERSION, "day": export.day.isoformat(),
              "since": export.since.isoformat(), "as_of": export.as_of.isoformat(),
              "source_identity": data.identity, "profile_hash": export.profile_hash,
              "implementation_hash": export.implementation_hash,
              "source_scans": export.source_scans, "source_characters": export.source_characters,
              "source_character_budget": export.source_character_budget,
              "new_context_scans": export.audit.new_context_scans,
              "legacy_scans": export.audit.legacy_scans,
              "opportunity_rows": len(data.opportunities),
              "base_true": sum(o.base is True for o in data.opportunities),
              "base_unknown": sum(o.base is None for o in data.opportunities),
              "roster_slots": evaluated.roster_count, "price_rows": export.audit.price_rows,
              "stored_prices": len(data.prices), "status": evaluated.status,
              "complete_common_sessions": evaluated.base.complete_common_sessions,
              "eb_paired_days": len(evaluated.base.paired_days), "unit_missing": missing,
              "audit_missing": dict(export.audit.missing),
              "arms": [asdict(arm) for arm in evaluated.base.arms],
              "stresses": [stress(item) for item in (evaluated.base, *evaluated.stresses)],
              "paired_difference_excess": evaluated.base.total_paired_excess,
              "new_fitted_trials": 0, "evaluated_arms": 5, "cost_scenarios": 4,
              "fixed_arm_cost_evaluations": 20, "inference": evaluated.inference,
              "winner": evaluated.winner}
    validate_summary(result, export.exported_at)
    return result


def validate_summary(row: object, now: datetime) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("pilot_summary_invalid")
    # Persisted JSON is checked at this boundary, never used to select a rule.
    if (row.get("version") != VERSION or row.get("profile_hash") != config.profile_hash()
            or row.get("implementation_hash") != config.implementation_hash()
            or row.get("inference") != "NOT_PERFORMED" or row.get("winner") is not None):
        raise ValueError("pilot_summary_identity_invalid")
    if any(row.get(k) != v for k, v in {"new_fitted_trials": 0, "evaluated_arms": 5,
                                       "cost_scenarios": 4, "fixed_arm_cost_evaluations": 20}.items()):
        raise ValueError("pilot_summary_counts_invalid")
    try:
        day = date.fromisoformat(row["day"])
        start, end = validate_day(day, now)
        if (aware(datetime.fromisoformat(row["since"])) != start
                or aware(datetime.fromisoformat(row["as_of"])) != end):
            raise ValueError("pilot_summary_bounds_invalid")
    except (KeyError, TypeError) as exc:
        raise ValueError("pilot_summary_bounds_invalid") from exc
    identity = row.get("source_identity")
    if (not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity)
            or row.get("status", "INSUFFICIENT_EVIDENCE") not in ("INSUFFICIENT_EVIDENCE", "DESCRIPTIVE_READY_NOT_VALIDATED")):
        raise ValueError("pilot_summary_source_invalid")
    for field in ("source_scans", "source_characters", "source_character_budget", "new_context_scans",
                  "legacy_scans", "opportunity_rows", "base_true", "base_unknown", "roster_slots",
                  "price_rows", "stored_prices", "complete_common_sessions", "eb_paired_days"):
        value = row.get(field)
        if type(value) is not int or value < 0:
            raise ValueError("pilot_summary_counts_invalid")
    if (not isinstance(row.get("arms"), list) or len(row["arms"]) != 5
            or {a.get("arm") for a in row["arms"] if isinstance(a, dict)} != set(experiment.ARMS)
            or not isinstance(row.get("stresses"), list) or len(row["stresses"]) != 4
            or not isinstance(row.get("unit_missing"), dict) or not isinstance(row.get("audit_missing"), dict)):
        raise ValueError("pilot_summary_metrics_invalid")
    if (row["source_scans"] > 256 or row["source_characters"] > row["source_character_budget"]
            or row["source_character_budget"] > 64 * 1024 * 1024
            or row["new_context_scans"] + row["legacy_scans"] > row["source_scans"]
            or row["base_true"] + row["base_unknown"] > row["opportunity_rows"]
            or row["stored_prices"] > row["price_rows"]
            or row["complete_common_sessions"] > 1 or row["eb_paired_days"] > 1):
        raise ValueError("pilot_summary_counts_invalid")
    for field in ("unit_missing", "audit_missing"):
        if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in row[field].items()):
            raise ValueError("pilot_summary_metrics_invalid")
    costs = {item.get("slippage_bps_per_side") for item in row["stresses"] if isinstance(item, dict)}
    if costs != {config.settings().slippage_bps_per_side, *config.settings().stress_slippage_bps_per_side}:
        raise ValueError("pilot_summary_costs_invalid")
    groups = [row["arms"]]
    for item in row["stresses"]:
        for field in ("complete_common_sessions", "eb_paired_days"):
            if type(item.get(field)) is not int or not 0 <= item[field] <= 1:
                raise ValueError("pilot_summary_metrics_invalid")
        groups.append(item.get("arms"))
    for arms in groups:
        if (not isinstance(arms, list) or len(arms) != 5
                or {a.get("arm") for a in arms if isinstance(a, dict)} != set(experiment.ARMS)):
            raise ValueError("pilot_summary_metrics_invalid")
        for arm in arms:
            for field in ("selected", "ineligible", "unknown", "priced", "complete_days", "incomplete_days"):
                if type(arm.get(field)) is not int or arm[field] < 0:
                    raise ValueError("pilot_summary_metrics_invalid")
            if (arm["selected"] + arm["ineligible"] + arm["unknown"] != row["roster_slots"]
                    or arm["priced"] > arm["selected"]
                    or arm["complete_days"] + arm["incomplete_days"] > 1):
                raise ValueError("pilot_summary_metrics_invalid")
            for field in ("net_complete_days", "excess_complete_days"):
                value = arm.get(field)
                if arm["complete_days"] == 0:
                    if value is not None:
                        raise ValueError("pilot_summary_missing_is_not_zero")
                elif type(value) not in (float, int) or not math.isfinite(value):
                    raise ValueError("pilot_summary_metrics_invalid")
    for field in ("source_export_sha256", "frozen_report_sha256", "arithmetic_audit_sha256"):
        value = row.get(field)
        if value is not None and (not isinstance(value, str) or len(value) != 64
                                  or any(c not in "0123456789abcdef" for c in value)):
            raise ValueError("pilot_summary_provenance_invalid")
    json.dumps(row, allow_nan=False)
    return row


def load_seed(now: datetime, path: Path | None = None) -> list[dict[str, Any]]:
    at = aware(now)
    source = seed_path if path is None else path
    raw = source.read_bytes()
    sidecar = source.with_suffix(".sha256").read_text().strip().split()
    if not sidecar:
        raise ValueError("pilot_seed_checksum_invalid")
    expected = sidecar[0]
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("pilot_seed_checksum_invalid")
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or payload.get("version") != VERSION
            or payload.get("profile_hash") != config.profile_hash()
            or payload.get("implementation_hash") != config.implementation_hash()
            or not isinstance(payload.get("rows"), list)):
        raise ValueError("pilot_seed_identity_invalid")
    rows = [validate_summary(row, at) for row in payload["rows"]]
    if len(rows) != 2 or {row["day"] for row in rows} != {"2026-11-04", "2026-11-05"}:
        raise ValueError("pilot_seed_roster_invalid")
    if len({row["source_identity"] for row in rows}) != len(rows):
        raise ValueError("pilot_seed_source_duplicate")
    return rows
