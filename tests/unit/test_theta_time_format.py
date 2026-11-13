"""Phase 5.70.2: Theta Terminal v3 window times carry at most milliseconds.

Terminal v3 answers HTTP 400 to ``10:00:00.000000`` and 200 to ``10:00:00.000``
(reproduced 13 Nov 2026). The automatic connector mapped that 400 to
``terminal_unreachable`` and never consumed a job.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from scripts import theta_auto_connector as connector
from scripts import theta_mac_connector as legacy

from tests.unit.test_theta_mac_connector import SYMBOL, FakeOpener, payload
from uoa_detector.options_alpha.theta_quotes import ThetaQuoteClient, terminal_clock

ODD = datetime(2026, 11, 9, 15, 0, 0, 123456, tzinfo=UTC)  # 10:00:00.123456 ET
WHOLE = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
CLOCKS: list[Callable[..., str]] = [connector.terminal_clock, legacy.terminal_clock, terminal_clock]


@pytest.mark.parametrize("clock", CLOCKS)
def test_start_rounds_up_end_rounds_down_whole_seconds_keep_three_digits(clock: Callable[..., str]) -> None:
    assert clock(ODD, round_up=True) == "10:00:00.124"
    assert clock(ODD, round_up=False) == "10:00:00.123"
    assert clock(WHOLE, round_up=True) == "10:00:00.000"
    assert clock(WHOLE, round_up=False) == "10:00:00.000"
    exact_ms = WHOLE + timedelta(milliseconds=7)
    assert clock(exact_ms, round_up=True) == clock(exact_ms, round_up=False) == "10:00:00.007"


@pytest.mark.parametrize("clock", CLOCKS)
def test_rounding_never_moves_start_before_or_end_after_the_window(clock: Callable[..., str]) -> None:
    for micro in (1, 499, 500, 999, 1001, 999_999):
        moment = WHOLE + timedelta(microseconds=micro)
        local = moment.astimezone(connector.ET).replace(tzinfo=None)
        up = datetime.combine(local.date(), datetime.strptime(clock(moment, round_up=True), "%H:%M:%S.%f").time())
        down = datetime.combine(local.date(), datetime.strptime(clock(moment, round_up=False), "%H:%M:%S.%f").time())
        assert down <= local <= up
        assert up - down <= timedelta(milliseconds=1)


@pytest.mark.parametrize("module", [connector, legacy])
def test_connector_window_query_uses_milliseconds_and_keeps_packet_window(module: object) -> None:
    transport = module.Transport(  # type: ignore[attr-defined]
        "https://uoa.example", "http://127.0.0.1:25503", "private-user", "private-password")
    transport._local = FakeOpener(json.dumps(payload()).encode())
    result = transport.window(module.Contract.from_occ(SYMBOL), ODD, "entry")  # type: ignore[attr-defined]
    params = parse_qs(urlsplit(transport._local.calls[0][0].full_url).query)
    assert params["start_time"] == ["10:00:00.124"]
    assert params["end_time"] == ["10:02:00.123"]
    assert result["window_start"] == ODD.isoformat()
    assert result["window_end"] == (ODD + timedelta(seconds=120)).isoformat()


@pytest.mark.asyncio
async def test_quote_client_query_uses_milliseconds() -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(472)

    client = ThetaQuoteClient(transport=httpx.MockTransport(handle))
    await client.fetch_window(option_symbol=SYMBOL, session=ODD.astimezone(connector.ET).date(),
                              window_start=ODD, window_end=ODD + timedelta(seconds=120))
    assert seen["start_time"] == "10:00:00.124"
    assert seen["end_time"] == "10:02:00.123"
