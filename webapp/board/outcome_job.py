"""The daily outcome job (Phase 5.2.C2b).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §4 (C2, "the daily
post-close job computes outcomes for each horizon in
``outcomes.horizons_trading_days``, once the horizon has passed").

One entry in the daily-job registry, at ``outcomes.job_time_et``. For every
decision card and every horizon:

- **Primary (market-neutral excess).** Read out of ``alfa_daily_close`` — the
  table the post-close close job already fills — with
  ``daily_close.pct_move_between``, and turned into the contract's number by
  ``outcomes.measure``. It costs NO Unusual Whales call: both legs are stored
  closes. ``log`` and ``pas`` cards run the identical code path, which is the
  only reason the two can be compared (§4).
- **Secondary (the dominant contract's bid on the horizon day).** One
  ``/api/option-contract/{occ}/historic`` call per DISTINCT contract per run —
  a contract's rows serve every horizon of every card that names it. The bid is
  whatever that day's row carries, and ``veri yok`` when the contract did not
  trade that day: the probe found NBBO is null on zero-volume days, so no option
  price is ever invented.

The session calendar is SPY's own stored sessions, not a holiday table: the
h-th session after the card's session is the h-th day SPY actually traded, so a
market holiday shifts the horizon correctly without a calendar to maintain.
The benchmark is fetched on every close-job run (``daily_close.job_tickers``
appends it), so it is the one series that is always there.

What the job refuses to do:

- **Never a zero.** A missing close leaves the row ``bekliyor`` (§4); it is
  written so the ledger shows the card is being tracked, and a later run
  advances it when the close arrives.
- **Never a final row because OUR fetch failed.** A degraded secondary fetch
  leaves the row ``bekliyor`` rather than freezing ``veri yok`` into an
  append-only table. A not-found contract IS data (program rule 9): the primary
  is written and the option bid stays absent.
- **Never a second write.** A (card, horizon) that already holds a final row is
  skipped, so a restart, a catch-up tick, or two runs on the same day cost
  nothing and change nothing.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from zoneinfo import ZoneInfo

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.cards import DIRECTION_VALUES, CardRepo
from webapp.board.daily_close import (
    BENCHMARK_TICKER,
    CloseIndex,
    build_close_index,
    load_closes_by_ticker,
)
from webapp.board.outcomes import (
    COMPUTED,
    NO_DATA,
    FinalOutcome,
    OutcomeMeasurement,
    OutcomeRepo,
    measure,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.cards import DecisionCard
    from webapp.board.outcomes import Outcome
    from webapp.board.settings import BoardSettings
    from webapp.board.uw_errors import JsonClient

_logger = logging.getLogger(__name__)

# The registry entry's name. ``alfa_job_run`` keys its marker by this, so it is
# part of the job's identity across restarts and must not change.
OUTCOME_JOB_NAME: Final = "outcomes"
OPTION_HISTORIC_PATH: Final = "/api/option-contract/{symbol}/historic"

_ET: Final = ZoneInfo("America/New_York")

# What one (card, horizon) turned out to be on this run.
Resolution = Literal["not_due", "computed", "no_data", "pending"]


# ---------------------------------------------------------------------------
# The session calendar (SPY's own stored sessions)
# ---------------------------------------------------------------------------


def card_session_day(created_at: datetime) -> date:
    """The ET date a card was pressed on (event time, D9)."""
    return created_at.astimezone(_ET).date()


def session_on_or_before(sessions: Sequence[date], day: date) -> date | None:
    """The newest stored session on or before ``day``; ``None`` when none is stored."""
    position = bisect_right(sessions, day)
    return sessions[position - 1] if position else None


def session_after(sessions: Sequence[date], start: date, horizon_days: int) -> date | None:
    """The ``horizon_days``-th session strictly after ``start``.

    ``None`` when the calendar does not hold that many sessions yet — which is
    exactly "the horizon has not passed" (§4), and the job then writes nothing
    at all for that (card, horizon).
    """
    if horizon_days <= 0:
        msg = f"horizon_days must be positive, got {horizon_days!r}"
        raise ValueError(msg)
    index = bisect_right(sessions, start) + horizon_days - 1
    return sessions[index] if index < len(sessions) else None


# ---------------------------------------------------------------------------
# Resolving one (card, horizon)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeResolution:
    """What one (card, horizon) resolves to against the stored closes."""

    state: Resolution
    measurement: OutcomeMeasurement | None = None
    horizon_day: date | None = None

    @property
    def final(self) -> FinalOutcome | None:
        """The finished outcome to store, or ``None`` while the row must wait."""
        if self.state == "computed":
            return FinalOutcome(status=COMPUTED, measurement=self.measurement)
        if self.state == "no_data":
            return FinalOutcome(status=NO_DATA)
        return None


def _covers(index: CloseIndex, start: date, end: date) -> bool:
    """True when the ticker's stored history spans the whole window.

    The close job stores a ticker's full history in one fetch, so a series that
    reaches both sides of the window and still misses the exact sessions is a
    real gap in the source — a final ``veri yok`` — while a series that does not
    reach them is simply not fetched yet, and the row waits.
    """
    days = index.days
    return bool(days) and days[0] <= start and days[-1] >= end


def resolve_outcome(
    card: DecisionCard,
    horizon_days: int,
    *,
    sessions: Sequence[date],
    underlying: CloseIndex,
    spy: CloseIndex,
) -> OutcomeResolution:
    """Contract §4's primary, for one card at one horizon, from stored closes only.

    Both legs are measured over the SAME two sessions: the move helper resolves
    each to the newest close on or before a date, so a leg that lands on another
    day is refused rather than compared across mismatched windows.
    """
    start = session_on_or_before(sessions, card_session_day(card.created_at))
    if start is None:
        return OutcomeResolution(state="pending")
    end = session_after(sessions, start, horizon_days)
    if end is None:
        return OutcomeResolution(state="not_due")
    if card.direction not in DIRECTION_VALUES:
        # A card that cannot say which way it pointed cannot be measured, and the
        # table is append-only: recording it as "veri yok" is the honest end.
        _logger.warning("outcome job: card %s has an unreadable direction", card.id)
        return OutcomeResolution(state="no_data", horizon_day=end)
    underlying_move = underlying.pct_move_between(start, end)
    spy_move = spy.pct_move_between(start, end)
    exact = [
        move for move in (underlying_move, spy_move)
        if move is not None and move.start.day == start and move.end.day == end
    ]
    if len(exact) != 2 or underlying_move is None or spy_move is None:
        state: Resolution = (
            "no_data" if _covers(underlying, start, end) and _covers(spy, start, end) else "pending"
        )
        return OutcomeResolution(state=state, horizon_day=end)
    found = measure(
        direction=card.direction,
        underlying_at_card_day=underlying_move.start.close,
        underlying_at_horizon=underlying_move.end.close,
        spy_at_card_day=spy_move.start.close,
        spy_at_horizon=spy_move.end.close,
    )
    if found is None:
        # A stored close that is not a price. It must not become a -100% outcome.
        return OutcomeResolution(state="pending", horizon_day=end)
    return OutcomeResolution(state="computed", measurement=found, horizon_day=end)


# ---------------------------------------------------------------------------
# The secondary: the dominant contract's bid on the horizon day
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BidFetch:
    """One contract's historic bids by day, and whether the fetch itself failed."""

    bids: Mapping[date, float]
    degraded: bool = False


