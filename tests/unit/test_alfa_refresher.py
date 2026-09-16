"""Phase 5.2.A2: the board refresher (``webapp/board/refresher.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher"),
§4.4 and §5 A2; decisions P16 and P17.

The live run is seeded into a tmp sqlite file. The fake UW client serves
option-contracts and flow rows trimmed from live responses captured 2026-09-15.

Pins:
  - quotes cover open journal legs first (critical), then each row's dominant
    contract in row order, then every other contract; one call per
    underlying, chunked by ``refresh.max_symbols_per_request``;
  - requested and returned symbols are diffed (``returned=False``); untraded
    rows keep null NBBO;
  - exit depth for the dominant contracts of the top
    ``refresh.exit_depth_top_k`` rows only;
  - soft cap: once ``x-uw-daily-req-count`` reaches the cap on the same UTC
    day, only critical quotes continue (also mid-cycle); a count from an
    earlier day, or no count, never pauses;
  - journal legs are parsed defensively;
  - a degraded fetch writes nothing and keeps the previous row; daily-limit
    and auth errors propagate from a cycle;
  - the loop: closed-market sleep, breaker-open skip, daily-limit backoff, auth
    errors logged with a cadence wait, one client for many cycles, closed on
    exit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.board.quotes import (
    QuoteSnapshot,
    ensure_quotes_tables,
    read_depths,
    read_quotes,
    upsert_quotes,
)
from webapp.board.refresher import (
    SoftCap,
    board_refresh_loop,
    journal_legs,
    run_quotes_cycle,
    signal_legs,
)
from webapp.board.settings import BoardSettings, load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_BOARD_PROFILE = _REPO / "profiles" / "board_v1.yaml"
_CALIBRATION = _REPO / "profiles" / "v5_default.yaml"
_SETTINGS = load_board_settings(_BOARD_PROFILE)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)  # Tuesday, inside RTH
_RUN = "live-2026-09-15"
_SIGNAL_TS = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)

_CONTRACT_ROWS: dict[str, dict[str, Any]] = {
    "SPY260915C00757000": {
        "option_symbol": "SPY260915C00757000", "nbbo_bid": "1.23", "nbbo_ask": "1.24",
        "last_price": "1.24", "volume": 147025, "open_interest": 1228,
        "last_tape_time": "2026-09-15T15:30:28Z",
    },
    "SPY260918P00760000": {
        "option_symbol": "SPY260918P00760000", "nbbo_bid": "6.98", "nbbo_ask": "7.01",
        "last_price": "6.98", "volume": 36745, "open_interest": 107149,
        "last_tape_time": "2026-09-15T15:30:26Z",
    },
    "SPY260918C00684000": {
        "option_symbol": "SPY260918C00684000", "nbbo_bid": None, "nbbo_ask": None,
        "last_price": None, "volume": 0, "open_interest": 25,
        "last_tape_time": "2026-09-15T10:30:34Z",
    },
    "SMCI260918C00037000": {
        "option_symbol": "SMCI260918C00037000", "nbbo_bid": "0.87", "nbbo_ask": "0.91",
        "last_price": "0.89", "volume": 1876, "open_interest": 4650,
        "last_tape_time": "2026-09-15T15:26:21Z",
    },
}
_FLOW_ROWS: dict[str, dict[str, Any]] = {
    "SMCI260918C00037000": {
        "option_chain_id": "SMCI260918C00037000", "executed_at": "2026-09-15T15:26:21.567000Z",
        "nbbo_bid": "0.87", "nbbo_ask": "0.91", "nbbo_bid_size": 137, "nbbo_ask_size": 108,
        "nbbo_bid_time": "2026-09-15T15:26:21.566000Z", "nbbo_ask_time": "2026-09-15T15:26:21.517000Z",
    },
}

# Phase 5.2.A3: the loop also fetches the net-premium tape and ticker info.
# Trimmed from the live NVDA net-prem-ticks and /info responses captured 2026-09-15.
_TAPE_ROWS: list[dict[str, Any]] = [
    {
        "date": "2026-09-15", "tape_time": "2026-09-15T15:29:00.000000Z", "net_call_premium": "638346.00",
        "net_put_premium": "-1206342.00", "net_call_volume": 3421, "net_put_volume": -6248,
        "call_volume": 15430, "put_volume": 8079, "net_delta": "172878.594859157335195500",
    },
]
_INFO_ROWS: dict[str, dict[str, Any]] = {
    "SPY": {"symbol": "SPY", "sector": None, "issue_type": "ETF"},
    "SMCI": {"symbol": "SMCI", "sector": "Technology", "issue_type": "Common Stock"},
}
# Phase 5.2.B2b: the loop also fetches the ATM straddle (expiry list once per ET day).
_EXPIRY_ROWS: list[dict[str, Any]] = [
    {"expires": "2026-09-18", "open_interest": 531720, "volume": 25428, "chains": 150},
    {"expires": "2026-09-25", "open_interest": 42125, "volume": 6353, "chains": 124},
]

# (event_id, ticker, type, strike, dte, premium, fill_side, recorded chain)
_SPECS = [
    ("e1", "SPY", "call", "757", 0, "500000", "at_ask", "SPY260915C00757000"),
    ("e2", "SPY", "call", "684", 3, "100000", "at_ask", None),
    ("e3", "SPY", "put", "760", 3, "300000", "at_ask", "SPY260918P00760000"),
    ("e4", "SMCI", "call", "37", 3, "200000", None, None),  # legacy row without meta
]


def _seed(url: str) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(_SPECS) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (eid, ticker, kind, strike, dte, premium, _side, _chain) in enumerate(_SPECS):
            pr = build_print(
                event_id=eid, ts=_SIGNAL_TS + timedelta(seconds=i), ticker=ticker,
                option_type=kind,  # type: ignore[arg-type]
                strike=strike, dte=dte, premium=premium,
            )
            store.add(
                EnrichedEvent(print=pr),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = make_engine(url)
    try:
        ensure_telemetry_tables(engine)
        with Session(engine) as session:
            for eid, ticker, kind, strike, dte, _premium, side, chain in _SPECS:
                if side is None:
                    continue
                session.add(
                    AlfaPrintMeta(
                        run_id=_RUN, event_id=eid, ticker=ticker, option_chain=chain,
                        fill_side=side, option_type=kind, strike=strike,
                        expiry=(_SIGNAL_TS + timedelta(days=dte)).date(),
                        print_ts=_SIGNAL_TS, written_at=_SIGNAL_TS,
                    ),
                )
            session.commit()
    finally:
        engine.dispose()


class _FakeClient:
    def __init__(
        self,
        *,
        count: int | None = None,
        count_after_call: int | None = None,
        breaker_open: bool = False,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_daily_request_count = count
        self._count_after_call = count_after_call
        self._breaker_open = breaker_open
        self.circuit_breaker = SimpleNamespace(is_open=lambda: self._breaker_open)
        self.error = error
        self.closed = False

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        self.calls.append((path, dict(params or {})))
        if self._count_after_call is not None:
            self.last_daily_request_count = self._count_after_call
        if self.error is not None:
            raise self.error
        if path.endswith("/option-contracts"):
            symbols = (params or {})["option_symbol[]"]
            return {"data": [_CONTRACT_ROWS[s] for s in symbols if s in _CONTRACT_ROWS]}
        if path.endswith("/flow"):
            symbol = path.split("/")[3]
            return {"data": [_FLOW_ROWS[symbol]] if symbol in _FLOW_ROWS else [], "date": "2026-09-15"}
        if path.endswith("/expiry-breakdown"):
            return {"data": list(_EXPIRY_ROWS)}
        if path.endswith("/atm-chains"):
            return {"data": []}
        if path.endswith("/net-prem-ticks"):
            return {"data": list(_TAPE_ROWS)}
        if path.endswith("/info"):
            return {"data": _INFO_ROWS[path.split("/")[3]]}
        raise AssertionError(path)

    async def aclose(self) -> None:
        self.closed = True


class _Journal:
    def __init__(self, trades: list[Any]) -> None:
        self.trades = trades
        self.statuses: list[str | None] = []

    def list(self, status: str | None = None) -> list[Any]:
        self.statuses.append(status)
        return [t for t in self.trades if status is None or t.status == status]


def _trade(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "ticker": "NVDA", "instrument": "call", "strike": 180.0, "expiry": "2026-09-18", "status": "open",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _refresh(**changes: object) -> BoardSettings:
    return _SETTINGS.model_copy(update={"refresh": _SETTINGS.refresh.model_copy(update=changes)})


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'board.db'}"


async def _cycle(
    url: str,
    client: _FakeClient,
    *,
    settings: BoardSettings = _SETTINGS,
    journal: _Journal | None = None,
    soft_cap: SoftCap | None = None,
) -> Any:
    engine = make_engine(url)
    try:
        ensure_quotes_tables(engine)
        return await run_quotes_cycle(
            client, engine, settings,  # type: ignore[arg-type]
            reader=BoardSignalReader(engine=engine),
            journal=journal or _Journal([]),  # type: ignore[arg-type]
            soft_cap=soft_cap or SoftCap(settings.refresh.daily_request_soft_cap),
            clock=lambda: _NOW,
        )
    finally:
        engine.dispose()


def _quote_calls(client: _FakeClient) -> list[tuple[str, list[str]]]:
    return [
        (path.split("/")[3], params["option_symbol[]"])
        for path, params in client.calls
        if path.endswith("/option-contracts")
    ]


def _flow_calls(client: _FakeClient) -> list[str]:
    return [path.split("/")[3] for path, _ in client.calls if path.endswith("/flow")]


# ---------------------------------------------------------------------------
# Leg selection
# ---------------------------------------------------------------------------


def test_signal_legs_put_dominant_contracts_first(url: str) -> None:
    from webapp.board.aggregate import build_board_rows

    _seed(url)
    engine = make_engine(url)
    try:
        rows = build_board_rows(BoardSignalReader(engine=engine).load_run(_RUN), _SETTINGS.aggregation)
    finally:
        engine.dispose()
    assert [(r.ticker, r.direction) for r in rows] == [("SPY", "up"), ("SPY", "down"), ("SMCI", "up")]
    assert signal_legs(rows) == [
        ("SPY", "SPY260915C00757000"),  # dominant of SPY up (recorded chain)
        ("SPY", "SPY260918P00760000"),  # dominant of SPY down
        ("SMCI", "SMCI260918C00037000"),  # dominant of SMCI up (legacy: built from the contract)
        ("SPY", "SPY260918C00684000"),  # the other SPY up contract (no chain recorded)
    ]


def test_journal_legs_are_parsed_defensively() -> None:
    trades = [
        _trade(),
        _trade(instrument="shares"),
        _trade(instrument=" PUT ", strike=175.5, expiry="2026-09-18T00:00:00"),
        _trade(expiry="18/09/2026"),
        _trade(expiry="2026-09-11"),  # expired
        _trade(strike=None),
        _trade(strike=0.0),
        _trade(ticker=""),
        _trade(expiry=None),
        _trade(ticker="smci", strike=36.5),
    ]
    assert journal_legs(trades, _NOW.date()) == [  # type: ignore[arg-type]
        ("NVDA", "NVDA260918C00180000"),
        ("NVDA", "NVDA260918P00175500"),
        ("SMCI", "SMCI260918C00036500"),
    ]


# ---------------------------------------------------------------------------
# One cycle
# ---------------------------------------------------------------------------


async def test_cycle_quotes_journal_legs_then_signal_contracts_then_top_k_depth(url: str) -> None:
    _seed(url)
    client = _FakeClient(count=5000)
    journal = _Journal([_trade(), _trade(status="closed", ticker="AMD")])

    report = await _cycle(url, client, journal=journal)

    assert journal.statuses == ["open"]
    assert _quote_calls(client) == [
        ("NVDA", ["NVDA260918C00180000"]),
        ("SPY", ["SPY260915C00757000", "SPY260918P00760000", "SPY260918C00684000"]),
        ("SMCI", ["SMCI260918C00037000"]),
    ]
    assert _flow_calls(client) == ["SPY260915C00757000", "SPY260918P00760000", "SMCI260918C00037000"]
    assert all(params == {"limit": 1} for path, params in client.calls if path.endswith("/flow"))
    assert (report.run_id, report.requests, report.quotes_written, report.depths_written) == (_RUN, 6, 5, 1)
    assert (report.degraded_fetches, report.skipped_non_critical) == (0, False)

    engine = make_engine(url)
    try:
        quotes = read_quotes(engine, [*_CONTRACT_ROWS, "NVDA260918C00180000"])
        depths = read_depths(engine, ["SMCI260918C00037000", "SPY260915C00757000"])
    finally:
        engine.dispose()
    assert quotes["NVDA260918C00180000"].returned is False
    assert (quotes["SPY260915C00757000"].nbbo_bid, quotes["SPY260915C00757000"].fetched_at) == (1.23, _NOW)
    untraded = quotes["SPY260918C00684000"]
    assert untraded.returned is True
    assert (untraded.nbbo_bid, untraded.last_tape_time) == (None, None)
    assert set(depths) == {"SMCI260918C00037000"}
    assert depths["SMCI260918C00037000"].nbbo_bid_size == 137


async def test_symbols_are_chunked_per_underlying(url: str) -> None:
    _seed(url)
    client = _FakeClient()
    report = await _cycle(url, client, settings=_refresh(max_symbols_per_request=2, exit_depth_top_k=0))
    assert _quote_calls(client) == [
        ("SPY", ["SPY260915C00757000", "SPY260918P00760000"]),
        ("SPY", ["SPY260918C00684000"]),
        ("SMCI", ["SMCI260918C00037000"]),
    ]
    assert _flow_calls(client) == []
    assert report.requests == 3


async def test_top_k_limits_depth_calls(url: str) -> None:
    _seed(url)
    client = _FakeClient()
    await _cycle(url, client, settings=_refresh(exit_depth_top_k=1))
    assert _flow_calls(client) == ["SPY260915C00757000"]


async def test_soft_cap_reached_mid_cycle_pauses_non_critical_fetches(
    url: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url)
    client = _FakeClient(count_after_call=_SETTINGS.refresh.daily_request_soft_cap)
    with caplog.at_level(logging.WARNING, logger="webapp.board.refresher"):
        report = await _cycle(url, client, journal=_Journal([_trade()]))
    assert _quote_calls(client) == [("NVDA", ["NVDA260918C00180000"])]
    assert _flow_calls(client) == []
    assert (report.requests, report.skipped_non_critical) == (1, True)
    assert "soft cap" in caplog.text


async def test_soft_cap_keeps_critical_legs_of_a_ticker(url: str) -> None:
    _seed(url)
    soft_cap = SoftCap(_SETTINGS.refresh.daily_request_soft_cap)
    soft_cap.observe(24500, _NOW - timedelta(minutes=1))
    client = _FakeClient()
    held = _trade(ticker="SPY", instrument="put", strike=760.0, expiry="2026-09-18")
    report = await _cycle(url, client, journal=_Journal([held]), soft_cap=soft_cap)
    assert _quote_calls(client) == [("SPY", ["SPY260918P00760000"])]
    assert _flow_calls(client) == []
    assert report.skipped_non_critical is True


async def test_soft_cap_from_an_earlier_day_or_no_count_never_pauses(url: str) -> None:
    _seed(url)
    yesterday = SoftCap(_SETTINGS.refresh.daily_request_soft_cap)
    yesterday.observe(29000, _NOW - timedelta(days=1))
    client = _FakeClient()
    report = await _cycle(url, client, soft_cap=yesterday)
    assert (report.requests, report.skipped_non_critical) == (5, False)

    unknown = SoftCap(_SETTINGS.refresh.daily_request_soft_cap)
    unknown.observe(None, _NOW)
    assert unknown.reached(_NOW) is False


def test_soft_cap_bookkeeping() -> None:
    cap = SoftCap(100)
    cap.observe(99, _NOW)
    assert cap.reached(_NOW) is False
    cap.observe(100, _NOW)
    assert cap.reached(_NOW) is True
    assert cap.reached(_NOW + timedelta(days=1)) is False
    cap.observe(100, _NOW + timedelta(days=1))  # unchanged value: not re-stamped
    assert cap.reached(_NOW + timedelta(days=1)) is False


async def test_degraded_fetches_write_nothing_and_keep_previous_rows(url: str) -> None:
    _seed(url)
    earlier = _NOW - timedelta(minutes=5)
    engine = make_engine(url)
    try:
        ensure_quotes_tables(engine)
        upsert_quotes(
            engine,
            [QuoteSnapshot("SPY260915C00757000", "SPY", 1.0, 1.1, 1.05, 10, 1228, None, True)],
            fetched_at=earlier,
        )
    finally:
        engine.dispose()
    client = _FakeClient(error=UnusualWhalesTransientError("returned HTTP 503"))
    report = await _cycle(url, client)
    assert (report.requests, report.degraded_fetches, report.quotes_written) == (5, 5, 0)

    engine = make_engine(url)
    try:
        kept = read_quotes(engine, ["SPY260915C00757000"])["SPY260915C00757000"]
    finally:
        engine.dispose()
    assert (kept.nbbo_bid, kept.fetched_at) == (1.0, earlier)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), id="daily-limit"),
        pytest.param(UnusualWhalesAuthError("returned HTTP 401"), id="auth"),
    ],
)
async def test_daily_limit_and_auth_errors_propagate_from_a_cycle(url: str, error: Exception) -> None:
    _seed(url)
    with pytest.raises(type(error)):
        await _cycle(url, _FakeClient(error=error))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class _Stop(BaseException):
    """Ends the loop from the injected sleep."""


async def _run_loop(
    url: str,
    client: _FakeClient,
    *,
    now: datetime = _NOW,
    max_sleeps: int = 1,
    journal: Any = None,
) -> tuple[list[float], list[object]]:
    made: list[object] = []
    slept: list[float] = []

    def _factory(uw_settings: object) -> _FakeClient:
        made.append(uw_settings)
        return client

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= max_sleeps:
            raise _Stop

    with pytest.raises(_Stop):
        await board_refresh_loop(
            database_url=url,
            profile_path=_CALIBRATION,
            board_profile_path=_BOARD_PROFILE,
            client_factory=_factory,  # type: ignore[arg-type]
            journal_factory=lambda _url: journal if journal is not None else _Journal([]),
            clock=lambda: now,
            sleep=_sleep,
        )
    return slept, made


async def test_loop_sleeps_while_the_market_is_closed(url: str) -> None:
    client = _FakeClient()
    saturday = datetime(2026, 9, 19, 15, 30, tzinfo=UTC)
    slept, made = await _run_loop(url, client, now=saturday, max_sleeps=2)
    closed_sleep = _SETTINGS.refresh.closed_market_sleep_seconds
    assert slept == [closed_sleep, closed_sleep]
    assert client.calls == []
    assert len(made) == 1
    assert client.closed is True


async def test_loop_skips_the_cycle_while_the_breaker_is_open(url: str) -> None:
    _seed(url)
    client = _FakeClient(breaker_open=True)
    slept, _made = await _run_loop(url, client)
    assert slept == [_SETTINGS.refresh.cadence_seconds]
    assert client.calls == []


async def test_loop_backs_off_on_the_daily_limit(url: str) -> None:
    _seed(url)
    client = _FakeClient(error=UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"))
    slept, _made = await _run_loop(url, client)
    assert slept == [_SETTINGS.refresh.daily_limit_backoff_seconds]
    assert len(client.calls) == 1


async def test_loop_keeps_one_client_across_cycles_and_closes_it(url: str) -> None:
    _seed(url)
    client = _FakeClient()
    slept, made = await _run_loop(url, client, max_sleeps=3)
    assert slept == [_SETTINGS.refresh.cadence_seconds] * 3
    assert len(made) == 1
    # Per cycle: SPY and SMCI quotes, three depth calls, (B2b) two atm-chains and (A3) two
    # net-premium tapes; the expiry list and ticker info on the first cycle of the day only.
    assert len(client.calls) == 3 * 5 + 3 * 2 + 3 * 2 + 2 + 2
    assert client.closed is True


async def test_loop_logs_auth_errors_and_keeps_going(
    url: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(url)
    client = _FakeClient(error=UnusualWhalesAuthError("returned HTTP 401"))
    with caplog.at_level(logging.ERROR, logger="webapp.board.refresher"):
        slept, _made = await _run_loop(url, client, max_sleeps=2)
    assert slept == [_SETTINGS.refresh.cadence_seconds] * 2
    assert len(client.calls) == 2
    assert caplog.text.count("UW auth error") == 2


async def test_loop_survives_a_failing_cycle(url: str, caplog: pytest.LogCaptureFixture) -> None:
    class _BrokenJournal:
        def list(self, status: str | None = None) -> list[Any]:
            msg = "journal table unavailable"
            raise RuntimeError(msg)

    client = _FakeClient()
    with caplog.at_level(logging.ERROR, logger="webapp.board.refresher"):
        slept, _made = await _run_loop(url, client, max_sleeps=2, journal=_BrokenJournal())
    assert slept == [_SETTINGS.refresh.cadence_seconds] * 2
    assert caplog.text.count("board refresher cycle failed") == 2
