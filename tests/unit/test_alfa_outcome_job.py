"""Phase 5.2.C2b: the daily outcome job (``webapp/board/outcome_job.py``).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (alfa_outcome) and
§4 (C2).

Hermetic: a tmp sqlite file holds the cards, the closes and the outcomes; a fake
client serves ``/historic`` payloads shaped like the live ones (rows under
``chains``, newest first, prices as strings, ``nbbo_bid`` null on a day the
contract did not trade). No network, no env.

The calendar is two real trading weeks, stored as SPY closes:
2026-09-14..18 and 2026-09-21..25. A card pressed on Wednesday 2026-09-16 has
its 1-day horizon on the 17th and its 5-day horizon on the 23rd.

Pins:
  - the primary on hand-computed closes, for both directions, and identical for
    a log and a pas card;
  - the primary costs ZERO Unusual Whales calls; the secondary costs one request
    per DISTINCT contract per run, whatever the number of cards and horizons;
  - the horizon gate: nothing at all is written before the horizon has passed;
  - a missing close leaves the row bekliyor and never becomes a zero, and a gap
    inside a covered window is a final "veri yok";
  - bekliyor advances to a final status exactly once, and a restart re-running
    the job writes nothing and changes nothing;
  - a zero-volume horizon day yields no option bid (never an invented price), a
    not-found contract still stores the primary, and a DEGRADED fetch leaves the
    row bekliyor rather than freezing "veri yok";
  - the daily limit propagates to the clock;
  - the registry entry is ONE, named ``outcomes``, at ``outcomes.job_time_et``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.orm import Session
from webapp.board import outcome_job as job_module
from webapp.board.cards import AlfaDecisionCard, CardRepo, DecisionCard
from webapp.board.daily_close import (
    AlfaDailyClose,
    ClosePoint,
    build_close_index,
    ensure_daily_close_tables,
    load_closes,
    pct_move_between,
)
from webapp.board.db import make_engine
from webapp.board.outcome_job import (
    OPTION_HISTORIC_PATH,
    OUTCOME_JOB_NAME,
    BidFetch,
    OutcomeJob,
    fetch_historic_bids,
    outcome_job_time,
    outcome_jobs,
    parse_historic_bids,
    resolve_outcome,
    run_outcome_job,
    run_outcomes,
    session_after,
    session_on_or_before,
)
from webapp.board.outcomes import COMPUTED, NO_DATA, PENDING, OutcomeRepo
from webapp.board.settings import load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from sqlalchemy.engine import Engine
    from webapp.board.settings import BoardSettings

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_HORIZONS = _SETTINGS.outcomes.horizons_trading_days

_NOW = datetime(2026, 9, 25, 21, 35, tzinfo=UTC)  # a post-close run, 17:35 ET
# Wednesday 2026-09-16, 10:05 ET: the press is inside the session, so the card's
# session is that day's close.
_CREATED = datetime(2026, 9, 16, 14, 5, tzinfo=UTC)
_CARD_DAY = date(2026, 9, 16)
_H1_DAY = date(2026, 9, 17)
_H5_DAY = date(2026, 9, 23)
_SYMBOL = "AAA260918C00100000"

_SESSIONS = (
    date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17),
    date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
    date(2026, 9, 24), date(2026, 9, 25),
)
# SPY: +0.5% over one session, +1.0% over five.
_SPY_CLOSES = {day: 400.0 for day in _SESSIONS}
_SPY_CLOSES[_H1_DAY] = 402.0
_SPY_CLOSES[_H5_DAY] = 404.0
# AAA: +2.0% over one session, +5.0% over five.
_AAA_CLOSES = {day: 100.0 for day in _SESSIONS}
_AAA_CLOSES[_H1_DAY] = 102.0
_AAA_CLOSES[_H5_DAY] = 105.0
# So the market-neutral excess of a ``yukarı`` card is +1.5% at 1 day, +4.0% at 5.
_H1_EXCESS = 1.5
_H5_EXCESS = 4.0


class _Client:
    """A fake ``JsonClient``: it records every path and serves fixed payloads."""

    def __init__(
        self, payloads: Mapping[str, dict[str, Any]] | None = None, error: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._payloads = dict(payloads or {})
        self._error = error

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        self.calls.append(path)
        if self._error is not None:
            raise self._error
        return self._payloads.get(path, {"chains": []})


class _NoClient:
    """A client that fails the test if the job calls Unusual Whales at all."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        msg = f"the primary must cost no Unusual Whales call, got {path}"
        raise AssertionError(msg)