def _price(value: object) -> float | None:
    """A payload price as a float. Prices arrive as strings; a negative one is not a price."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    # 0.0 is a real bid (a contract nobody will pay for); below zero is not.
    return parsed if parsed >= 0 else None


def _traded(item: Mapping[str, Any]) -> bool:
    """False only when the row states the contract did not trade that day (volume 0).

    Contract §4 gives the rule — the secondary is ``veri yok`` "when the contract
    did not trade that day" — and justifies it with a probe note, "NBBO is null
    on zero-volume days". The live endpoint CONTRADICTS the note: on 2026-09-16
    ``SPXW260925C07730000`` served ``{"date": "2026-08-18", "volume": 0,
    "nbbo_bid": "120.60"}``. So the rule cannot rest on the payload being null,
    and it is enforced here instead: a day with no trade records no bid, whatever
    quote the row carries. Following the RULE rather than its rationale is the
    conservative reading, and ``alfa_outcome`` is append-only — a bid recorded
    today cannot be taken back.

    A row with no volume field keeps its bid: the absence of a number is not
    evidence that the contract did not trade.
    """
    volume = item.get("volume")
    if volume is None or isinstance(volume, bool):
        return True
    if isinstance(volume, int | float):
        return volume != 0
    if isinstance(volume, str):
        try:
            return float(volume) != 0
        except ValueError:
            return True
    return True


def parse_historic_bids(payload: Mapping[str, Any]) -> dict[date, float]:
    """``{day: nbbo_bid}`` from a ``/historic`` payload; days without a bid are absent.

    Rows arrive under ``chains`` (or ``data``), newest first, with prices as
    strings. A day that carries no usable bid — or that the contract did not
    trade on (:func:`_traded`) — is simply not in the map: never a zero, and
    never a quote standing in for a trade the contract never saw.
    """
    raw = payload.get("chains")
    if not isinstance(raw, list):
        raw = payload.get("data")
    if not isinstance(raw, list):
        return {}
    bids: dict[date, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = item.get("date")
        if not isinstance(day, str):
            continue
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            continue
        if not _traded(item):
            continue
        bid = _price(item.get("nbbo_bid"))
        if bid is not None:
            bids.setdefault(parsed_day, bid)
    return bids


async def fetch_historic_bids(client: JsonClient, symbol: str) -> BidFetch:
    """One ``/historic`` call for one contract. Daily-limit and auth errors propagate.

    A not-found contract is data, not a failure (program rule 9): it has no
    historic rows, so the primary is still stored and the option bid stays
    absent. A rate-limited, transient or breaker-open fetch is OUR failure, and
    the caller then leaves the row ``bekliyor`` instead of freezing ``veri yok``.
    """
    try:
        payload = await client.request_json(OPTION_HISTORIC_PATH.format(symbol=symbol))
    except UnusualWhalesNotFoundError:
        return BidFetch(bids={})
    except UnusualWhalesDailyLimitError:
        # A subclass of the rate-limit error, so it MUST be re-raised before the
        # degraded clause below or the key's daily limit would read as one slow
        # contract and the clock would never back off (the same order daily_close
        # uses).
        raise
    except (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError) as exc:
        _logger.warning("outcome job: historic fetch degraded for %s: %s", symbol, exc)
        return BidFetch(bids={}, degraded=True)
    return BidFetch(bids=parse_historic_bids(payload))


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeJobReport:
    """What one run did. The detail line is what the job marker stores."""

    cards: int = 0
    computed: int = 0
    no_data: int = 0
    pending: int = 0
    advanced: int = 0
    requests: int = 0
    degraded: int = 0

    @property
    def written(self) -> int:
        return self.computed + self.no_data + self.pending

    @property
    def detail(self) -> str:
        return (
            f"cards={self.cards} computed={self.computed} no_data={self.no_data} "
            f"pending={self.pending} advanced={self.advanced} "
            f"requests={self.requests} degraded={self.degraded}"
        )


@dataclass
class _Tally:
    computed: int = 0
    no_data: int = 0
    pending: int = 0
    advanced: int = 0
    requests: int = 0
    degraded: int = 0


async def run_outcome_job(
    client: JsonClient,
    engine: Engine,
    *,
    settings: BoardSettings,
    now: datetime,
) -> OutcomeJobReport:
    """Compute every due outcome once. Daily-limit and key failures propagate.

    ``now`` is passed for event time (D9) and is not used to decide whether a
    horizon has passed: the SPY session calendar answers that, and it only holds
    sessions the close job has already stored, which are settled closes.
    """
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    horizons = settings.outcomes.horizons_trading_days
    cards = CardRepo(engine).list_cards(limit=settings.outcomes.max_cards_per_run)
    if not cards:
        return OutcomeJobReport()
    repo = OutcomeRepo(engine)
    stored = {
        (outcome.card_id, outcome.horizon_days): outcome
        for outcome in repo.list_outcomes(
            card_ids=[card.id for card in cards],
            limit=len(cards) * len(horizons) + 1,
        )
    }
    closes = load_closes_by_ticker(
        engine, [BENCHMARK_TICKER, *(card.ticker for card in cards)],
    )
    indexes = {ticker: build_close_index(points) for ticker, points in closes.items()}
    spy = indexes.get(BENCHMARK_TICKER, build_close_index(()))
    sessions = spy.days
    empty = build_close_index(())
    tally = _Tally()
    bids: dict[str, BidFetch] = {}
    for card in cards:
        for horizon in horizons:
            existing = stored.get((card.id, horizon))
            if existing is not None and existing.is_final:
                continue
            resolution = resolve_outcome(
                card, horizon,
                sessions=sessions,
                underlying=indexes.get(card.ticker, empty),
                spy=spy,
            )
            if resolution.state == "not_due":
                continue
            await _store(client, repo, card, horizon, resolution, existing, bids, tally)
    return OutcomeJobReport(
        cards=len(cards),
        computed=tally.computed,
        no_data=tally.no_data,
        pending=tally.pending,
        advanced=tally.advanced,
        requests=tally.requests,
        degraded=tally.degraded,
    )


async def _store(
    client: JsonClient,
    repo: OutcomeRepo,
    card: DecisionCard,
    horizon: int,
    resolution: OutcomeResolution,
    existing: Outcome | None,
    bids: dict[str, BidFetch],
    tally: _Tally,
) -> None:
    """Write (or advance) one row. A row is only ever finalised once."""
    final = resolution.final
    option_bid: float | None = None
    if final is not None and final.status == COMPUTED:
        fetched = await _bids(client, card.dominant_option_symbol, bids, tally)
        if fetched is not None and fetched.degraded:
            # Our fetch failed; the primary is not lost, and nothing is frozen.
            final = None
        elif fetched is not None and resolution.horizon_day is not None:
            option_bid = fetched.bids.get(resolution.horizon_day)
    if final is None:
        if existing is None:
            repo.write_pending(
                card_id=card.id, horizon_days=horizon,
                option_symbol=card.dominant_option_symbol,
            )
            tally.pending += 1
        return
    stored_final = FinalOutcome(
        status=final.status, measurement=final.measurement, option_bid_at_horizon=option_bid,
    )
    if existing is None:
        repo.write_final(
            card_id=card.id, horizon_days=horizon, final=stored_final,
            option_symbol=card.dominant_option_symbol,
        )
    elif repo.advance(card_id=card.id, horizon_days=horizon, final=stored_final):
        tally.advanced += 1
    else:  # another writer finalised it first; the stored row wins
        return
    if stored_final.status == COMPUTED:
        tally.computed += 1
    else:
        tally.no_data += 1


async def _bids(
    client: JsonClient,
    symbol: str | None,
    bids: dict[str, BidFetch],
    tally: _Tally,
) -> BidFetch | None:
    """This run's bids for ``symbol``, fetched at most once. ``None`` when the card names none."""
    if not symbol:
        return None
    if symbol not in bids:
        fetched = await fetch_historic_bids(client, symbol)
        tally.requests += 1
        tally.degraded += int(fetched.degraded)
        bids[symbol] = fetched
    return bids[symbol]


