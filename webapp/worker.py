"""Live worker — poll UW flow into Postgres so the screener shows live signals.

Runs the same detection pipeline the backtest used, but driven by
``UnusualWhalesFlowPollSource`` (live REST flow) and writing to Postgres via
the store the webapp reads. Designed to run as a background task inside the
FastAPI process (one Railway service), opt-in via ``LIVE_TICKERS``.

Enrichment is live Unusual Whales (Phase 5.0.5). The stages come from
``build_live_stage_pipeline(client, profile, degrade_transient_errors=True)``:
the screener's stage order, every provider on the one shared client, one
catalyst provider for M22 and M24. A transient UW error (rate limit, daily
quota, 5xx, open circuit breaker) scores that axis as no-data for the event
instead of aborting the run and restarting the worker. An auth error or a
programming error still propagates to the restart path below, so a bad key is
logged loudly rather than scored as missing data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages import build_live_stage_pipeline
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.flow_poll import UnusualWhalesFlowPollSource
from webapp.board.telemetry import AlfaTelemetryWriter

_logger = logging.getLogger(__name__)

_DEFAULT_PROFILE = Path("profiles/v5_default.yaml")
_RESTART_BACKOFF_S = 30.0
# Phase 4.42: the live worker commits every signal immediately. The store's
# default batch of 100 is right for backtests, but live flow is a few signals
# per minute: with batching, today's run stays invisible (page reads STALE) until
# the 100th signal, and every restart/redeploy silently drops up to 99 buffered
# signals. A persistence setting, not a scoring threshold (D8).
_LIVE_FLUSH_THRESHOLD = 1


def _normalize_pg(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _open_live_store(database_url: str) -> SqliteBacktestStore:
    """Open the webapp's store for live ingestion: one commit per signal.

    Replay-safe (Phase 4.43): the flow source re-emits its last 10 minutes of
    alerts every time it is rebuilt (in-process restart, Railway redeploy), with
    the same event ids as rows already stored. A plain store fails that commit
    and re-sends the rejected row on every later write, freezing the dashboard
    for the rest of the day.
    """
    return SqliteBacktestStore(
        _normalize_pg(database_url),
        flush_threshold=_LIVE_FLUSH_THRESHOLD,
        replay_safe=True,
    )


def live_config_from_env() -> dict[str, object] | None:
    """Read live-worker config from the environment, or None if not opted in.

    Opt-in is ``LIVE_TICKERS`` (comma-separated) plus a UW key. Absent either,
    the worker does not start and the webapp just serves stored signals.
    """
    tickers_raw = os.environ.get("LIVE_TICKERS", "").strip()
    if not tickers_raw:
        return None
    if not os.environ.get("UNUSUAL_WHALES_API_KEY"):
        _logger.warning("LIVE_TICKERS set but UNUSUAL_WHALES_API_KEY missing; live worker disabled")
        return None
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        _logger.warning("LIVE_TICKERS set but DATABASE_URL missing; live worker disabled")
        return None
    tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
    return {
        "tickers": tickers,
        "database_url": database_url,
        # 60s FLOOR (Phase 4.28): with N live tickers + per-signal enrichment,
        # a 20s interval over RTH alone can exceed the UW daily cap. The floor
        # keeps the budget safe even if LIVE_POLL_INTERVAL_S is set lower in the
        # deploy env; raise the env var to go slower, never faster than 60s.
        "poll_interval_s": max(60.0, float(os.environ.get("LIVE_POLL_INTERVAL_S", "60"))),
        "min_premium": Decimal(os.environ.get("LIVE_MIN_PREMIUM", "25000")),
    }


async def run_live_worker(
    *,
    tickers: list[str],
    database_url: str,
    poll_interval_s: float = 60.0,
    min_premium: Decimal = Decimal(25000),
    profile_path: Path = _DEFAULT_PROFILE,
) -> None:
    """Run the live poll→pipeline→Postgres loop until cancelled.

    Resilient: if the pipeline raises, log and restart after a backoff. Cancel
    (process shutdown) closes the source and store cleanly.
    """
    profile = load_profile(profile_path)
    store = _open_live_store(database_url)
    _logger.info("live worker started: tickers=%s", tickers)

    def _ensure_run() -> str:
        # One run per UTC day, recomputed each iteration so a worker that runs
        # for days ROLLS OVER at midnight (a fixed start-of-process run_id would
        # keep dumping later days into the first day's run). On a same-day
        # restart the run exists -> adopt it (append) rather than collide on PK.
        run_id = f"live-{datetime.now(UTC).date().isoformat()}"
        if store.get_run(run_id) is None:
            store.start_run(
                profile=profile, universe_id="live", run_id=run_id,
                dataset_window_start=datetime.now(UTC),
            )
        else:
            store._active_run_id = run_id
        return run_id

    try:
        while True:
            client = None
            source = None
            writer: AlfaTelemetryWriter | None = None
            # Whole loop body in the restart try: a transient DB/UW blip during
            # setup (run adoption, client, stage wiring) must back off + retry,
            # not kill live ingestion permanently (it would freeze silently
            # while the LIVE badge keeps pulsing).
            try:
                _ensure_run()
                client = UnusualWhalesClient(
                    api_key=Credentials().require_unusual_whales_api_key(),
                    settings=profile.data_sources.unusual_whales,
                )
                source = UnusualWhalesFlowPollSource(
                    client, tickers,
                    poll_interval_s=poll_interval_s, min_premium=min_premium,
                )
                # Live UW enrichment (Phase 5.0.5): the screener's stages and
                # order on the shared client, with every provider except M23's
                # wrapped so a transient UW error (rate limit, daily quota,
                # 5xx, open breaker) scores that axis as no-data for the event
                # instead of aborting the run. Timeouts stay per-stage (D7).
                # Auth and programming errors still reach the restart path.
                stages = build_live_stage_pipeline(
                    client, profile, degrade_transient_errors=True,
                )
                # Phase 5.2.A0c: keep each event's stage telemetry and print
                # meta (fill side, option chain) in the alfa_* side tables.
                # Bound to this iteration's degrading wrappers and to the run
                # the store is writing. The writer never raises, and
                # Pipeline.run closes it when the stream ends.
                writer = AlfaTelemetryWriter.for_stages(
                    database_url=database_url,
                    run_id_source=lambda: store.active_run_id,
                    stages=stages,
                )
                pipeline = Pipeline(
                    [source], stages, profile=profile, store=store,
                    context=PipelineContext(profile=profile),
                    decision_record_writer=writer,
                )
                await pipeline.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("live worker error; restarting in %ss", _RESTART_BACKOFF_S)
                if writer is not None:
                    writer.close()
                if source is not None:
                    with contextlib.suppress(Exception):
                        await source.close()
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()
                await asyncio.sleep(_RESTART_BACKOFF_S)
                continue
    except asyncio.CancelledError:
        _logger.info("live worker cancelled; shutting down")
        raise
    finally:
        with contextlib.suppress(Exception):
            store.close()
