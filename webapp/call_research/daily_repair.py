"""Bounded operator-only daily capture; native JSON and SQL rows are adapter boundaries."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML
from sqlalchemy import Table, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import previous_session_before
from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import session_factory
from webapp.calibration.model import Bar
from webapp.calibration.settings import calendar
from webapp.calibration.storage import DailyBar
from webapp.call_research import availability, coverage
from webapp.call_research.storage import DailyReceipt
from webapp.history_capture.capture import ROOT, CaptureStoppedError, Client, seal
from webapp.history_capture.capture import Settings as CaptureSettings
from webapp.ohlc import _to_float
from webapp.opening.model import utc


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    history_sessions: int
    max_requests: int
    quota_reserve: int
    spacing_seconds: float
    timeout_seconds: int
    max_packet_rows: int


def settings() -> Settings:
    return Settings(**YAML(typ="safe").load((ROOT / "profiles/daily_repair_v1.yaml").read_text()))


@dataclass(frozen=True)
class Plan:
    expected_day: date
    days: tuple[date, ...]
    names: tuple[str, ...]
    ready: int
    expected: int


class Fetcher(Protocol):
    calls: int
    remaining: int | None

    async def get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]: ...


def plan(engine: Engine, now: datetime) -> Plan:
    current = coverage.view(engine, utc(now))
    if current.expected_day is None or current.limited:
        raise ValueError("coverage_unavailable")
    names = tuple(row.ticker for row in current.rows if row.status != "ready")
    cfg = settings()
    if len(names) > cfg.max_requests:
        raise ValueError("membership_bound")
    days = [current.expected_day]
    for _ in range(cfg.history_sessions - 1):
        previous = previous_session_before(days[-1], calendar())
        if previous is None:
            raise ValueError("calendar_unknown")
        days.append(previous)
    return Plan(current.expected_day, tuple(days), names, current.ready, current.expected)


def bars(ticker: str, packet: dict[str, Any], wanted: tuple[date, ...]) -> tuple[Bar, ...]:
    data = packet["payload"].get("data")
    if not isinstance(data, list) or len(data) > settings().max_packet_rows:
        raise ValueError("daily_rows_unavailable_or_unbounded")
    grouped: dict[date, Bar] = {}
    for raw in data:
        if not isinstance(raw, dict):
            raise ValueError("malformed_daily_row")
        if raw.get("market_time") != "r":
            continue
        native = raw.get("date")
        if not isinstance(native, str):
            raise ValueError("native_date_unavailable")
        try:
            day = date.fromisoformat(native[:10])
        except ValueError:
            raise ValueError("native_date_invalid") from None
        if day not in wanted:
            continue
        values = [_to_float(raw.get(k)) for k in ("open", "high", "low", "close")]
        if any(v is None for v in values):
            raise ValueError("invalid_ohlc")
        volume = _to_float(raw.get("volume"))
        opening, high, low, close = cast(tuple[float, float, float, float], tuple(values))
        point = Bar(ticker, day, opening, high, low, close, volume)
        if not point.valid or (volume is not None and volume < 0):
            raise ValueError("invalid_ohlc")
        if day in grouped and grouped[day] != point:
            raise ValueError("conflicting_native_rows")
        grouped[day] = point
    return tuple(grouped[d] for d in sorted(grouped))


def ingest(
    engine: Engine,
    ticker: str,
    points: tuple[Bar, ...],
    at: datetime,
    clock: Callable[[], datetime],
) -> tuple[int, int, int]:
    # Stamp after the actual response; existing keys and first receipts never change.
    at = utc(at)
    keys = [digest((ticker, p.day)) for p in points]
    with session_factory(engine)() as session:
        old = {
            r.key: r.payload_json
            for r in session.scalars(select(DailyBar).where(DailyBar.key.in_(keys)))
        }
    rows = []
    conflicts = 0
    for point, key in zip(points, keys, strict=True):
        payload = dumps({k: getattr(point, k) for k in ("open", "high", "low", "close", "volume")})
        if key in old:
            conflicts += old[key] != payload
        else:
            rows.append(
                dict(key=key, ticker=ticker, day=point.day, observed_at=at, payload_json=payload)
            )
    inserted = append(engine, cast(Table, DailyBar.__table__), rows)
    receipts = availability.adopt(engine, (ticker,), utc(clock()), clock)
    return inserted, conflicts, receipts


async def repair(
    engine: Engine,
    fetcher: Fetcher,
    output: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    started = utc(clock())
    todo = plan(engine, started)
    output.mkdir(parents=True, exist_ok=False)
    state: dict[str, Any] = dict(
        version=settings().version,
        started_at=started,
        expected_day=todo.expected_day,
        before_ready=todo.ready,
        expected=todo.expected,
        planned_names=todo.names,
        profile_hash=digest(settings().model_dump()),
        rows=[],
        status="running",
    )

    def checkpoint() -> None:
        state.update(http_calls=fetcher.calls, remaining=fetcher.remaining)
        pending = output / "run.part"
        pending.write_text(dumps(state) + "\n")
        pending.replace(output / "run.json")

    checkpoint()
    # Existing table identities only. No signal/cohort/outcome or job-marker write.
    for model in (DailyBar, DailyReceipt):
        cast(Table, model.__table__).create(engine, checkfirst=True)
    try:
        for ticker in todo.names:
            packet = await fetcher.get(f"/api/stock/{ticker}/ohlc/1d", {})
            received = utc(clock())
            if received < started or datetime.fromisoformat(packet["received_at"]) > received:
                raise CaptureStoppedError("physical_clock_invalid")
            if plan_day(received) != todo.expected_day:
                raise CaptureStoppedError("settled_session_changed")
            seal(
                output / f"{ticker}.json.gz",
                packet | {"ticker": ticker, "kind": "operational_daily"},
            )
            row: dict[str, Any] = {"ticker": ticker, "received_at": received}
            try:
                points = bars(ticker, packet, todo.days)
                inserted, conflicts, receipts = ingest(engine, ticker, points, received, clock)
                row.update(
                    status="stored" if points else "no_data",
                    source_bars=len(points),
                    inserted=inserted,
                    conflicts=conflicts,
                    receipts=receipts,
                    absent_sessions=len(todo.days) - len(points),
                )
            except ValueError as error:
                row.update(status="rejected", reason=str(error))
            state["rows"].append(row)
            checkpoint()
        current = coverage.view(engine, utc(clock()))
        state.update(
            after_ready=current.ready,
            status="complete"
            if current.ready == current.expected
            and not any(r.get("conflicts") or r["status"] == "rejected" for r in state["rows"])
            else "partial",
        )
    except CaptureStoppedError as error:
        state.update(status="stopped", reason=str(error))
    except Exception as error:
        # Native/SQL exception text may contain connection metadata. Never persist it.
        state.update(status="stopped", reason="storage_or_source_" + type(error).__name__)
    finally:
        state["finished_at"] = utc(clock())
        checkpoint()
    return state


def plan_day(now: datetime) -> date | None:
    from webapp.research_readiness import settled_day

    return settled_day(now)


async def run(engine: Engine, output: Path) -> dict[str, Any]:
    todo, cfg = plan(engine, datetime.now(UTC)), settings()
    key = os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not key:
        raise CaptureStoppedError("missing_credential")
    bounded = CaptureSettings(
        version=cfg.version,
        start=min(todo.days),
        end=todo.expected_day,
        tickers=todo.names,
        benchmark="SPY",
        max_requests=cfg.max_requests,
        quota_reserve=cfg.quota_reserve,
        spacing_seconds=cfg.spacing_seconds,
        timeout_seconds=cfg.timeout_seconds,
        max_flow_pages=1,
        flow_page_limit=1,
    )
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + key},
        timeout=cfg.timeout_seconds,
        follow_redirects=False,
    ) as http:
        return await repair(engine, Client(http, bounded), output)


def main() -> int:
    import argparse
    from dataclasses import asdict

    from webapp.board.db import make_engine

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL must be securely bound; do not pass it as an argument")
    engine = make_engine(database_url)
    try:
        if not args.apply:
            print(dumps(asdict(plan(engine, datetime.now(UTC)))))
            return 0
        if not args.output:
            parser.error("--output is required with --apply")
        result = asyncio.run(run(engine, args.output))
        print(
            dumps(
                {
                    k: result.get(k)
                    for k in (
                        "status",
                        "before_ready",
                        "after_ready",
                        "expected",
                        "http_calls",
                        "remaining",
                    )
                }
            )
        )
        return 0 if result["status"] == "complete" else 2
    finally:
        engine.dispose()