@dataclass(frozen=True)
class _Context:
    """A stand-in for ``daily_jobs.JobContext`` — the same four attributes."""

    client: Any
    engine: Engine
    settings: BoardSettings
    now: datetime


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'outcome-job.db'}")
    ensure_daily_close_tables(eng)
    yield eng
    eng.dispose()


def _store_closes(engine: Engine, ticker: str, closes: Mapping[date, float]) -> None:
    with Session(engine) as session:
        session.add_all(
            AlfaDailyClose(ticker=ticker, day=day, close=close, fetched_at=_NOW)
            for day, close in closes.items()
        )
        session.commit()


def _calendar(engine: Engine, *, underlying: Mapping[date, float] | None = None) -> None:
    _store_closes(engine, "SPY", _SPY_CLOSES)
    _store_closes(engine, "AAA", _AAA_CLOSES if underlying is None else underlying)


def _card(
    engine: Engine,
    *,
    decision: str = "pas",
    ticker: str = "AAA",
    direction: str = "yukarı",
    symbol: str | None = None,
    created: datetime = _CREATED,
) -> str:
    repo = CardRepo(engine, clock=lambda: created)
    return repo.write_card(
        decision=decision,  # type: ignore[arg-type]
        ticker=ticker,
        direction=direction,
        run_id="live-2026-09-16",
        dominant_option_symbol=symbol,
        card={},
        board_profile_hash="hash",
        calibration_profile_hash=None,
    )


def _settings(**outcomes: object) -> BoardSettings:
    return _SETTINGS.model_copy(
        update={"outcomes": _SETTINGS.outcomes.model_copy(update=outcomes)},
    )


def _historic(*rows: tuple[str, str | None]) -> dict[str, Any]:
    """A ``/historic`` payload: (date, nbbo_bid) rows, prices as strings like the live one."""
    return {
        "chains": [
            {"date": day, "nbbo_bid": bid, "nbbo_ask": "9.99", "volume": 0 if bid is None else 10}
            for day, bid in rows
        ],
    }


async def _run(
    engine: Engine, client: Any, *, settings: BoardSettings | None = None,
) -> job_module.OutcomeJobReport:
    return await run_outcome_job(
        client, engine, settings=settings or _SETTINGS, now=_NOW,
    )


# ---------------------------------------------------------------------------
# The session calendar
# ---------------------------------------------------------------------------


def test_the_session_helpers_walk_the_stored_calendar() -> None:
    assert session_on_or_before(_SESSIONS, _CARD_DAY) == _CARD_DAY
    # A Saturday press belongs to Friday's close.
    assert session_on_or_before(_SESSIONS, date(2026, 9, 19)) == date(2026, 9, 18)
    assert session_on_or_before(_SESSIONS, date(2026, 9, 1)) is None
    assert session_after(_SESSIONS, _CARD_DAY, 1) == _H1_DAY
    assert session_after(_SESSIONS, _CARD_DAY, 5) == _H5_DAY
    # A weekend is not a session: five sessions after Wednesday is the NEXT week.
    assert (_H5_DAY - _CARD_DAY).days == 7
    assert session_after(_SESSIONS, date(2026, 9, 25), 1) is None  # the horizon has not passed
    with pytest.raises(ValueError, match="horizon_days"):
        session_after(_SESSIONS, _CARD_DAY, 0)


def test_the_window_uses_the_daily_close_move_helper(engine: Engine) -> None:
    """The job's indexed lookup is the module-level ``pct_move_between``, pinned."""
    _calendar(engine)
    points = load_closes(engine, "AAA")
    index = build_close_index(points)
    indexed = index.pct_move_between(_CARD_DAY, _H1_DAY)
    direct = pct_move_between(points, _CARD_DAY, _H1_DAY)
    assert indexed == direct
    assert direct is not None
    assert direct.pct == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# The primary (contract §4)
# ---------------------------------------------------------------------------


