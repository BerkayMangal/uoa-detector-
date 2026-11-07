"""Owned forward observer; no provider call and no outcome-dependent decision."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from uoa_detector.live_alpha.calendar import MarketMode, classify, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import normalize_database_url
from webapp.fundamental_context import view as fundamental_view
from webapp.fusion_live import adapters, archive, config, policy, storage
from webapp.fusion_live.model import Arm
from webapp.semantic_news import view as semantic_view
from webapp.shadow_capture import bindings
from webapp.shadow_capture import config as shadow_config
from webapp.shadow_capture import storage as shadow_storage
from webapp.shadow_capture.model import Record as ShadowRecord
from webapp.shadow_capture.model import aware, digest

STATE: dict[str, object] = {}
_ET = ZoneInfo("America/New_York")


def enabled() -> bool:
    return config.enabled()


def safe_status() -> dict[str, object]:
    fields = ("status", "at", "activation_at", "candidates", "decisions", "pending", "day_results")
    return {k: v for k, v in STATE.items() if k in fields and isinstance(v, str | int | bool)}


def configured_engine(database_url: str) -> Engine:
    url = normalize_database_url(database_url)
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return create_engine(url, future=True, pool_pre_ping=True)
    original = parsed.query.get("options", os.getenv("PGOPTIONS", ""))
    if not isinstance(original, str):
        raise ValueError("ambiguous_database_options")
    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        connect_args={
            "options": original
            + " -c statement_timeout=2000 -c lock_timeout=1000 -c max_parallel_workers_per_gather=0"
        },
    )


def serializable(value: object) -> Any:
    """JSON adapter only; aware clocks retain their actual UTC instant."""

    def convert(v: object) -> str:
        if isinstance(v, datetime):
            return aware(v).isoformat()
        if isinstance(v, date):
            return v.isoformat()
        raise TypeError("unsupported_fusion_json")

    return json.loads(json.dumps(value, default=convert, sort_keys=True, allow_nan=False))


def _views(
    engine: Engine, now: datetime, day: date
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    news = asdict(semantic_view.build(engine, now, day))
    fundamentals = asdict(fundamental_view.build(engine, now, day))

    def keyed(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {
            str(row["candidate_key"]): row
            for item in adapters.sequence(report.get("rows"))
            if (row := adapters.mapping(item)) and isinstance(row.get("candidate_key"), str)
        }

    return keyed(news), keyed(fundamentals)


def _eligible(
    engine: Engine, candidate: ShadowRecord, active: storage.Record, now: datetime
) -> bool:
    if (
        candidate.kind != "candidate"
        or candidate.confirmed_at is None
        or active.available_at is None
        or candidate.confirmed_at < active.available_at
        or candidate.confirmed_at > now
    ):
        return False
    started = archive.source_started(engine, candidate)
    original = shadow_storage.active(engine, now)
    return (
        started is not None
        and started >= active.available_at
        and original is not None
        and bindings.valid_candidate(engine, candidate, original, now)
    )


def tick(engine: Engine, now: datetime, *, clock: Callable[[], datetime]) -> dict[str, object]:
    at = aware(now)
    active = storage.active(engine, at)
    if active is None or active.available_at is None:
        return {"status": "activation_unavailable", "candidates": 0, "decisions": 0, "pending": 0}
    day = at.astimezone(_ET).date()
    if not shadow_storage.available(engine):
        return {"status": "waiting", "candidates": 0, "decisions": 0, "pending": 0}
    candidates = shadow_storage.list_events(
        engine, "candidate", at, day=day, limit=config.settings().candidate_limit + 1
    )
    if len(candidates) > config.settings().candidate_limit:
        return {"status": "source_limit", "candidates": 0, "decisions": 0, "pending": 0}
    usable = [c for c in candidates if _eligible(engine, c, active, at)]
    unfinished: list[tuple[ShadowRecord, tuple[Arm, ...]]] = []
    decisions = pending = 0
    for candidate in usable:
        missing: list[Arm] = []
        for arm in config.settings().arms:
            key = digest((config.settings().version, active.key, candidate.key, arm))
            saved = storage.get(engine, key, at, confirmed=False)
            if saved is None:
                missing.append(arm)
            else:
                decisions += 1
        if missing:
            unfinished.append((candidate, tuple(missing)))
    if not unfinished:
        return {
            "status": "active" if usable else "waiting",
            "candidates": len(usable),
            "decisions": decisions,
            "pending": 0,
        }
    news_rows: dict[str, Mapping[str, Any]] = {}
    fund_rows: dict[str, Mapping[str, Any]] = {}
    # No outcome/reference view is called on this path. Only contemporaneous
    # feature read models may affect a pre-entry decision.
    if any(
        arm != "base"
        and at
        < c.confirmed_at
        + timedelta(
            seconds=shadow_config.settings().entry_delay_seconds
            - config.settings().entry_lead_seconds
        )
        for c, arms in unfinished
        for arm in arms
        if c.confirmed_at is not None
    ):
        news_rows, fund_rows = _views(engine, at, day)
    for candidate, arms in unfinished:
        assert candidate.confirmed_at is not None
        moment = aware(clock())
        if moment < at:
            raise ValueError("fusion_clock_reversed")
        cutoff = candidate.confirmed_at + timedelta(
            seconds=shadow_config.settings().entry_delay_seconds
            - config.settings().entry_lead_seconds
        )
        claim_key = digest((config.settings().version, active.key, candidate.key, "claim"))
        claim = storage.get(engine, claim_key, moment, confirmed=False)
        if claim is None:
            raw = archive.snapshot(engine, candidate, moment)
            storage.put(
                engine,
                key=claim_key,
                kind="claim",
                at=moment,
                day=day,
                candidate_key=candidate.key,
                payload={
                    "identity": config.identity(),
                    "activation_key": active.key,
                    "candidate_checksum": candidate.checksum,
                    "ticker": candidate.payload["ticker"],
                    "cutoff": cutoff.isoformat(),
                    "source": raw,
                },
                clock=clock,
            )
            claim = storage.get(engine, claim_key, aware(clock()))
        if claim is None or claim.available_at is None:
            pending += len(arms)
            continue
        if (
            claim.payload.get("identity") != config.identity()
            or claim.payload.get("activation_key") != active.key
            or claim.payload.get("candidate_checksum") != candidate.checksum
            or claim.candidate_key != candidate.key
        ):
            raise ValueError("fusion_claim_unverified")
        moment = aware(clock())
        if moment < claim.available_at:
            raise ValueError("fusion_claim_clock_reversed")
        ticker = str(candidate.payload["ticker"])
        news = adapters.semantic(
            news_rows.get(candidate.key), candidate_key=candidate.key, ticker=ticker, now=moment
        )
        financials = adapters.fundamental(
            fund_rows.get(candidate.key), candidate_key=candidate.key, ticker=ticker, now=moment
        )
        for arm in arms:
            decision_at = aware(clock())
            if decision_at < moment:
                raise ValueError("fusion_clock_reversed")
            verdict = policy.evaluate(arm, decision_at, cutoff, news, financials)
            if verdict.state == "PENDING":
                pending += 1
                continue
            feature = archive.features(
                candidate,
                adapters.mapping(claim.payload.get("source")),
                claim.available_at,
                decision_at,
                news,
                financials,
            )
            payload = {
                "identity": config.identity(),
                "activation_key": active.key,
                "candidate_checksum": candidate.checksum,
                "ticker": ticker,
                "cutoff": cutoff.isoformat(),
                "state": verdict.state,
                "reasons": list(verdict.reasons),
                "used_at": [
                    t.isoformat()
                    for t in sorted(
                        set((candidate.confirmed_at, claim.available_at, *verdict.used_at))
                    )
                ],
                "news": asdict(news),
                "fundamental": asdict(financials),
                "feature_row": feature.model_dump(mode="json"),
            }
            saved = storage.put(
                engine,
                key=digest((config.settings().version, active.key, candidate.key, arm)),
                kind="decision",
                at=decision_at,
                day=day,
                candidate_key=candidate.key,
                arm=arm,
                payload=serializable(payload),
                clock=clock,
            )
            decisions += int(saved is not None)
    return {
        "status": "active" if usable else "waiting",
        "candidates": len(usable),
        "decisions": decisions,
        "pending": pending,
    }


def _latest_completed(now: datetime) -> date | None:
    calendar = load_live_alpha_settings().calendar
    current = aware(now).astimezone(_ET).date() - timedelta(days=1)
    for _ in range(10):
        eligible = is_session_day(current, calendar)
        if eligible is None:
            return None
        if eligible:
            return current
        current -= timedelta(days=1)
    return None


def close_day(engine: Engine, now: datetime, *, clock: Callable[[], datetime]) -> int:
    from webapp.fusion_live import scoring

    at = aware(now)
    active = storage.active(engine, at)
    day = _latest_completed(at)
    if (
        active is None
        or active.available_at is None
        or day is None
        or day < active.available_at.astimezone(_ET).date()
    ):
        return 0
    key = digest((config.settings().version, active.key, day.isoformat(), "day_result"))
    if storage.get(engine, key, at, confirmed=False) is not None:
        return 0
    result = scoring.build_day(engine, day, at)
    if result.candidates == 0 or not result.session_closed:
        return 0
    moment = aware(clock())
    if moment < at:
        raise ValueError("fusion_clock_reversed")
    saved = storage.put(
        engine,
        key=key,
        kind="day_result",
        at=moment,
        day=day,
        candidate_key=active.key,
        payload={
            "identity": config.identity(),
            "activation_key": active.key,
            "result": serializable(asdict(result)),
        },
        clock=clock,
    )
    return int(saved is not None)


async def loop(
    database_url: str,
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> None:
    if not enabled() or (max_cycles is not None and max_cycles < 1):
        return
    now = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    engine = configured_engine(database_url)
    STATE.clear()
    STATE.update(status="starting")
    try:
        storage.ensure(engine)
        active = storage.activate(engine, now)
        STATE.update(activation_at=active.available_at.isoformat() if active.available_at else "")
        cycles = 0
        while True:
            try:
                result = tick(engine, aware(now()), clock=now)
                STATE.update(result)
                STATE.update(
                    day_results=close_day(engine, aware(now()), clock=now),
                    at=aware(now()).isoformat(),
                )
            except Exception:
                STATE.update(status="storage_or_decision_failed", at=aware(now()).isoformat())
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            live = (
                classify(aware(now()), load_live_alpha_settings().calendar).mode is MarketMode.LIVE
            )
            await pause(
                config.settings().poll_seconds if live else config.settings().idle_poll_seconds
            )
    finally:
        engine.dispose()
