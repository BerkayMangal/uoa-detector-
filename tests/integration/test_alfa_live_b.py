"""Phase 5.2 FAZ B live Unusual Whales endpoint tests (data layers B2a, B4a, B5a, B6a).

One test per endpoint the FAZ B data layers call. Each runs the real job (or its
fetch) against the live API and a tmp sqlite database, then asserts real rows in
the probed shape (probe 2026-09-15). No contract, expiry or date is hard-coded.

Gating:
  - ``integration`` marker;
  - skipped when ``UNUSUAL_WHALES_API_KEY`` is not set.

Run once with the key (never echo it):
  set -a; source .env; set +a; uv run pytest tests/integration/test_alfa_live_b.py -q
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from webapp.board import atm, catalysts, oi_confirm
from webapp.board.db import make_engine, session_factory
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.calibration.profile import UnusualWhalesSettings
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_TICKER = "SPY"
_ET = ZoneInfo("America/New_York")


def _key_or_skip() -> SecretStr:
    raw = os.environ.get("UNUSUAL_WHALES_API_KEY", "").strip()
    if not raw:
        pytest.skip("UNUSUAL_WHALES_API_KEY not set; live UW tests skipped.")
    return SecretStr(raw)


def _client() -> UnusualWhalesClient:
    return UnusualWhalesClient(api_key=_key_or_skip(), settings=UnusualWhalesSettings())


def _settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


def _sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    atm.ensure_atm_tables(engine)
    oi_confirm.ensure_oi_confirm_tables(engine)
    catalysts.ensure_catalyst_tables(engine)
    return session_factory(engine)


# ---------------------------------------------------------------------------
# B2a
# ---------------------------------------------------------------------------


async def test_live_expiry_breakdown(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    try:
        report = await atm.refresh_expiry_breakdown(
            client, sessions, tickers=[_TICKER], settings=_settings(),
            now=datetime.now(UTC),
        )
    finally:
        await client.aclose()
    assert report.degraded == () and report.no_data == ()
    with sessions() as s:
        listed = atm.load_listed_expiries(s, _TICKER)
    assert len(listed) >= 5, listed
    assert list(listed) == sorted(listed)
    assert listed[-1] > date.today()


async def test_live_atm_chains(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    settings = _settings()
    now = datetime.now(UTC)
    try:
        await atm.refresh_expiry_breakdown(
            client, sessions, tickers=[_TICKER], settings=settings, now=now,
        )
        report = await atm.refresh_atm_chains(
            client, sessions, wanted={_TICKER: []}, settings=settings, now=now,
        )
    finally:
        await client.aclose()
    assert report.degraded == () and report.malformed_rows == 0
    assert report.stored_rows >= 1, report
    with sessions() as s:
        rows = atm.load_atm(s, _TICKER)
    assert rows
    for row in rows:
        assert row.stock_price is not None and row.stock_price > 0
        # One strike per expiry, near spot (ATM is the nearest listed strike).
        assert abs(row.strike - row.stock_price) / row.stock_price < 0.05
        assert row.call_ask is not None or row.put_ask is not None


# ---------------------------------------------------------------------------
# B4a
# ---------------------------------------------------------------------------


def _previous_session(day: date) -> date:
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


async def test_live_option_contract_historic(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    settings = _settings()
    now = datetime.now(UTC)
    today = now.astimezone(_ET).date()
    try:
        # Anchor on a real ATM contract about three weeks out, so it traded last session.
        await atm.refresh_expiry_breakdown(
            client, sessions, tickers=[_TICKER], settings=settings, now=now,
        )
        await atm.refresh_atm_chains(
            client, sessions, wanted={_TICKER: [today + timedelta(days=21)]},
            settings=settings, now=now,
        )
        with sessions() as s:
            symbols = [
                r.call_symbol for r in s.scalars(select(atm.AlfaAtm)).all()
                if r.call_symbol and r.expiry >= today + timedelta(days=7)
            ]
        assert symbols, "no ATM contract at least a week out to anchor on"
        symbol = symbols[-1]
        raw = await client.request_json(
            oi_confirm.HISTORIC_PATH.format(symbol=symbol),
            params={"limit": oi_confirm.HISTORIC_LIMIT},
        )
        trade_date = _previous_session(today)
        report = await oi_confirm.confirm_open_interest(
            client, sessions,
            flagged=[oi_confirm.FlaggedContract(
                option_symbol=symbol, ticker=_TICKER, trade_date=trade_date, flagged_size=1,
            )],
            settings=settings, now=now,
        )
    finally:
        await client.aclose()
    chains = raw["chains"]  # probed shape: rows under "chains", newest first
    assert 1 <= len(chains) <= oi_confirm.HISTORIC_LIMIT
    dates = [date.fromisoformat(r["date"]) for r in chains]
    assert dates == sorted(dates, reverse=True)
    assert all(isinstance(r["open_interest"], int) for r in chains)
    assert report.degraded == () and report.no_data == ()
    with sessions() as s:
        view = oi_confirm.load_oi_confirm(s, symbol, trade_date)
    assert view is not None
    if view.status == "bekliyor":
        assert report.awaiting == ((symbol, trade_date),)
    else:
        assert view.oi_t is not None and view.oi_t1 is not None
        assert view.t1_date is not None and view.t1_date > trade_date
        assert view.delta_oi == view.oi_t1 - view.oi_t


async def test_live_earnings(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    now = datetime.now(UTC)
    try:
        report = await catalysts.refresh_earnings(
            client, sessions, tickers=["MU", "AAPL"], settings=_settings(), now=now,
        )
    finally:
        await client.aclose()
    assert report.degraded == ()
    upcoming = []
    for ticker in ("MU", "AAPL"):
        with sessions() as s:
            events, fetches = catalysts.load_catalyst_inputs(s, ticker)
        assert any(f.source == "earnings" and f.last_success_at for f in fetches)
        upcoming.extend(e for e in events if e.kind == "earnings")
    assert upcoming, "neither MU nor AAPL has an upcoming earnings row"
    for event in upcoming:
        assert event.starts_at.astimezone(_ET).date() >= now.astimezone(_ET).date()
        assert event.timing in {"premarket", "postmarket", "unknown"}


async def test_live_fda_calendar(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    now = datetime.now(UTC)
    try:
        report = await catalysts.refresh_fda(
            client, sessions, tickers=["IONS", "MRK"], settings=_settings(), now=now,
        )
    finally:
        await client.aclose()
    assert report.degraded == ()
    events = []
    for ticker in ("IONS", "MRK"):
        with sessions() as s:
            rows, _ = catalysts.load_catalyst_inputs(s, ticker)
        events.extend(e for e in rows if e.kind == "fda")
    assert events, "no upcoming FDA rows for IONS or MRK"
    today = now.astimezone(_ET).date()
    for event in events:
        assert event.precision in {"day", "vague"}
        if event.precision == "day":
            assert date.fromisoformat(event.when_key) >= today


async def test_live_economic_calendar(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    settings = _settings()
    now = datetime.now(UTC)
    try:
        report = await catalysts.refresh_macro(client, sessions, settings=settings, now=now)
    finally:
        await client.aclose()
    assert report.degraded == () and report.no_data == ()
    with sessions() as s:
        events, fetches = catalysts.load_catalyst_inputs(s, _TICKER)
    assert any(f.source == "macro" and f.last_success_at for f in fetches)
    macro = [e for e in events if e.kind == "macro"]
    assert macro, "no economic-calendar row matched the profile's macro names"
    names = {n.lower() for n in settings.catalyst.macro_event_names}
    for event in macro:
        assert event.timing in names
        assert event.precision == "exact"
        assert abs((event.starts_at - now).days) <= settings.catalyst.macro_horizon_days + 7