# ---------------------------------------------------------------------------
# The registry entry (contract §4: the daily post-close job)
# ---------------------------------------------------------------------------


class OutcomeJobContext(Protocol):
    """What this job needs from the refresher's ``daily_jobs.JobContext``.

    A protocol rather than an import: the clock lives in ``webapp/board/daily_jobs.py``,
    whose registry appends this job as one more entry, and a job that imported
    the clock that imports it would be a cycle. ``JobContext`` satisfies this
    structurally under ``mypy --strict``, and a test fake needs nothing else.
    """

    @property
    def client(self) -> JsonClient: ...
    @property
    def engine(self) -> Engine: ...
    @property
    def settings(self) -> BoardSettings: ...
    @property
    def now(self) -> datetime: ...


async def run_outcomes(ctx: OutcomeJobContext) -> str:
    """The job body the registry calls; returns the short English marker detail."""
    report = await run_outcome_job(
        ctx.client, ctx.engine, settings=ctx.settings, now=ctx.now,
    )
    return report.detail


@dataclass(frozen=True)
class OutcomeJob:
    """One daily-job registry entry, shaped exactly like ``daily_jobs.DailyJob``."""

    name: str
    et_time: time
    run: Callable[[OutcomeJobContext], Awaitable[str]]
    trading_day_only: bool = True


def outcome_job_time(settings: BoardSettings) -> time:
    """``outcomes.job_time_et`` as a clock time (D8: the profile, never a literal)."""
    return time.fromisoformat(settings.outcomes.job_time_et)


def outcome_jobs(settings: BoardSettings) -> tuple[OutcomeJob, ...]:
    """The ONE entry FAZ C appends to the daily-job registry, at ``outcomes.job_time_et``.

    ``daily_jobs.build_registry`` ends with this entry appended:

        DailyJob(name=OUTCOME_JOB_NAME, et_time=outcome_job_time(settings), run=run_outcomes)
    """
    return (
        OutcomeJob(
            name=OUTCOME_JOB_NAME,
            et_time=outcome_job_time(settings),
            run=run_outcomes,
        ),
    )
