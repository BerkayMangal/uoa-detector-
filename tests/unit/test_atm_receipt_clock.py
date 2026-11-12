"""ATM acquisition completion clocks stay distinct from provider quote clocks."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from webapp.board.atm import (
    AlfaAtm,
    AlfaAtmExpiry,
    ensure_atm_tables,
    read_board_atm,
    refresh_atm_chains,
)
from webapp.board.db import make_engine, session_factory
from webapp.board.refresher import SoftCap, run_atm_cycle
from webapp.board.settings import load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesNotFoundError,
    UnusualWhalesTransientError,
)

NOW = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)
EXPIRY = date(2026, 11, 27)
VENDOR_DAY = date(2026, 11, 11)
VENDOR_AT = datetime(2026, 11, 11, 20, 59, tzinfo=UTC)
SETTINGS = load_board_settings()


@dataclass
class Clock:
    at: datetime = NOW

    def __call__(self) -> datetime:
        return self.at


class Client:
    def __init__(
        self, clock: Clock, *, delays: dict[str, int], mode: str = "ok",
    ) -> None:
        self.clock = clock
        self.delays = delays
        self.mode = mode
        self.calls: list[str] = []
        self.last_daily_request_count: int | None = 0
        self.circuit_breaker = SimpleNamespace(is_open=lambda: False)

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append(path)
        self.clock.at += timedelta(seconds=self.delays.get(path, 0))
        if path.endswith("/expiry-breakdown"):
            return {"data": [{"expires": EXPIRY.isoformat()}]}
        if self.mode == "transient":
            raise UnusualWhalesTransientError("synthetic failure")
        if self.mode == "not_found":
            raise UnusualWhalesNotFoundError("synthetic absence", status_code=404)
        if self.mode == "empty":
            return {"data": []}
        if self.mode == "malformed":
            return {"data": [{"option_symbol": "invalid"}]}
        ticker = path.split("/")[3]
        return {"data": [{
            "option_symbol": f"{ticker}261127{right}00100000",
            "bid": "1.00", "ask": "1.10", "stock_price": "100.00",
            "date": VENDOR_DAY.isoformat(), "tape_time": VENDOR_AT.isoformat(),
        } for right in ("C", "P")]}

    async def aclose(self) -> None:
        pass


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    instance = make_engine(f"sqlite:///{tmp_path / 'receipt.db'}")
    ensure_atm_tables(instance)
    yield instance
    instance.dispose()


def seed_expiries(engine: Engine, *tickers: str) -> None:
    with session_factory(engine)() as session, session.begin():
        session.add_all([
            AlfaAtmExpiry(ticker=ticker, expiry=EXPIRY, fetched_at=NOW)
            for ticker in tickers
        ])


async def test_each_delayed_ticker_uses_its_own_decoded_response_clock(engine: Engine) -> None:
    seed_expiries(engine, "SPY", "QQQ")
    clock = Clock()
    client = Client(clock, delays={
        "/api/stock/SPY/atm-chains": 40,
        "/api/stock/QQQ/atm-chains": 70,
    })
    report = await refresh_atm_chains(
        client, session_factory(engine), wanted={"SPY": [EXPIRY], "QQQ": [EXPIRY]},
        settings=SETTINGS, now=NOW, clock=clock,
    )
    rows = read_board_atm(engine, ["SPY", "QQQ"])
    assert report.requests == 2
    assert rows["SPY"][0].fetched_at == NOW + timedelta(seconds=40)
    assert rows["QQQ"][0].fetched_at == NOW + timedelta(seconds=110)
    assert rows["SPY"][0].trade_date == rows["QQQ"][0].trade_date == VENDOR_DAY
    with session_factory(engine)() as session:
        for ticker in ("SPY", "QQQ"):
            row = session.get(AlfaAtm, (ticker, EXPIRY))
            assert row is not None and row.call_tape_time is not None and row.put_tape_time is not None
            assert row.call_tape_time.replace(tzinfo=UTC) == VENDOR_AT
            assert row.put_tape_time.replace(tzinfo=UTC) == VENDOR_AT


async def test_production_cycle_clock_includes_expiry_request_delay(engine: Engine) -> None:
    clock = Clock()
    client = Client(clock, delays={
        "/api/stock/SPY/expiry-breakdown": 240,
        "/api/stock/SPY/atm-chains": 45,
    })
    report = await run_atm_cycle(
        client, engine, SETTINGS, wanted={"SPY": [EXPIRY]},
        soft_cap=SoftCap(SETTINGS.refresh.daily_request_soft_cap), clock=clock,
    )
    assert report.requests == 2
    assert read_board_atm(engine, ["SPY"])["SPY"][0].fetched_at == NOW + timedelta(seconds=285)


@pytest.mark.parametrize("mode", ["empty", "malformed", "transient", "not_found"])
async def test_unsuccessful_or_empty_response_does_not_freshen_saved_quote(
    engine: Engine, mode: str,
) -> None:
    seed_expiries(engine, "SPY")
    clock = Clock()
    await refresh_atm_chains(
        Client(clock, delays={}), session_factory(engine), wanted={"SPY": [EXPIRY]},
        settings=SETTINGS, now=NOW, clock=clock,
    )
    before = read_board_atm(engine, ["SPY"])["SPY"]
    await refresh_atm_chains(
        Client(clock, delays={"/api/stock/SPY/atm-chains": 180}, mode=mode),
        session_factory(engine), wanted={"SPY": [EXPIRY]},
        settings=SETTINGS, now=NOW, clock=clock,
    )
    assert clock.at == NOW + timedelta(seconds=180)
    assert read_board_atm(engine, ["SPY"])["SPY"] == before


async def test_historical_caller_without_clock_preserves_explicit_now(engine: Engine) -> None:
    seed_expiries(engine, "SPY")
    clock = Clock()
    await refresh_atm_chains(
        Client(clock, delays={"/api/stock/SPY/atm-chains": 180}),
        session_factory(engine), wanted={"SPY": [EXPIRY]}, settings=SETTINGS, now=NOW,
    )
    assert read_board_atm(engine, ["SPY"])["SPY"][0].fetched_at == NOW
