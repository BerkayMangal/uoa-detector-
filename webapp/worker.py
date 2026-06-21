"""Live worker — poll UW flow into Postgres so the screener shows live signals.

Runs the same detection pipeline the backtest used, but driven by
``UnusualWhalesFlowPollSource`` (live REST flow) and writing to Postgres via
the store the webapp reads. Designed to run as a background task inside the
FastAPI process (one Railway service), opt-in via ``LIVE_TICKERS``.

Enrichment axes (gamma/IV/etc.) run with their NoOp defaults for now — they
score neutral, so live cards show the core flow axes (flow, clustering,
time-of-day, sweep/size). Wiring live UW enrichment providers is the next step.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from uoa_detector.backtest.cell_runner import fusion_stages_with_uw
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
from uoa_detector.sources.unusual_whales.flow_poll import UnusualWhalesFlowPollSource

_logger = logging.getLogger(__name__)

_DEFAULT_PROFILE = Path("profiles/v5_default.yaml")
_RESTART_BACKOFF_S = 30.0


def _normalize_pg(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


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
        "poll_interval_s": float(os.environ.get("LIVE_POLL_INTERVAL_S", "20")),
        "min_premium": Decimal(os.environ.get("LIVE_MIN_PREMIUM", "25000")),
    }


async def run_live_worker(
    *,
    tickers: list[str],
    database_url: str,
    poll_interval_s: float = 20.0,
    min_premium: Decimal = Decimal(25000),
    profile_path: Path = _DEFAULT_PROFILE,
) -> None:
    """Run the live poll→pipeline→Postgres loop until cancelled.

    Resilient: if the pipeline raises, log and restart after a backoff. Cancel
    (process shutdown) closes the source and store cleanly.
    """
    profile = load_profile(profile_path)
    store = SqliteBacktestStore(_normalize_pg(database_url))
    run_id = f"live-{datetime.now(UTC).date().isoformat()}"
    _logger.info("live worker started: run_id=%s tickers=%s", run_id, tickers)

    def _ensure_run() -> None:
        # One run per day. On a same-day restart (redeploy / crash) the run
        # already exists — adopt it so signals keep appending, rather than
        # colliding on the run_id primary key. pipeline.run() finishes the run
        # in its finally, so we re-adopt at the top of every loop iteration.
        if store.get_run(run_id) is None:
            store.start_run(
                profile=profile, universe_id="live", run_id=run_id,
                dataset_window_start=datetime.now(UTC),
            )
        else:
            store._active_run_id = run_id

    try:
        while True:
            client = None
            source = None
            # Whole loop body in the restart try: a transient DB/UW blip during
            # setup (run adoption, client, stage wiring) must back off + retry,
            # not kill live ingestion permanently (it would freeze silently
            # while the LIVE badge keeps pulsing).
            try:
                _ensure_run()
                client = UnusualWhalesClient(
                    api_key=Credentials().unusual_whales_api_key,
                    settings=profile.data_sources.unusual_whales,
                )
                source = UnusualWhalesFlowPollSource(
                    client, tickers,
                    poll_interval_s=poll_interval_s, min_premium=min_premium,
                )
                # Full 8-axis enrichment wired to live UW. Providers are cached
                # per-ticker and degrade to neutral on timeout/error (D7), so a
                # slow or 401-gated axis never stalls the stream.
                stages = fusion_stages_with_uw(
                    client, profile.data_sources.unusual_whales,
                )
                pipeline = Pipeline(
                    [source], stages, profile=profile, store=store,
                    context=PipelineContext(profile=profile),
                )
                await pipeline.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("live worker error; restarting in %ss", _RESTART_BACKOFF_S)
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
