"""Phase 5.2.A2: quote and exit-depth fetchers and tables (``webapp/board/quotes.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.2, §4.4 and §5 A2;
decisions P14 and P16.

Fixture rows are trimmed from live Unusual Whales responses captured 2026-09-15:
  - ``/api/stock/SPY/option-contracts?option_symbol[]=...``: two traded rows;
  - ``/api/stock/SPY/option-contracts?expiry=2026-09-18&page=1``: an untraded
    row (null NBBO, volume 0, placeholder ``last_tape_time`` 10:30:34Z);
  - ``/api/stock/SMCI/option-contracts?expiry=2026-09-18``: a zero-bid row;
  - ``/api/option-contract/SMCI260918C00037000/flow``: the newest print.

Pins:
  - one snapshot per requested symbol (deduplicated, upper-cased); a symbol UW
    did not return is ``returned=False``; string numbers parse; null NBBO
    stays null; a tape time for a zero-volume row or outside the session is
    not a trade;
  - fetchers send exactly one call with the probed params; NotFound is
    no-data; rate limit, transient, open breaker and unexpected shapes are
    degraded (nothing to write); daily limit and auth errors propagate;
  - OCC symbols for chains and journal legs; recorded chains win;
  - upsert and read round-trip, tz-aware, with the depth quote time as the
    later NBBO time; the render reader creates missing tables once.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.aggregate import build_board_rows
from webapp.board.db import make_engine
from webapp.board.quotes import (
    CONTRACT_FLOW_PATH,
    OPTION_CONTRACTS_PATH,
    SYMBOL_PARAM,
    DepthSnapshot,
    QuoteSnapshot,
    contract_symbol,
    dominant_symbol,
    ensure_quotes_tables,
    fetch_contract_depth,
    fetch_contract_quotes,
    occ_symbol,
    parse_contract_flow,
    parse_option_contract_rows,
    read_board_quotes,
    read_depths,
    read_quotes,
    upsert_depths,
    upsert_quotes,
)
from webapp.board.settings import AggregationSettings
from webapp.board.signals import BoardPrint, PrintMetaView

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

if TYPE_CHECKING:
    from pathlib import Path

_SPY_0DTE_CALL = {
    "option_symbol": "SPY260915C00757000", "nbbo_bid": "1.23", "nbbo_ask": "1.24",
    "last_price": "1.24", "volume": 147025, "open_interest": 1228,
    "last_tape_time": "2026-09-15T15:30:28Z", "prev_oi": 680, "delta": 0.55508312433416,
    "total_premium": "18274026.00",
}
_SPY_PUT = {
    "option_symbol": "SPY260918P00760000", "nbbo_bid": "6.98", "nbbo_ask": "7.01",
    "last_price": "6.98", "volume": 36745, "open_interest": 107149,
    "last_tape_time": "2026-09-15T15:30:26Z", "prev_oi": 109097, "delta": -0.570461584063746,
    "total_premium": "22804943.00",
}
_SPY_UNTRADED = {
    "option_symbol": "SPY260918C00684000", "nbbo_bid": None, "nbbo_ask": None,
    "last_price": None, "volume": 0, "open_interest": 25,
    "last_tape_time": "2026-09-15T10:30:34Z", "prev_oi": 25, "delta": 0.993458314399113,
    "total_premium": "0",
}
_SMCI_ZERO_BID = {
    "option_symbol": "SMCI260918C00050000", "nbbo_bid": "0.00", "nbbo_ask": "0.01",
    "last_price": "0.01", "volume": 2674, "open_interest": 12977,
    "last_tape_time": "2026-09-15T15:10:52Z", "prev_oi": 12967, "delta": 0.00913931682618969,
    "total_premium": "2711.00",
}
_SMCI_FLOW_ROW = {
    "option_chain_id": "SMCI260918C00037000", "executed_at": "2026-09-15T15:26:21.567000Z",
    "price": "0.89", "size": 21, "nbbo_bid": "0.87", "nbbo_ask": "0.91",
    "nbbo_bid_size": 137, "nbbo_ask_size": 108,
    "nbbo_bid_time": "2026-09-15T15:26:21.566000Z", "nbbo_ask_time": "2026-09-15T15:26:21.517000Z",
}
_BOGUS = "SPY991231C99999000"


class _FakeClient:
    """Records (path, params); returns one scripted body or raises."""

    def __init__(self, payload: object = None, error: BaseException | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> Any:
        self.calls.append((path, params))
        if self.error is not None:
            raise self.error
        return self.payload


_DEGRADABLE = [
    pytest.param(UnusualWhalesRateLimitError("rate-limited (HTTP 429): slow down"), id="rate-limit"),
    pytest.param(UnusualWhalesTransientError("returned HTTP 503"), id="transient"),
    pytest.param(CircuitBreakerOpenError("circuit breaker is open"), id="breaker-open"),
]
_PROPAGATING = [
    pytest.param(UnusualWhalesDailyLimitError("(HTTP 429): daily_request_limit_hit"), UnusualWhalesDailyLimitError, id="daily-limit"),
    pytest.param(UnusualWhalesAuthError("returned HTTP 401"), UnusualWhalesAuthError, id="auth"),
]


# ---------------------------------------------------------------------------
# option-contracts parsing
# ---------------------------------------------------------------------------


def test_parse_returned_rows_and_diff_requested_symbols() -> None:
    payload = {"data": [_SPY_0DTE_CALL, _SPY_PUT, _SPY_UNTRADED]}
    requested = [
        "SPY260915C00757000", "SPY260918P00760000", "SPY260918C00684000", _BOGUS,
        "spy260915c00757000",  # duplicate, lower case
    ]
    quotes = parse_option_contract_rows(payload, "SPY", requested)

    assert [q.option_symbol for q in quotes] == [
        "SPY260915C00757000", "SPY260918P00760000", "SPY260918C00684000", _BOGUS,
    ]
    by = {q.option_symbol: q for q in quotes}
    call = by["SPY260915C00757000"]
    assert (call.nbbo_bid, call.nbbo_ask, call.last_price) == (1.23, 1.24, 1.24)
    assert (call.volume, call.open_interest) == (147025, 1228)
    assert call.last_tape_time == datetime(2026, 9, 15, 15, 30, 28, tzinfo=UTC)
    assert call.returned is True and call.ticker == "SPY"

    untraded = by["SPY260918C00684000"]
    assert untraded.returned is True
    assert (untraded.nbbo_bid, untraded.nbbo_ask, untraded.last_price) == (None, None, None)
    assert untraded.volume == 0
    assert untraded.last_tape_time is None  # the 10:30:34Z placeholder is not a trade

    bogus = by[_BOGUS]
    assert bogus.returned is False
    assert (bogus.nbbo_bid, bogus.volume, bogus.last_tape_time) == (None, None, None)


def test_tape_time_outside_the_session_is_not_a_trade() -> None:
    early = dict(_SMCI_ZERO_BID, last_tape_time="2026-09-15T10:30:02Z")
    (q,) = parse_option_contract_rows({"data": [early]}, "SMCI", ["SMCI260918C00050000"])
    assert q.last_tape_time is None
    (q,) = parse_option_contract_rows({"data": [_SMCI_ZERO_BID]}, "SMCI", ["SMCI260918C00050000"])
    assert q.last_tape_time == datetime(2026, 9, 15, 15, 10, 52, tzinfo=UTC)


def test_zero_bid_parses_as_zero_not_null() -> None:
    (q,) = parse_option_contract_rows({"data": [_SMCI_ZERO_BID]}, "SMCI", ["SMCI260918C00050000"])
    assert (q.nbbo_bid, q.nbbo_ask) == (0.0, 0.01)


def test_numbers_are_parsed_defensively() -> None:
    row = dict(
        _SPY_PUT, nbbo_bid="n/a", nbbo_ask="-1.0", last_price=True,
        volume="12", open_interest="1.5",
    )
    (q,) = parse_option_contract_rows({"data": [row]}, "SPY", ["SPY260918P00760000"])
    assert (q.nbbo_bid, q.nbbo_ask, q.last_price) == (None, None, None)
    assert (q.volume, q.open_interest) == (12, None)


def test_unrequested_and_non_object_rows_are_ignored() -> None:
    payload = {"data": ["junk", 7, {"no_symbol": 1}, _SPY_PUT, _SPY_0DTE_CALL]}
    quotes = parse_option_contract_rows(payload, "SPY", ["SPY260918P00760000"])
    assert [q.option_symbol for q in quotes] == ["SPY260918P00760000"]
    assert quotes[0].returned is True


@pytest.mark.parametrize("payload", [{"rows": []}, [], None, {"data": {"a": 1}}])
def test_unexpected_payload_shape_raises(payload: object) -> None:
    with pytest.raises(ValueError, match="data"):
        parse_option_contract_rows(payload, "SPY", ["SPY260918P00760000"])


# ---------------------------------------------------------------------------
# option-contracts fetcher
# ---------------------------------------------------------------------------


async def test_fetch_quotes_sends_one_call_with_the_symbol_filter() -> None:
    client = _FakeClient({"data": [_SPY_0DTE_CALL, _SPY_PUT]})
    fetch = await fetch_contract_quotes(
        client, "spy", ["SPY260915C00757000", "SPY260918P00760000", "SPY260915C00757000"],
    )
    assert client.calls == [
        (
            OPTION_CONTRACTS_PATH.format(ticker="SPY"),
            {SYMBOL_PARAM: ["SPY260915C00757000", "SPY260918P00760000"]},
        ),
    ]
    assert client.calls[0][0] == "/api/stock/SPY/option-contracts"
    assert SYMBOL_PARAM == "option_symbol[]"
    assert fetch.degraded is False
    assert [q.returned for q in fetch.quotes] == [True, True]


async def test_fetch_quotes_without_symbols_makes_no_call() -> None:
    client = _FakeClient({"data": []})
    fetch = await fetch_contract_quotes(client, "SPY", [])
    assert client.calls == []
    assert fetch.quotes == () and fetch.degraded is False


async def test_fetch_quotes_not_found_means_no_data_for_every_symbol() -> None:
    client = _FakeClient(error=UnusualWhalesNotFoundError("HTTP 422", status_code=422))
    fetch = await fetch_contract_quotes(client, "ZZZZQ", ["ZZZZQ260918C00010000"])
    assert fetch.degraded is False
    assert [(q.option_symbol, q.returned) for q in fetch.quotes] == [("ZZZZQ260918C00010000", False)]


@pytest.mark.parametrize("error", _DEGRADABLE)
async def test_fetch_quotes_degrades_on_service_errors(error: Exception) -> None:
    fetch = await fetch_contract_quotes(_FakeClient(error=error), "SPY", ["SPY260918P00760000"])
    assert fetch.degraded is True
    assert fetch.quotes == ()


@pytest.mark.parametrize(("error", "raised"), _PROPAGATING)
async def test_fetch_quotes_propagates_daily_limit_and_auth(error: Exception, raised: type[Exception]) -> None:
    with pytest.raises(raised):
        await fetch_contract_quotes(_FakeClient(error=error), "SPY", ["SPY260918P00760000"])


async def test_fetch_quotes_unexpected_shape_is_degraded_not_yok() -> None:
    fetch = await fetch_contract_quotes(_FakeClient({"unexpected": True}), "SPY", ["SPY260918P00760000"])
    assert fetch.degraded is True
    assert fetch.quotes == ()


# ---------------------------------------------------------------------------
# contract flow (exit depth)
# ---------------------------------------------------------------------------


def test_parse_contract_flow_reads_sizes_and_times_of_the_newest_print() -> None:
    depth = parse_contract_flow({"data": [_SMCI_FLOW_ROW], "date": "2026-09-15"}, "smci260918c00037000")
    assert depth == DepthSnapshot(
        option_symbol="SMCI260918C00037000",
        nbbo_bid_size=137,
        nbbo_ask_size=108,
        nbbo_bid_time=datetime(2026, 9, 15, 15, 26, 21, 566000, tzinfo=UTC),
        nbbo_ask_time=datetime(2026, 9, 15, 15, 26, 21, 517000, tzinfo=UTC),
    )
    assert parse_contract_flow({"data": []}, "SMCI260918C00037000") is None


async def test_fetch_depth_asks_for_one_print() -> None:
    client = _FakeClient({"data": [_SMCI_FLOW_ROW]})
    fetch = await fetch_contract_depth(client, "SMCI260918C00037000")
    assert client.calls == [(CONTRACT_FLOW_PATH.format(symbol="SMCI260918C00037000"), {"limit": 1})]
    assert client.calls[0][0] == "/api/option-contract/SMCI260918C00037000/flow"
    assert fetch.degraded is False
    assert fetch.depth is not None and fetch.depth.nbbo_bid_size == 137


async def test_fetch_depth_not_found_or_empty_is_no_data() -> None:
    not_found = await fetch_contract_depth(
        _FakeClient(error=UnusualWhalesNotFoundError("HTTP 404", status_code=404)), "SPY991231C99999000",
    )
    assert (not_found.depth, not_found.degraded) == (None, False)
    empty = await fetch_contract_depth(_FakeClient({"data": []}), "SPY260918P00760000")
    assert (empty.depth, empty.degraded) == (None, False)


@pytest.mark.parametrize("error", _DEGRADABLE)
async def test_fetch_depth_degrades_on_service_errors(error: Exception) -> None:
    fetch = await fetch_contract_depth(_FakeClient(error=error), "SPY260918P00760000")
    assert (fetch.depth, fetch.degraded) == (None, True)


@pytest.mark.parametrize(("error", "raised"), _PROPAGATING)
async def test_fetch_depth_propagates_daily_limit_and_auth(error: Exception, raised: type[Exception]) -> None:
    with pytest.raises(raised):
        await fetch_contract_depth(_FakeClient(error=error), "SPY260918P00760000")


async def test_fetch_depth_unexpected_shape_is_degraded() -> None:
    fetch = await fetch_contract_depth(_FakeClient("not json object"), "SPY260918P00760000")
    assert (fetch.depth, fetch.degraded) == (None, True)


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "expiry", "kind", "strike", "expected"),
    [
        ("SPY", date(2026, 9, 15), "call", "757", "SPY260915C00757000"),
        ("SMCI", date(2026, 9, 18), "call", "36.5", "SMCI260918C00036500"),
        ("spy", date(2026, 9, 18), "put", "760.0", "SPY260918P00760000"),
        ("BRK.B", date(2026, 10, 16), "call", "500", "BRKB261016C00500000"),
        ("SPY", date(2026, 9, 18), "put", "1.2345", None),  # finer than the 0.001 OCC grid
        ("SPY", date(2026, 9, 18), "put", "0", None),
        ("SPY", date(2026, 9, 18), "put", "100000", None),  # does not fit 8 digits
        ("SPY", date(2026, 9, 18), "shares", "100", None),
        ("", date(2026, 9, 18), "call", "100", None),
    ],
)
def test_occ_symbol(ticker: str, expiry: date, kind: str, strike: str, expected: str | None) -> None:
    assert occ_symbol(ticker, expiry, kind, Decimal(strike)) == expected


def _row(chain: str | None) -> Any:
    pr = build_print(
        event_id="q1", ts=datetime(2026, 9, 15, 14, 0, tzinfo=UTC), ticker="SPY",
        option_type="put", strike="760", dte=3,
    )
    sig = BacktestStore().add(
        EnrichedEvent(print=pr),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )
    meta = PrintMetaView(fill_side="at_ask", option_chain=chain)
    settings = AggregationSettings(
        intentional_min_top_strike_share_pct=60.0,
        scattered_max_top_strike_share_pct=30.0,
        min_strikes_for_scattered=4,
    )
    (row,) = build_board_rows([BoardPrint(run_id="r", event_id="q1", signal=sig, meta=meta)], settings)
    return row


def test_recorded_chain_wins_over_the_constructed_symbol() -> None:
    assert dominant_symbol(_row("SPY260918P00760000")) == "SPY260918P00760000"
    assert dominant_symbol(_row(None)) == "SPY260918P00760000"  # built from the contract
    row = _row(None)
    assert contract_symbol("SPY", row.dominant) == "SPY260918P00760000"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_upsert_and_read_round_trip(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'quotes.db'}")
    try:
        ensure_quotes_tables(engine)
        first = datetime(2026, 9, 15, 15, 25, tzinfo=UTC)
        second = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
        quotes = parse_option_contract_rows(
            {"data": [_SPY_0DTE_CALL, _SPY_UNTRADED]}, "SPY",
            ["SPY260915C00757000", "SPY260918C00684000", _BOGUS],
        )
        assert upsert_quotes(engine, quotes, fetched_at=first) == 3
        newer = QuoteSnapshot(
            option_symbol="SPY260915C00757000", ticker="SPY", nbbo_bid=1.25, nbbo_ask=1.27,
            last_price=1.26, volume=150000, open_interest=1228,
            last_tape_time=datetime(2026, 9, 15, 15, 29, 59, tzinfo=UTC), returned=True,
        )
        upsert_quotes(engine, [newer], fetched_at=second)

        read = read_quotes(engine, ["SPY260915C00757000", "SPY260918C00684000", _BOGUS, "NOPE"])
        assert set(read) == {"SPY260915C00757000", "SPY260918C00684000", _BOGUS}
        call = read["SPY260915C00757000"]
        assert (call.nbbo_bid, call.nbbo_ask, call.volume) == (1.25, 1.27, 150000)
        assert call.fetched_at == second
        assert call.last_tape_time == datetime(2026, 9, 15, 15, 29, 59, tzinfo=UTC)
        assert read[_BOGUS].returned is False
        assert read["SPY260918C00684000"].nbbo_bid is None
        assert read_quotes(engine, []) == {}

        depth = parse_contract_flow({"data": [_SMCI_FLOW_ROW]}, "SMCI260918C00037000")
        assert depth is not None
        assert upsert_depths(engine, [depth], fetched_at=second) == 1
        (view,) = read_depths(engine, ["SMCI260918C00037000"]).values()
        assert (view.nbbo_bid_size, view.nbbo_ask_size) == (137, 108)
        assert view.quote_time == datetime(2026, 9, 15, 15, 26, 21, 566000, tzinfo=UTC)
        assert view.fetched_at == second
    finally:
        engine.dispose()


def test_render_reader_creates_missing_tables_once(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        quotes, depths = read_board_quotes(engine, ["SPY260918P00760000"])
        assert (quotes, depths) == ({}, {})
        quotes, depths = read_board_quotes(engine, ["SPY260918P00760000"])
        assert (quotes, depths) == ({}, {})
    finally:
        engine.dispose()