async def test_the_primary_is_hand_computable_and_costs_no_uw_call(engine: Engine) -> None:
    _calendar(engine)
    card_id = _card(engine)
    report = await _run(engine, _NoClient())
    assert (report.cards, report.computed, report.requests) == (1, len(_HORIZONS), 0)
    repo = OutcomeRepo(engine)
    one = repo.get_outcome(card_id, 1)
    five = repo.get_outcome(card_id, 5)
    assert one is not None and five is not None
    assert one.status == COMPUTED
    assert one.market_neutral_excess == pytest.approx(_H1_EXCESS)
    assert five.market_neutral_excess == pytest.approx(_H5_EXCESS)
    # The four closes it used are stored with it.
    assert (one.underlying_close_at_card_day, one.underlying_close_at_horizon) == (100.0, 102.0)
    assert (one.spy_close_at_card_day, one.spy_close_at_horizon) == (400.0, 402.0)
    assert (five.underlying_close_at_horizon, five.spy_close_at_horizon) == (105.0, 404.0)
    assert one.option_bid_at_horizon is None  # the card names no contract


async def test_an_asagi_card_gets_the_opposite_sign(engine: Engine) -> None:
    _calendar(engine)
    card_id = _card(engine, direction="aşağı")
    await _run(engine, _NoClient())
    stored = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert stored is not None
    assert stored.market_neutral_excess == pytest.approx(-_H1_EXCESS)


async def test_log_and_pas_are_measured_by_identical_math(engine: Engine) -> None:
    """Contract §4: the two stay comparable only because nothing differs here."""
    _calendar(engine)
    logged = _card(engine, decision="log")
    passed = _card(engine, decision="pas")
    await _run(engine, _NoClient())
    repo = OutcomeRepo(engine)
    log_row = repo.get_outcome(logged, 1)
    pas_row = repo.get_outcome(passed, 1)
    assert log_row is not None and pas_row is not None
    assert log_row.market_neutral_excess == pas_row.market_neutral_excess
    assert log_row.status == pas_row.status == COMPUTED


async def test_the_horizon_gate_writes_nothing_before_the_horizon_passes(engine: Engine) -> None:
    _calendar(engine)
    # Pressed on the last stored session: one session has passed, five have not.
    card_id = _card(engine, created=datetime(2026, 9, 24, 14, 5, tzinfo=UTC))
    report = await _run(engine, _NoClient())
    repo = OutcomeRepo(engine)
    assert repo.get_outcome(card_id, 1) is not None
    assert repo.get_outcome(card_id, 5) is None  # not due: no row at all, not bekliyor
    assert report.computed == 1


# ---------------------------------------------------------------------------
# Missing data (contract §4: never a zero)
# ---------------------------------------------------------------------------


async def test_a_ticker_with_no_closes_stays_bekliyor(engine: Engine) -> None:
    _store_closes(engine, "SPY", _SPY_CLOSES)  # the calendar exists, the name does not
    card_id = _card(engine, ticker="BBB")
    report = await _run(engine, _NoClient())
    assert report.pending == len(_HORIZONS)
    stored = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert stored is not None
    assert stored.status == PENDING
    assert stored.market_neutral_excess is None  # never a zero
    assert stored.underlying_close_at_card_day is None


async def test_a_gap_inside_a_covered_window_is_a_final_veri_yok(engine: Engine) -> None:
    """The source has the window but not those sessions: more fetching will not help."""
    _store_closes(engine, "SPY", _SPY_CLOSES)
    _store_closes(engine, "AAA", {date(2026, 9, 14): 100.0, date(2026, 9, 25): 110.0})
    card_id = _card(engine)
    report = await _run(engine, _NoClient())
    stored = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert stored is not None
    assert stored.status == NO_DATA
    assert stored.is_final is True
    assert stored.market_neutral_excess is None
    assert report.no_data == len(_HORIZONS)


async def test_a_card_whose_direction_cannot_be_read_is_veri_yok(engine: Engine) -> None:
    _calendar(engine)
    CardRepo(engine)  # the table exists
    with Session(engine) as session:
        session.add(AlfaDecisionCard(
            id="bad-direction", created_at=_CREATED, decision="pas", ticker="AAA",
            direction="bullish", run_id="r", dominant_option_symbol=None, card_json="{}",
            board_version="unknown", board_profile_hash="h", calibration_profile_hash=None,
            trade_id=None, note=None,
        ))
        session.commit()
    await _run(engine, _NoClient())
    stored = OutcomeRepo(engine).get_outcome("bad-direction", 1)
    assert stored is not None
    assert stored.status == NO_DATA


