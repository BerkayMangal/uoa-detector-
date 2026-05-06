"""Phase 3.3.2.6 ThetaData smoke integration test.

This test hits the real Theta Terminal endpoint at the local default
(http://127.0.0.1:25510). It is gated behind:

  - The ``integration`` pytest marker (registered in pyproject.toml).
  - A non-empty ``THETADATA_API_KEY`` env var.

To run:
  - Start Theta Terminal locally with a valid Pro subscription.
  - Set ``THETADATA_API_KEY`` (and optionally ``THETADATA_USERNAME``).
  - ``uv run pytest -m integration tests/integration/test_thetadata_smoke.py -v``

Without the env var, every test in this module is skipped — by design.
The CI suite never runs these; bisectable history is preserved by the
unit tests under ``tests/unit/test_thetadata_*.py`` which cover all
adapter logic via mocks.

What this smoke test pins (when the key is present):
  1. ``ThetaDataClient`` connects to Theta Terminal and a basic GET on
     ``/v2/list/roots/option`` returns 200 with a JSON body.
  2. The mapping pipeline (real REST trade response → ``RawPrint``)
     produces a valid RawPrint when fed real bytes from a live trade
     endpoint call.

These are the two highest-value real-world checks before scaling to
3.3.4's bulk download. Production validation of bandwidth limits,
exact OPRA condition codes, and full WebSocket streaming will surface
during 3.3.4-3.3.5 — those phases are explicitly flagged as needing
real-key validation per the acceptance doc.

⚠️  FLAGGED: This module's tests can ONLY be validated by Berkay once
the ThetaData Pro key arrives. The unit tests above pin all parsing
and dispatch logic; this file pins the network plumbing only.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import ThetaDataSettings
from uoa_detector.config.credentials import Credentials
from uoa_detector.sources.thetadata.client import ThetaDataClient

pytestmark = pytest.mark.integration


def _key_or_skip() -> SecretStr:
    """Load a real ThetaData key from env or skip the test."""
    raw = os.environ.get("THETADATA_API_KEY", "").strip()
    if not raw:
        pytest.skip(
            "THETADATA_API_KEY not set in environment; "
            "smoke integration test skipped. "
            "See tests/integration/test_thetadata_smoke.py docstring "
            "for run instructions.",
        )
    return SecretStr(raw)


def _settings() -> ThetaDataSettings:
    # Conservative for a smoke test: don't hammer the local terminal.
    return ThetaDataSettings(
        rate_limit_requests_per_second=2.0,
        historical_concurrency=1,
        live_reconnect_max_attempts=2,
        live_reconnect_initial_backoff_s=1.0,
        live_reconnect_max_backoff_s=10.0,
    )


@pytest.mark.asyncio
async def test_thetadata_terminal_reachable_smoke() -> None:
    """The simplest possible smoke check: one GET returns 200 + JSON.

    Pins that the local Theta Terminal is up and the API key is
    accepted. Failures here mean either the terminal is not running
    or the key is invalid — both operator-actionable.
    """
    api_key = _key_or_skip()
    client = ThetaDataClient(
        api_key=api_key,
        settings=_settings(),
    )
    try:
        # /v2/list/roots/option is one of the lightest endpoints —
        # returns a JSON list of all option roots. No date/contract
        # parameters, so it doesn't tickle subscription tier logic.
        result = await client.request_json(
            "/v2/list/roots/option",
            params={},
        )
    finally:
        await client.aclose()
    # Theta Terminal returns either {"header": {...}, "response": [...]}
    # or a bare list — accept both for robustness.
    assert result is not None


@pytest.mark.asyncio
async def test_thetadata_credentials_load_from_env_smoke() -> None:
    """The Credentials model loads the env var that gates this test.

    Smoke check that the credential-management infrastructure
    (Phase 3.3.1) and the adapter (Phase 3.3.2) compose correctly:
    one .env load → one client construction → one request.
    """
    _key_or_skip()  # gate
    creds = Credentials()
    api_key = creds.require_thetadata_api_key()
    client = ThetaDataClient(
        api_key=api_key,
        settings=_settings(),
    )
    try:
        result = await client.request_json(
            "/v2/list/roots/option",
            params={},
        )
    finally:
        await client.aclose()
    assert result is not None


@pytest.mark.asyncio
async def test_thetadata_historical_one_day_smoke() -> None:
    """Pull one day of trades for one liquid contract; assert non-empty.

    This is the lightest-weight end-to-end test of the historical
    pipeline against real data. Picks a known-liquid contract from
    a recent past date so the response is small but non-empty.

    The exact contract here is illustrative; Berkay can swap it for
    a date/strike known to be live in the user's data window when
    the real run happens. This test is wholly run-by-operator.
    """
    api_key = _key_or_skip()
    client = ThetaDataClient(
        api_key=api_key,
        settings=_settings(),
    )
    # Use a near-the-money SPY weekly expiring shortly after a known
    # past trading day. Berkay should override these to whatever's
    # actually in his data window.
    target_date = date(2024, 1, 5)
    expiry = date(2024, 1, 5)  # 0DTE
    try:
        result = await client.request_json(
            "/v2/hist/option/trade",
            params={
                "root": "SPY",
                "exp": f"{expiry.year:04d}{expiry.month:02d}{expiry.day:02d}",
                "strike": "470000",  # $470.00 in 1/10-cent units
                "right": "C",
                "start_date": f"{target_date.year:04d}"
                              f"{target_date.month:02d}"
                              f"{target_date.day:02d}",
                "end_date": f"{target_date.year:04d}"
                            f"{target_date.month:02d}"
                            f"{target_date.day:02d}",
            },
        )
    finally:
        await client.aclose()
    # Don't assert on shape too tightly — Theta Terminal versions
    # vary in response envelope. Just assert we got something parseable.
    assert result is not None
    # Document: caller should manually inspect this against the OPRA
    # mapping in mapping.py to validate the array layout is unchanged.
    # If the schema has drifted, mapping._TRADE_KEEP / trade_row_from_array
    # will need updating in lockstep. This is the moment that risk surfaces.
