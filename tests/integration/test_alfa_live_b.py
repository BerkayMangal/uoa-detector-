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
from webapp.board import atm, catalysts, oi_confirm, regime
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
    regime.ensure_regime_tables(engine)
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


# ---------------------------------------------------------------------------
# B5a
# ---------------------------------------------------------------------------


async def test_live_market_tide(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    now = datetime.now(UTC)
    try:
        report = await regime.refresh_market_tide(client, sessions, settings=_settings(), now=now)
    finally:
        await client.aclose()
    assert report.degraded == ()
    if report.no_data:
        pytest.skip("market tide has no complete bucket yet (before the first bucket closes)")
    assert report.stored == (regime.SOURCE_TIDE,)
    with sessions() as s:
        buckets = regime.load_regime_inputs(s, now=now).tide_buckets
    if not buckets:
        pytest.skip("the stored tide bucket belongs to the previous session (pre-market run)")
    latest = buckets[-1]
    # Probed shape: 5-minute buckets (interval_5m=true), running totals, bucket already complete.
    assert latest.bucket_end - latest.bucket_start == timedelta(minutes=5)
    assert latest.bucket_end <= now
    assert latest.net_volume is not None


async def test_live_spot_exposures(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    now = datetime.now(UTC)
    try:
        report = await regime.refresh_spot_exposures(
            client, sessions, tickers=[_TICKER], settings=_settings(), now=now,
        )
    finally:
        await client.aclose()
    assert report.degraded == ()
    if report.no_data:
        pytest.skip("no regular-session spot-exposure rows yet (pre-market)")
    with sessions() as s:
        (reading,) = regime.load_regime_inputs(s, now=now).gamma
    assert reading.ticker == _TICKER
    assert reading.gamma_oi != 0
    local = reading.time.astimezone(_ET)
    assert (local.hour, local.minute) >= (9, 30)
    assert reading.session_first_time <= reading.time <= now + timedelta(minutes=5)
    assert reading.price is not None and reading.price > 0


async def test_live_gex_levels(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    now = datetime.now(UTC)
    try:
        report = await regime.refresh_gex_levels(
            client, sessions, tickers=[_TICKER], settings=_settings(), now=now,
        )
    finally:
        await client.aclose()
    assert report.stored == (regime.gex_source(_TICKER),), report
    with sessions() as s:
        (levels,) = regime.load_regime_inputs(s, now=now).gex
    assert levels.gamma_flip is not None and levels.gamma_flip > 0
    assert levels.time is not None


async def test_live_volatility_term_structure(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    settings = _settings()
    now = datetime.now(UTC)
    try:
        spy = await regime.refresh_iv_term_structure(client, sessions, settings=settings, now=now)
        vix = await regime.refresh_vix_spot(client, sessions, settings=settings, now=now)
    finally:
        await client.aclose()
    assert spy.stored == (regime.SOURCE_CURVE,), spy
    assert vix.stored == (regime.SOURCE_VIX,), vix
    with sessions() as s:
        inputs = regime.load_regime_inputs(s, now=now)
    curve = inputs.curve
    assert curve is not None
    cfg = settings.regime
    assert cfg.exclude_event_hump_max_dte < curve.short_dte < curve.long_dte
    assert abs(curve.short_dte - cfg.iv_anchor_short_dte) <= 20
    assert abs(curve.long_dte - cfg.iv_anchor_long_dte) <= 45
    assert 0 < curve.short_iv < 2 and 0 < curve.long_iv < 2  # fractions
    assert inputs.vix is not None
    assert 5 < inputs.vix.vix_spot < 100


async def test_live_greek_exposure(tmp_path: Path) -> None:
    client = _client()
    sessions = _sessions(tmp_path)
    now = datetime.now(UTC)
    try:
        report = await regime.refresh_gamma_history(
            client, sessions, tickers=[_TICKER], settings=_settings(), now=now,
        )
    finally:
        await client.aclose()
    assert report.stored == (regime.history_source(_TICKER),), report
    with sessions() as s:
        (history,) = regime.load_regime_inputs(s, now=now).history
    assert history.total_days >= 200  # about one year of daily rows
    assert 0 <= history.negative_days <= history.total_days
    assert 0 < history.percentile <= 100
    assert (now.astimezone(_ET).date() - history.as_of).days <= 7
