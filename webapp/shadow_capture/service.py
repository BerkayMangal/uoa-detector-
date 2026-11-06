"""Owned, isolated prospective collector; ordinary HTTP GET cannot start it."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.live_alpha.calendar import MarketMode, classify
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.board.db import normalize_database_url
from webapp.board.quota_ledger import ensure_quota_tables
from webapp.board.refresher import _LIVE_CALIBRATION_PROFILE
from webapp.shadow_capture import bindings, collector, config, storage
from webapp.shadow_capture.model import aware
from webapp.timed_measurement.collector import QuoteClient
from webapp.timed_measurement.transport import QuoteTransport

STATE: dict[str, object] = {}


def enabled() -> bool:
    return config.enabled()


def safe_status() -> dict[str, object]:
    allowed = {
        "status",
        "at",
        "activation_at",
        "candidates",
        "observed",
        "pending",
        "missing",
        "requests",
        "reserved_requests",
        "source_scans",
    }
    return {k: v for k, v in STATE.items() if k in allowed and isinstance(v, str | int | bool)}


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


def _client(clock: Callable[[], datetime]) -> QuoteClient:
    cfg = config.settings()
    return QuoteTransport(
        api_key=Credentials().require_unusual_whales_api_key(),
        settings=load_profile(_LIVE_CALIBRATION_PROFILE).data_sources.unusual_whales,
        timeout_seconds=cfg.request_timeout_seconds,
        max_response_bytes=cfg.max_body_bytes,
        clock=clock,
    )


async def loop(
    database_url: str,
    *,
    client_factory: Callable[[], QuoteClient] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> None:
    if not enabled() or (max_cycles is not None and max_cycles < 1):
        return
    now = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    engine = configured_engine(database_url)
    client: QuoteClient | None = None
    STATE.clear()
    STATE.update(status="starting")
    try:
        storage.ensure(engine)
        ensure_quota_tables(engine)
        activation = storage.activate(engine, now)
        if activation.confirmed_at is None:
            STATE.update(status="activation_unverified")
            return
        STATE.update(activation_at=activation.confirmed_at.isoformat())
        client = client_factory() if client_factory is not None else _client(now)
        cycles = 0
        while True:
            try:
                source = bindings.discover(engine, aware(now()), clock=now)
                result = await collector.tick(engine, client, aware(now()), clock=now)
                STATE.update(source)
                STATE.update(result)
                STATE.update(at=aware(now()).isoformat())
            except Exception:
                # Raw provider/SQL exceptions never enter public health.
                STATE.update(status="storage_or_capture_failed", at=aware(now()).isoformat())
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
        if client is not None:
            await client.aclose()
        engine.dispose()