async def test_a_pending_row_advances_exactly_once_when_the_close_arrives(
    engine: Engine,
) -> None:
    _store_closes(engine, "SPY", _SPY_CLOSES)
    card_id = _card(engine)
    first = await _run(engine, _NoClient())
    assert first.pending == len(_HORIZONS)
    _store_closes(engine, "AAA", _AAA_CLOSES)
    second = await _run(engine, _NoClient())
    assert second.advanced == len(_HORIZONS)
    moved = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert moved is not None
    assert moved.status == COMPUTED
    assert moved.market_neutral_excess == pytest.approx(_H1_EXCESS)
    # A third run changes nothing at all.
    third = await _run(engine, _NoClient())
    assert (third.advanced, third.computed, third.pending) == (0, 0, 0)
    assert OutcomeRepo(engine).get_outcome(card_id, 1) == moved


async def test_a_restart_re_running_the_job_writes_nothing(engine: Engine) -> None:
    _calendar(engine)
    card_id = _card(engine)
    await _run(engine, _NoClient())
    before = OutcomeRepo(engine).list_outcomes(card_ids=[card_id])
    again = await _run(engine, _NoClient())
    assert (again.computed, again.pending, again.advanced, again.requests) == (0, 0, 0, 0)
    assert OutcomeRepo(engine).list_outcomes(card_ids=[card_id]) == before


async def test_the_scan_is_bounded_by_the_profile(engine: Engine) -> None:
    _calendar(engine)
    older = _card(engine, created=_CREATED)
    newer = _card(engine, created=_CREATED.replace(hour=15))
    report = await _run(engine, _NoClient(), settings=_settings(max_cards_per_run=1))
    assert report.cards == 1
    repo = OutcomeRepo(engine)
    assert repo.get_outcome(newer, 1) is not None  # newest first
    assert repo.get_outcome(older, 1) is None


# ---------------------------------------------------------------------------
# The secondary: the dominant contract's bid (contract §4)
# ---------------------------------------------------------------------------


def test_parse_historic_bids_reads_the_live_shape() -> None:
    payload = _historic(("2026-09-17", "6.90"), ("2026-09-16", None), ("2026-09-15", "5.45"))
    assert parse_historic_bids(payload) == {
        date(2026, 9, 17): 6.90, date(2026, 9, 15): 5.45,
    }
    # Rows under "data", the other spelling the endpoint uses.
    assert parse_historic_bids({"data": [{"date": "2026-09-17", "nbbo_bid": "1.00"}]}) == {
        date(2026, 9, 17): 1.0,
    }
    # A zero bid is a real price; a negative one is not.
    assert parse_historic_bids(_historic(("2026-09-17", "0"))) == {date(2026, 9, 17): 0.0}
    assert parse_historic_bids(_historic(("2026-09-17", "-1"))) == {}
    # Junk is skipped, never guessed.
    assert parse_historic_bids({"chains": "nope"}) == {}
    assert parse_historic_bids({}) == {}
    assert parse_historic_bids({"chains": [1, {"date": "x", "nbbo_bid": "1"}, {"nbbo_bid": "1"}]}) == {}
    # Newest first: the first row of a repeated day wins.
    assert parse_historic_bids(
        _historic(("2026-09-17", "2.00"), ("2026-09-17", "1.00")),
    ) == {date(2026, 9, 17): 2.0}


def test_a_zero_volume_row_records_no_bid_even_when_it_carries_a_quote() -> None:
    """The live counter-example to the probe note in contract §4.

    Run against the real endpoint on 2026-09-16, ``SPXW260925C07730000`` served
    ``{"date": "2026-08-18", "volume": 0, "nbbo_bid": "120.60"}`` — so a
    zero-volume day CAN carry an NBBO, and §4's note ("NBBO is null on
    zero-volume days") does not hold. §4's RULE does: a day the contract did not
    trade is ``veri yok``. The parser enforces it rather than trusting the note.
    """
    payload = {"chains": [{"date": "2026-08-18", "nbbo_bid": "120.60", "volume": 0}]}
    assert parse_historic_bids(payload) == {}
    # A string zero counts the same way; junk in the field does not hide a bid.
    assert parse_historic_bids({"chains": [{"date": "2026-08-18", "nbbo_bid": "1", "volume": "0"}]}) == {}
    assert parse_historic_bids(
        {"chains": [{"date": "2026-08-18", "nbbo_bid": "1", "volume": "n/a"}]},
    ) == {date(2026, 8, 18): 1.0}


