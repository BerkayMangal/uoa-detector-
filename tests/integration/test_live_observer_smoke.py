"""Phase 3.3.5.4 live observer smoke test.

Connects ``UnusualWhalesLiveSource`` to the real Unusual Whales
WebSocket, runs the full pipeline for ~30 seconds against liquid
US equities, and asserts at least one decision record emerges.

GATED BY:
  - ``UNUSUAL_WHALES_API_KEY`` env var present
  - US equity market hours: 09:30–16:00 ET, Monday–Friday
  - ``@pytest.mark.live_market`` (custom marker, not run by default)

Skipped outside any of these conditions. CI never runs this; CI only
runs ``pytest -m 'not integration and not live_market'`` which is the
default.

Why a separate marker (live_market) vs the existing 'integration':

  - 'integration' tests (Phase 3.3.2.6 + 3.3.3.6) hit external HTTP
    endpoints with stable responses regardless of when they run.
    Smoke runs with the right env vars succeed at any time.

  - 'live_market' tests need the market to be OPEN. UW's WebSocket
    flow stream emits zero events at 3am ET on a Sunday; running the
    test then would always fail / timeout. We refuse to run these
    outside market hours so the failure mode is 'skip' (clear) rather
    than 'timeout after 30s' (confusing).

ThetaData feed not exercised here:
  Phase 3.3.5.4 CLI gates --feeds thetadata behind a 'wired in Phase 4'
  error. The factory's ThetaData path is exercised by unit tests with
  mocked WS in Phase 3.3.3.3. A live ThetaData smoke would need a Pro
  key + Theta Terminal running locally + per-contract subscription
  enumeration — Phase 4 work.

Berkay's directive:
  Smoke tests are run by Berkay during market hours after the UW key
  is loaded. The agent never runs these; they are pinned by the
  marker + env-gating to prevent accidental execution.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from io import StringIO

import pytest
from pydantic import SecretStr

from uoa_detector.calibration import load_default_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.live.factory import build_live_sources
from uoa_detector.live.observer import LiveObserver
from uoa_detector.observability.output import NDJSONWriter
from uoa_detector.pipeline.orchestrator import Pipeline
from uoa_detector.pipeline.stages import default_stage_pipeline

pytestmark = pytest.mark.live_market


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def _is_market_open_now() -> bool:
    """Heuristic: US Eastern market hours, Mon-Fri.

    Approximated via UTC. Real exchange holidays not honoured (we
    refuse to run on holidays manually). For purpose of skipping
    tests during off-hours, this is conservative enough.
    """
    now_utc = datetime.now(UTC)
    # Mon=0, Sun=6
    if now_utc.weekday() >= 5:  # Sat / Sun
        return False
    # 09:30-16:00 ET ≈ 13:30-20:00 UTC (EST) or 14:30-21:00 UTC (EDT)
    # Use a generous window covering both DST states. Operator running
    # this manually outside market hours can override the marker filter
    # explicitly.
    hour = now_utc.hour
    # Generous window: 13:00-21:00 UTC covers both EST 09:00-17:00 and
    # EDT 09:00-17:00 with some slack on either side.
    return 13 <= hour < 21


def _gate_or_skip() -> SecretStr:
    """Skip unless UW key set AND market plausibly open."""
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "UNUSUAL_WHALES_API_KEY not set in environment; "
            "live_market smoke skipped.",
        )
    if not _is_market_open_now():
        pytest.skip(
            "US equity market does not appear to be open right now; "
            "live_market smoke skipped. Run during 09:30-16:00 ET "
            "Mon-Fri to exercise this test.",
        )
    return SecretStr(raw)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_observer_emits_records_against_real_uw() -> None:
    """Run the live observer for ~30s; assert a decision record emerged.

    Connects to UW only (ThetaData CLI wiring is Phase 4). Subscribes
    to a small set of liquid tickers; the test fails if the run
    completes the timeout without producing any output.
    """
    api_key = _gate_or_skip()
    creds = Credentials(unusual_whales_api_key=api_key)
    profile = load_default_profile()

    bundle = build_live_sources(
        feeds=("unusual_whales",),
        credentials=creds,
        thetadata_settings=profile.data_sources.thetadata,
        unusual_whales_settings=profile.data_sources.unusual_whales,
        tickers=["SPY", "QQQ", "AAPL", "MSFT", "TSLA"],
    )

    output_stream = StringIO()
    writer = NDJSONWriter(output_stream)

    pipeline = Pipeline(
        bundle.sources,
        list(default_stage_pipeline()),
        profile=profile,
        decision_record_writer=writer,
    )
    observer = LiveObserver(
        pipeline=pipeline,
        sources=bundle.sources,
        install_signal_handlers=False,  # avoid pytest signal interference
    )

    async def _stop_after_timeout() -> None:
        await asyncio.sleep(30.0)
        await observer.shutdown()

    stopper = asyncio.create_task(_stop_after_timeout())
    try:
        await observer.run()
    finally:
        if not stopper.done():
            stopper.cancel()

    output = output_stream.getvalue()
    # We expect at least one NDJSON record. UW's flow stream emits
    # frequently for SPY/QQQ during market hours.
    record_lines = [line for line in output.splitlines() if line.strip()]
    assert len(record_lines) > 0, (
        "live observer produced no decision records in 30s — UW WS "
        "may be down, key may be invalid, or market may be unusually "
        "quiet. Inspect UW status and retry."
    )
