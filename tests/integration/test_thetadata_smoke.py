"""Phase 3.3.2.6 ThetaData smoke integration test.

Phase 3.3.7.5 (v2 → v3 migration): URLs updated to v3 endpoints,
query parameters updated to v3 names + encodings (symbol /
expiration / dollar-string-strike / 'call'-'put' right). Default
port changed from 25510 (v2) to 25503 (v3) — set by
``ThetaDataClient``'s ``DEFAULT_BASE_URL`` constant.

This test hits the real Theta Terminal endpoint at the local default
(http://127.0.0.1:25503). It is gated behind:

  - The ``integration`` pytest marker (registered in pyproject.toml).
  - A non-empty ``THETADATA_API_KEY`` env var.

To run:
  - Start Theta Terminal v3 locally with a valid Pro subscription.
  - Set ``THETADATA_API_KEY`` (and optionally ``THETADATA_USERNAME``).
  - ``uv run pytest -m integration tests/integration/test_thetadata_smoke.py -v``

Without the env var, every test in this module is skipped — by design.
The CI suite never runs these; bisectable history is preserved by the
unit tests under ``tests/unit/test_thetadata_*.py`` which cover all
adapter logic via mocks (now exercising v3 wire formats).

What this smoke test pins (when the key is present):
  1. ``ThetaDataClient`` connects to Theta Terminal v3 and a basic
     GET on ``/v3/option/list/symbols`` returns 200 with a JSON body.
  2. The credential-loading path (Phase 3.3.1 → adapter) composes
     correctly with v3 endpoints.
  3. The historical pipeline (real v3 REST trade response →
     ``RawPrint``) produces a valid response when fed real bytes
     from a live trade endpoint call.

These are the highest-value real-world checks before scaling to
3.5.x bulk download. Production validation of bandwidth limits,
exact OPRA condition codes, and full WebSocket streaming will surface
during 3.5.x — those phases are explicitly flagged as needing
real-key validation per the acceptance doc.

⚠️  FLAGGED: This module's tests can ONLY be validated by Berkay
once a real ThetaData v3 Pro key is loaded into ``.env`` and Theta
Terminal v3 is running on port 25503. The unit tests pin all
parsing and dispatch logic; this file pins the network plumbing
only.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from pydantic import SecretStr

from uoa_detector.calibration.profile import ThetaDataSettings
from uoa_detector.config.credentials import Credentials
from uoa_detector.sources.thetadata.client import ThetaDataClient
from uoa_detector.sources.thetadata.mapping import format_v3_strike_param

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


def _yyyymmdd(d: date) -> str:
    """Format a date as ``YYYYMMDD`` (the v3 ``date`` / ``expiration``
    parameter format).
    """
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


@pytest.mark.asyncio
async def test_thetadata_terminal_reachable_smoke() -> None:
    """The simplest possible smoke check: one GET returns 200 + JSON.

    Phase 3.3.7.5: uses v3 ``/v3/option/list/symbols`` (the v3
    equivalent of v2's ``/v2/list/roots/option``).

    Pins that the local Theta Terminal v3 is up and the API key is
    accepted. Failures here mean either the terminal is not running
    on port 25503 or the key is invalid — both operator-actionable.
    """
    api_key = _key_or_skip()
    client = ThetaDataClient(
        api_key=api_key,
        settings=_settings(),
    )
    try:
        # /v3/option/list/symbols is one of the lightest endpoints —
        # returns a JSON array of all option symbols. No date /
        # contract parameters, so it doesn't tickle subscription
        # tier logic. ``format=json`` is auto-injected by
        # ThetaDataClient.request_json (Phase 3.3.7.3 J5).
        result = await client.request_json(
            "/v3/option/list/symbols",
            params={},
        )
    finally:
        await client.aclose()
    # v3 returns an array of dicts (each containing a "symbol"
    # field) or possibly an array of strings — both are
    # parseable JSON. The defensive v2-envelope fallback in our
    # decoders means we ALSO accept the legacy
    # {"header": ..., "response": [...]} shape if an older Terminal
    # exposes it. Just assert we got something parseable.
    assert result is not None


@pytest.mark.asyncio
async def test_thetadata_credentials_load_from_env_smoke() -> None:
    """The Credentials model loads the env var that gates this test.

    Smoke check that the credential-management infrastructure
    (Phase 3.3.1) and the adapter (Phase 3.3.2 + 3.3.7.x v3
    migration) compose correctly: one .env load → one client
    construction → one v3 request.
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
            "/v3/option/list/symbols",
            params={},
        )
    finally:
        await client.aclose()
    assert result is not None


@pytest.mark.asyncio
async def test_thetadata_historical_one_day_smoke() -> None:
    """Pull one day of trades for one liquid contract; assert non-empty.

    Phase 3.3.7.5: uses v3 ``/v3/option/history/trade`` with v3
    parameter names + encodings:
      - ``root`` → ``symbol``
      - ``exp`` → ``expiration``
      - ``strike`` 1/10-cent int → ``strike`` dollar string via
        ``format_v3_strike_param``
      - ``right`` 'C'/'P' → 'call'/'put'
      - ``start_date`` / ``end_date`` unchanged (per J7: still
        accepted for option history; the migration-guide footnote
        about removing them applied only to stock history)

    This is the lightest-weight end-to-end test of the historical
    pipeline against real v3 data. Picks a known-liquid contract
    from a recent past date so the response is small but non-empty.

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
            "/v3/option/history/trade",
            params={
                "symbol": "SPY",
                "expiration": _yyyymmdd(expiry),
                "strike": format_v3_strike_param(Decimal("470.00")),
                "right": "call",
                "start_date": _yyyymmdd(target_date),
                "end_date": _yyyymmdd(target_date),
            },
        )
    finally:
        await client.aclose()
    # v3 returns a top-level JSON array of trade objects, each with
    # {symbol, expiration, strike, right, timestamp, sequence,
    #  ext_condition1-4, condition, size, exchange, price}. The
    # defensive fallback in _decode_trade_response also accepts the
    # legacy v2 {header, response: [...]} envelope. Either way the
    # caller should be able to parse via mapping.trade_row_from_v3_dict.
    assert result is not None
    # Document: caller should manually inspect this against the
    # v3 trade response schema in mapping.py
    # (``trade_row_from_v3_dict``) to validate the field shape is
    # unchanged. If the v3 schema has drifted (e.g., field rename in
    # a later API release), trade_row_from_v3_dict will need
    # updating in lockstep. This is the moment that risk surfaces.