def test_a_row_without_a_volume_keeps_its_bid() -> None:
    """The absence of a number is not evidence that the contract did not trade."""
    assert parse_historic_bids({"chains": [{"date": "2026-09-17", "nbbo_bid": "1.00"}]}) == {
        date(2026, 9, 17): 1.0,
    }


async def test_the_horizon_days_bid_is_stored_with_the_primary(engine: Engine) -> None:
    _calendar(engine)
    card_id = _card(engine, symbol=_SYMBOL)
    client = _Client({
        OPTION_HISTORIC_PATH.format(symbol=_SYMBOL): _historic(
            ("2026-09-23", "3.10"), ("2026-09-17", "1.25"), ("2026-09-16", "1.00"),
        ),
    })
    report = await _run(engine, client)
    repo = OutcomeRepo(engine)
    one = repo.get_outcome(card_id, 1)
    five = repo.get_outcome(card_id, 5)
    assert one is not None and five is not None
    assert one.option_bid_at_horizon == 1.25  # the 17th, not the card day
    assert five.option_bid_at_horizon == 3.10
    assert one.option_symbol == _SYMBOL
    # ONE request served both horizons.
    assert report.requests == 1
    assert client.calls == [OPTION_HISTORIC_PATH.format(symbol=_SYMBOL)]


async def test_a_zero_volume_horizon_day_has_no_bid(engine: Engine) -> None:
    """NBBO is null on a day the contract did not trade; no option price is invented."""
    _calendar(engine)
    card_id = _card(engine, symbol=_SYMBOL)
    client = _Client({
        OPTION_HISTORIC_PATH.format(symbol=_SYMBOL): _historic(("2026-09-17", None)),
    })
    await _run(engine, client)
    stored = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert stored is not None
    assert stored.status == COMPUTED  # the primary is unaffected
    assert stored.option_bid_at_horizon is None
    assert stored.option_bid_text == "veri yok"


async def test_one_request_per_distinct_contract_whatever_the_card_count(
    engine: Engine,
) -> None:
    _calendar(engine)
    other = "AAA260918P00100000"
    for symbol in (_SYMBOL, _SYMBOL, other):
        _card(engine, symbol=symbol)
    _card(engine, symbol=None)  # a card with no contract costs no request
    client = _Client()
    report = await _run(engine, client)
    assert report.cards == 4
    assert sorted(client.calls) == sorted(
        OPTION_HISTORIC_PATH.format(symbol=s) for s in (_SYMBOL, other)
    )
    assert report.requests == 2  # 4 cards x 2 horizons, 2 distinct contracts


async def test_a_not_found_contract_still_stores_the_primary(engine: Engine) -> None:
    _calendar(engine)
    card_id = _card(engine, symbol=_SYMBOL)
    client = _Client(error=UnusualWhalesNotFoundError("HTTP 404", status_code=404))
    report = await _run(engine, client)
    stored = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert stored is not None
    assert stored.status == COMPUTED
    assert stored.option_bid_at_horizon is None
    assert (report.requests, report.degraded) == (1, 0)  # not-found is data, not a failure


@pytest.mark.parametrize(
    "error",
    [
        UnusualWhalesRateLimitError("HTTP 429"),
        UnusualWhalesTransientError("HTTP 503"),
        CircuitBreakerOpenError("breaker open"),
    ],
)
async def test_a_degraded_secondary_leaves_the_row_bekliyor(
    engine: Engine, error: Exception,
) -> None:
    """Our failure must never freeze "veri yok" into an append-only table."""
    _calendar(engine)
    card_id = _card(engine, symbol=_SYMBOL)
    report = await _run(engine, _Client(error=error))
    assert report.degraded == 1
    assert report.computed == 0
    stored = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert stored is not None
    assert stored.status == PENDING
    # The next run, with the endpoint back, finishes the job.
    client = _Client({
        OPTION_HISTORIC_PATH.format(symbol=_SYMBOL): _historic(("2026-09-17", "1.25")),
    })
    second = await _run(engine, client)
    assert second.advanced == len(_HORIZONS)
    moved = OutcomeRepo(engine).get_outcome(card_id, 1)
    assert moved is not None
    assert (moved.status, moved.option_bid_at_horizon) == (COMPUTED, 1.25)


