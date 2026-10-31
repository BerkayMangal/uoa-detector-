"""Small, date-checked capability probes. No key means zero HTTP requests."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.event_calibration.model import settings

BASE = "https://api.unusualwhales.com"


def _day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("America/New_York")).date() if parsed.tzinfo else None
    except ValueError:
        return None


async def probe(
    ticker: str, days: tuple[date, date], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    cfg = settings()
    report: dict[str, Any] = {
        "status": "MISSING_CREDENTIAL",
        "attempted_at": datetime.now(UTC).isoformat(),
        "requested_days": [d.isoformat() for d in days],
        "ticker": ticker,
        "requests": 0,
        "rows": [],
        "ready_for_calibration": False,
        "bulk_harvest_performed": False,
    }
    key = os.getenv("UNUSUAL_WHALES_API_KEY")
    if not key:
        return report
    if (
        not ticker
        or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in ticker)
        or days[0] == days[1]
    ):
        raise ValueError("valid uppercase ticker and distinct probe dates required")
    cal = load_live_alpha_settings().calendar
    if any(
        not is_session_day(d, cal) or d >= datetime.now(ZoneInfo(cal.timezone)).date() for d in days
    ):
        raise ValueError("two completed registered sessions required")
    report["status"] = "PROBED_CAPABILITY_ONLY"
    async with httpx.AsyncClient(
        transport=transport,
        headers={"Authorization": f"Bearer {key}"},
        timeout=cfg.probe_timeout_seconds,
        follow_redirects=False,
    ) as client:
        for day in days:
            zone = ZoneInfo(cal.timezone)
            start = datetime.combine(day, time.min, zone).astimezone(UTC)
            end = datetime.combine(day, cal.regular_close, zone).astimezone(UTC)
            endpoints: list[tuple[str, str, dict[str, str | int], tuple[str, ...]]] = [
                (
                    "minute_prices",
                    f"/api/stock/{ticker}/ohlc/1m",
                    {"date": day.isoformat()},
                    ("start_time", "time", "date"),
                ),
                (
                    "flow_alerts",
                    "/api/option-trades/flow-alerts",
                    {
                        "ticker_symbol": ticker,
                        "newer_than": int(start.timestamp()),
                        "older_than": int(end.timestamp()),
                        "limit": 200,
                    },
                    ("created_at",),
                ),
                (
                    "gamma",
                    f"/api/stock/{ticker}/greek-exposure/strike",
                    {"date": day.isoformat()},
                    ("date", "time"),
                ),
                (
                    "option_chains",
                    f"/api/stock/{ticker}/historic",
                    {"date": day.isoformat()},
                    ("date",),
                ),
            ]
            for kind, path, params, fields in endpoints:
                if report["requests"] >= cfg.probe_max_requests:
                    return report
                report["requests"] += 1
                try:
                    response = await client.get(BASE + path, params=params)
                except httpx.HTTPError as error:
                    report.update(status="TRANSPORT_BLOCKED", error=type(error).__name__)
                    return report
                row: dict[str, Any] = {
                    "kind": kind,
                    "requested_day": day.isoformat(),
                    "http": response.status_code,
                }
                report["rows"].append(row)
                if response.status_code != 200:
                    row["status"] = "REFUSED_OR_UNAVAILABLE"
                    report["status"] = "PROBE_STOPPED_HTTP"
                    return report
                try:
                    payload = response.json()
                    rows = (
                        payload.get("data", payload.get("chains", []))
                        if isinstance(payload, dict)
                        else payload
                    )
                    if not isinstance(rows, list):
                        raise ValueError("not rows")
                    dates = [
                        _day(
                            next((record.get(f) for f in fields if record.get(f) is not None), None)
                        )
                        if isinstance(record, dict)
                        else None
                        for record in rows
                    ]
                    good = bool(dates) and all(d == day for d in dates)
                    row.update(
                        count=len(rows),
                        date_checked=good,
                        status="DATED_ROWS" if good else "EMPTY_OR_DATE_UNVERIFIED",
                        page_complete=False if kind == "flow_alerts" and len(rows) >= 200 else None,
                    )
                except (ValueError, TypeError):
                    row["status"] = "INVALID_PAYLOAD"
                try:
                    native_date = parsedate_to_datetime(response.headers["date"])
                    if (
                        native_date.tzinfo is None
                        or native_date.astimezone(ZoneInfo(cal.timezone)).date()
                        != datetime.now(ZoneInfo(cal.timezone)).date()
                    ):
                        raise ValueError("quota timestamp is not current")
                    count = int(response.headers["x-uw-daily-req-count"])
                    limit = int(response.headers["x-uw-token-req-limit"])
                    if count < 0 or limit <= 0 or count > limit:
                        raise ValueError("invalid quota counters")
                    row["quota_remaining"] = limit - count
                except (ValueError, KeyError):
                    report["status"] = "QUOTA_UNVERIFIED"
                    return report
                if limit - count <= cfg.probe_quota_reserve:
                    report["status"] = "QUOTA_RESERVE_REACHED"
                    return report
    return report
