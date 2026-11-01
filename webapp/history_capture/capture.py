"""Sealed private market payloads, native quota guard and bounded pagination."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.flow import timestamp

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://api.unusualwhales.com"
ET = ZoneInfo("America/New_York")


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    start: date
    end: date
    tickers: tuple[str, ...]
    benchmark: str
    max_requests: int
    quota_reserve: int
    spacing_seconds: float
    timeout_seconds: int
    max_flow_pages: int
    flow_page_limit: int


def settings() -> Settings:
    return Settings(
        **YAML(typ="safe").load((ROOT / "profiles/history_capture_v1.yaml").read_text())
    )


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


class CaptureStoppedError(RuntimeError):
    """Operational stop; no implicit retry and no exception containing credentials."""


@dataclass
class Client:
    http: httpx.AsyncClient
    cfg: Settings
    calls: int = 0
    remaining: int | None = None
    last: float = 0

    async def get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        if self.calls >= self.cfg.max_requests:
            raise CaptureStoppedError("request_budget")
        if self.remaining is not None and self.remaining <= self.cfg.quota_reserve:
            raise CaptureStoppedError("quota_reserve")
        delay = self.cfg.spacing_seconds - (time.monotonic() - self.last)
        if delay > 0:
            await asyncio.sleep(delay)
        self.calls += 1
        self.last = time.monotonic()
        try:
            response = await self.http.get(BASE + path, params=params)
        except httpx.HTTPError as error:
            raise CaptureStoppedError("transport_" + type(error).__name__) from None
        if response.status_code != 200:
            raise CaptureStoppedError("http_" + str(response.status_code))
        try:
            native = parsedate_to_datetime(response.headers["date"])
            count = int(response.headers["x-uw-daily-req-count"])
            limit = int(response.headers["x-uw-token-req-limit"])
            if (
                native.tzinfo is None
                or native.astimezone(ET).date() != datetime.now(ET).date()
                or not 0 <= count <= limit
            ):
                raise ValueError("quota")
            self.remaining = limit - count
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("envelope")
        except (ValueError, KeyError, TypeError):
            raise CaptureStoppedError("unverified_quota_or_payload") from None
        return {
            "payload": payload,
            "received_at": datetime.now(UTC).isoformat(),
            "quota": {"native_date": native.isoformat(), "count": count, "limit": limit},
            "request": {"path": path, "params": params},
        }


def rows(packet: dict[str, Any]) -> list[dict[str, Any]]:
    payload = packet["payload"]
    result = payload.get("data", payload.get("chains"))
    if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
        raise CaptureStoppedError("invalid_rows")
    return result


def seal(path: Path, packet: dict[str, Any]) -> None:
    if path.exists():
        raise CaptureStoppedError("refuse_overwrite")
    packet = packet | {"packet_hash": digest(packet)}
    data = json.dumps(packet, sort_keys=True, default=str, allow_nan=False).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".part")
    pending.write_bytes(gzip.compress(data, mtime=0))
    pending.replace(path)


def read(path: Path) -> dict[str, Any]:
    packet: dict[str, Any] = json.loads(gzip.decompress(path.read_bytes()))
    saved = packet.pop("packet_hash")
    if saved != digest(packet):
        raise CaptureStoppedError("packet_checksum")
    return packet | {"packet_hash": saved}


async def flow(client: Client, ticker: str, day: date) -> dict[str, Any]:
    cal = load_live_alpha_settings().calendar
    start = datetime.combine(day, cal.regular_open, ET)
    end = datetime.combine(day, cal.regular_close, ET)
    cursor = end
    seen: dict[str, dict[str, Any]] = {}
    packets = []
    complete = False
    reason = "page_limit"
    for _ in range(client.cfg.max_flow_pages):
        packet = await client.get(
            "/api/option-trades/flow-alerts",
            {
                "ticker_symbol": ticker,
                "newer_than": int(start.timestamp()),
                "older_than": int(cursor.timestamp()) + 1,
                "limit": client.cfg.flow_page_limit,
            },
        )
        page = rows(packet)
        packets.append({k: v for k, v in packet.items() if k != "payload"})
        native = []
        added = 0
        for row in page:
            at = timestamp(row.get("created_at"))
            if at is None:
                raise CaptureStoppedError("flow_unknown_native_time")
            native.append(at)
            if start <= at <= end:
                key = str(
                    row.get("id")
                    or digest(
                        (row.get("option_chain"), row.get("created_at"), row.get("total_size"))
                    )
                )
                if key in seen and digest(seen[key]) != digest(row):
                    raise CaptureStoppedError("flow_conflicting_identity")
                if key not in seen:
                    added += 1
                seen[key] = row
        if len(page) < client.cfg.flow_page_limit or (native and min(native) < start):
            complete = True
            reason = "window_exhausted"
            break
        if not native or not added or min(native) >= cursor:
            reason = "cursor_stall"
            break
        cursor = min(native)
    return {
        "ticker": ticker,
        "day": day.isoformat(),
        "kind": "flow",
        "complete": complete,
        "stop_reason": reason,
        "payload": {"data": list(seen.values())},
        "pages": packets,
        "received_at": datetime.now(UTC).isoformat(),
    }


async def capture(destination: Path, *, cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings()
    key = os.getenv("UNUSUAL_WHALES_API_KEY")
    if not key:
        raise CaptureStoppedError("missing_credential")
    profile_hash = digest(cfg.model_dump(mode="json"))
    destination.mkdir(parents=True, exist_ok=True)
    state_path = destination / "state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if previous and previous.get("profile_hash") != profile_hash:
        raise CaptureStoppedError("capture_profile_mismatch")
    state: dict[str, Any] = {
        "profile_hash": profile_hash,
        "profile": cfg.model_dump(mode="json"),
        "started_at": datetime.now(UTC).isoformat(),
        "calls_this_run": 0,
        "sealed": 0,
        "resumed": 0,
        "status": "running",
        "remaining": None,
        "previous_runs": [
            *previous.get("previous_runs", []),
            {
                k: previous.get(k)
                for k in (
                    "started_at",
                    "calls_this_run",
                    "sealed",
                    "resumed",
                    "status",
                    "remaining",
                )
            },
        ]
        if previous
        else [],
        "packets": [],
    }

    def checkpoint() -> None:
        temp = state_path.with_suffix(".part")
        temp.write_text(json.dumps(state, indent=2, default=str) + "\n")
        temp.replace(state_path)

    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + key},
        timeout=cfg.timeout_seconds,
        follow_redirects=False,
    ) as http:
        client = Client(http, cfg)
        try:
            # Warmup has only dates <= the frozen research end; no later labels used.
            for ticker in (*cfg.tickers, cfg.benchmark):
                path = destination / f"{ticker}-daily.json.gz"
                if path.exists():
                    packet = read(path)
                    if packet.get("profile_hash") != profile_hash:
                        raise CaptureStoppedError("resume_profile")
                    state["resumed"] += 1
                    continue
                packet = await client.get(f"/api/stock/{ticker}/ohlc/1d", {})
                keep = []
                for row in rows(packet):
                    at = timestamp(row.get("start_time", row.get("time")))
                    day_raw = row.get("date")
                    native_day = (
                        at.astimezone(ET).date()
                        if at
                        else date.fromisoformat(day_raw[:10])
                        if isinstance(day_raw, str)
                        else None
                    )
                    if native_day is None:
                        raise CaptureStoppedError("daily_unknown_native_date")
                    if native_day <= cfg.end:
                        keep.append(row)
                packet["payload"] = {"data": keep}
                seal(
                    path,
                    packet
                    | {
                        "ticker": ticker,
                        "kind": "daily",
                        "complete": True,
                        "profile_hash": profile_hash,
                    },
                )
                state["sealed"] += 1
            day = cfg.start
            cal = load_live_alpha_settings().calendar
            while day <= cfg.end:
                if is_session_day(day, cal):
                    for ticker in (*cfg.tickers, cfg.benchmark):
                        kinds = (
                            ("minute", "flow", "chain") if ticker in cfg.tickers else ("minute",)
                        )
                        for kind in kinds:
                            path = destination / day.isoformat() / f"{ticker}-{kind}.json.gz"
                            if path.exists():
                                packet = read(path)
                                if packet.get("profile_hash") != profile_hash or not packet.get(
                                    "complete"
                                ):
                                    raise CaptureStoppedError("resume_incomplete_or_mismatched")
                                state["resumed"] += 1
                            else:
                                if kind == "flow":
                                    packet = await flow(client, ticker, day)
                                else:
                                    route = (
                                        f"/api/stock/{ticker}/ohlc/1m"
                                        if kind == "minute"
                                        else f"/api/stock/{ticker}/option-chains"
                                    )
                                    params: dict[str, str | int] = (
                                        {"date": day.isoformat()}
                                        if kind == "minute"
                                        else {"date": day.isoformat(), "greeks": "true"}
                                    )
                                    packet = await client.get(route, params)
                                    records = rows(packet)
                                    if kind == "minute":
                                        stamps = [
                                            timestamp(r.get("start_time", r.get("time")))
                                            for r in records
                                        ]
                                        if not records or any(
                                            t is None or t.astimezone(ET).date() != day
                                            for t in stamps
                                        ):
                                            raise CaptureStoppedError("minute_date_unverified")
                                    packet.update(
                                        ticker=ticker,
                                        day=day.isoformat(),
                                        kind=kind,
                                        complete=True,
                                        date_evidence="native_minute_times"
                                        if kind == "minute"
                                        else "requested_chain_date_only_quality_B",
                                    )
                                seal(path, packet | {"profile_hash": profile_hash})
                                state["sealed"] += 1
                                if not packet["complete"]:
                                    raise CaptureStoppedError(
                                        "incomplete_flow_" + packet["stop_reason"]
                                    )
                            state["packets"].append(str(path.relative_to(destination)))
                            state.update(
                                calls_this_run=client.calls,
                                remaining=client.remaining,
                                last_day=day.isoformat(),
                            )
                            checkpoint()
                    print(
                        json.dumps(
                            {
                                "day": day.isoformat(),
                                "calls": client.calls,
                                "remaining": client.remaining,
                                "sealed": state["sealed"],
                                "resumed": state["resumed"],
                            }
                        ),
                        flush=True,
                    )
                day += timedelta(days=1)
            state["status"] = "complete"
        except CaptureStoppedError as error:
            state.update(
                status="stopped",
                reason=str(error),
                calls_this_run=client.calls,
                remaining=client.remaining,
            )
        finally:
            checkpoint()
    return state