async def test_the_daily_limit_propagates_to_the_clock(engine: Engine) -> None:
    _calendar(engine)
    _card(engine, symbol=_SYMBOL)
    with pytest.raises(UnusualWhalesDailyLimitError):
        await _run(engine, _Client(error=UnusualWhalesDailyLimitError("HTTP 429 daily")))


async def test_fetch_historic_bids_classifies_its_failures() -> None:
    assert await fetch_historic_bids(_Client(), _SYMBOL) == BidFetch(bids={})
    not_found = await fetch_historic_bids(
        _Client(error=UnusualWhalesNotFoundError("HTTP 404", status_code=404)), _SYMBOL,
    )
    assert not_found == BidFetch(bids={}, degraded=False)
    degraded = await fetch_historic_bids(
        _Client(error=UnusualWhalesTransientError("HTTP 503")), _SYMBOL,
    )
    assert degraded.degraded is True


# ---------------------------------------------------------------------------
# The registry entry (contract §4: the daily post-close job)
# ---------------------------------------------------------------------------


def test_faz_c_appends_exactly_one_entry_at_the_profile_time() -> None:
    jobs = outcome_jobs(_SETTINGS)
    assert len(jobs) == 1
    (entry,) = jobs
    assert entry.name == OUTCOME_JOB_NAME == "outcomes"
    assert entry.et_time == outcome_job_time(_SETTINGS) == time.fromisoformat(
        _SETTINGS.outcomes.job_time_et,
    )
    assert entry.et_time == time(17, 30)
    assert entry.trading_day_only is True
    # The shape the daily-job registry takes, field for field.
    assert set(OutcomeJob.__dataclass_fields__) == {"name", "et_time", "run", "trading_day_only"}
    assert entry.run is run_outcomes


def test_the_entry_time_follows_the_profile_rather_than_a_literal() -> None:
    moved = _settings(job_time_et="16:45")
    assert outcome_jobs(moved)[0].et_time == time(16, 45)


async def test_the_job_body_returns_the_marker_detail(engine: Engine) -> None:
    _calendar(engine)
    _card(engine)
    detail = await run_outcomes(
        _Context(client=_NoClient(), engine=engine, settings=_SETTINGS, now=_NOW),
    )
    assert detail == "cards=1 computed=2 no_data=0 pending=0 advanced=0 requests=0 degraded=0"


async def test_an_empty_ledger_is_a_no_op(engine: Engine) -> None:
    _calendar(engine)
    CardRepo(engine)
    report = await _run(engine, _NoClient())
    assert (report.cards, report.written, report.requests) == (0, 0, 0)


async def test_a_naive_clock_is_refused(engine: Engine) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await run_outcome_job(
            _NoClient(), engine, settings=_SETTINGS, now=datetime(2026, 9, 25, 21, 35),
        )


def test_resolve_is_pure_and_says_which_day_it_measured() -> None:
    card = DecisionCard(
        id="c", created_at=_CREATED, decision="pas", ticker="AAA", direction="yukarı",
        run_id="r", dominant_option_symbol=None, card_json="{}", board_version="unknown",
        board_profile_hash="h", calibration_profile_hash=None, trade_id=None, note=None,
    )
    underlying = build_close_index(
        ClosePoint(day=day, close=close) for day, close in _AAA_CLOSES.items()
    )
    spy = build_close_index(
        ClosePoint(day=day, close=close) for day, close in _SPY_CLOSES.items()
    )
    resolution = resolve_outcome(
        card, 1, sessions=_SESSIONS, underlying=underlying, spy=spy,
    )
    assert resolution.state == "computed"
    assert resolution.horizon_day == _H1_DAY
    assert resolution.measurement is not None
    assert resolution.measurement.excess_pct == pytest.approx(_H1_EXCESS)
    assert resolution.final is not None
    assert resolution.final.status == COMPUTED
