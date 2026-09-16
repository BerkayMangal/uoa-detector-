"""Alfa Board refresher: quotes, exit depth, net-premium tape, ticker info (Phase 5.2.A2-A4).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher"),
§4.4 (request budget), §5 A2, §5 A3 and §5 A4; decisions P16 and P17.

One cycle runs every ``refresh.cadence_seconds`` during regular trading hours:

1. The live run that holds today's prints is read from the database and
   aggregated into the same rows ``GET /`` shows. It is the ``live-*`` run with
   the newest print, provided that print is on the current ET trading date
   (``current_live_run``). The id is never derived from the calendar date: the
   live worker keeps writing into the run id it started with, across a UTC
   midnight, until it restarts (review RT-2). With no live print today there
   is no board run, and only open journal legs are quoted.
2. Contracts are quoted through ``/api/stock/{ticker}/option-contracts``:
   - open journal legs first (critical: the owner holds them);
   - then each row's dominant contract, in row order;
   - then every other contract the run references.
   One call per underlying, with at most ``refresh.max_symbols_per_request``
   symbols per call.
3. Exit depth comes from ``/api/option-contract/{symbol}/flow?limit=1``, for
   the dominant contracts of the top ``refresh.exit_depth_top_k`` rows in the
   board's order (A4: lehte desc, aleyhte asc, bilinmiyor asc, total premium
   desc). If the evidence tables cannot be read, total premium order is used.
4. A3: the net-premium tape, one ``/api/stock/{ticker}/net-prem-ticks`` call per
   board ticker (``webapp/board/netprem.py``).
5. A3: ticker info, one ``/api/stock/{ticker}/info`` call per board ticker with
   no row fetched on the current UTC day (``webapp/board/ticker_info.py``).
   This is the daily job. It runs inside the RTH loop, so each ticker is
   fetched on its first cycle of the day.

Event loop. The refresher shares the web server's asyncio loop, so every
synchronous database call (run load, journal read, evidence order, upserts,
the ticker-info check) runs in a worker thread through ``asyncio.to_thread``,
and each upsert is one statement per batch (review RT-1).

Error containment (review RT-3):

- Loading the board run is the only step whose failure ends the cycle.
- Quotes, depth, tape and ticker info are isolated steps. A failure in one is
  logged (``board refresher cycle failed at step ...``) and the next step
  still runs.
- A 4xx other than 401/403 on one call (``UnusualWhalesAuthError`` that is not
  a key failure) marks that ticker's or contract's fetch degraded; the other
  tickers continue.

Guards (the ``flow_poll`` pattern; decision P17):

- ONE long-lived ``UnusualWhalesClient`` for the loop's lifetime, so its token
  bucket, circuit breaker and caches survive between cycles.
- Outside RTH (``sources.market_hours.is_market_open``) the loop sleeps
  ``refresh.closed_market_sleep_seconds``.
- While the client's circuit breaker is open, the cycle is skipped.
- ``UnusualWhalesDailyLimitError``: sleep ``refresh.daily_limit_backoff_seconds``.
- Soft cap: once the client's last seen ``x-uw-daily-req-count`` reaches
  ``refresh.daily_request_soft_cap`` on the same UTC day, non-critical fetches
  pause: signal-contract quotes, every depth call, the tape and ticker info.
  Journal-leg quotes continue.
- A key failure (HTTP 401/403, or an auth error without a status) is logged
  at ERROR and the loop waits a full cadence rather than restarting, so a bad
  key is loud without burning quota in a crash loop.

``GET /`` never calls Unusual Whales; it only reads what this loop writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials
from uoa_detector.sources.market_hours import is_market_open
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
)
from webapp.board.aggregate import build_board_rows
from webapp.board.db import make_engine
from webapp.board.evidence import (
    LegacyScores,
    build_row_evidence,
    evidence_sort_key,
    read_evidence_inputs,
    request_for,
    trade_date_et,
)
from webapp.board.netprem import TapeFetch, ensure_netprem_tables, fetch_net_prem_ticks, upsert_tape
from webapp.board.quotes import (
    DepthFetch,
    QuoteFetch,
    contract_symbol,
    dominant_symbol,
    ensure_quotes_tables,
    fetch_contract_depth,
    fetch_contract_quotes,
    occ_symbol,
    upsert_depths,
    upsert_quotes,
)
from webapp.board.settings import DEFAULT_BOARD_PROFILE, load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.board.ticker_info import (
    TickerInfoFetch,
    ensure_ticker_info_tables,
    fetch_ticker_info,
    tickers_needing_info,
    upsert_ticker_infos,
)
from webapp.journal import JournalRepo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence

    from sqlalchemy.engine import Engine

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from webapp.board.aggregate import BoardRow
    from webapp.board.settings import BoardSettings
    from webapp.board.signals import BoardPrint
    from webapp.journal import TradeRow

_logger = logging.getLogger(__name__)

_LIVE_CALIBRATION_PROFILE: Final = Path("profiles/v5_default.yaml")  # the live worker's profile
_LIVE_RUN_PREFIX: Final = "live-"
_ISO_DATE_LENGTH: Final = len("YYYY-MM-DD")
_OPEN_STATUS: Final = "open"
_OPTION_INSTRUMENTS: Final = frozenset({"call", "put"})
# HTTP statuses that mean the key itself failed (every call would fail the same way).
_KEY_FAILURE_STATUSES: Final = frozenset({401, 403})
_HTTP_STATUS: Final = re.compile(r"HTTP (\d{3})")

_T = TypeVar("_T")


class _Breaker(Protocol):
    def is_open(self) -> bool: ...


class BoardClient(Protocol):
    """What the refresher needs from ``UnusualWhalesClient``."""

    @property
    def circuit_breaker(self) -> _Breaker: ...

    @property
    def last_daily_request_count(self) -> int | None: ...

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class OpenTrades(Protocol):
    """What the refresher needs from ``JournalRepo``."""

    def list(self, status: str | None = ...) -> list[TradeRow]: ...


class SoftCap:
    """Pauses non-critical fetches once the key-wide daily count reaches the cap.

    The count is the client's last seen ``x-uw-daily-req-count``, stamped with
    the UTC day it was seen, so a count from an earlier day never pauses today.
    An unchanged value (no new header since) is not re-stamped.
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._count: int | None = None
        self._day: date | None = None

    @property
    def count(self) -> int | None:
        return self._count

    def observe(self, count: int | None, now: datetime) -> None:
        if count is None or count == self._count:
            return
        self._count = count
        self._day = now.date()

    def reached(self, now: datetime) -> bool:
        return self._count is not None and self._day == now.date() and self._count >= self._cap


@dataclass(frozen=True)
class CycleReport:
    run_id: str | None  # None: no live run has a print on the current ET trading date
    requests: int
    quotes_written: int
    depths_written: int
    degraded_fetches: int
    skipped_non_critical: bool
    tickers: tuple[str, ...] = ()  # the board's tickers in row order (A3: tape and ticker info)
    failed_steps: tuple[str, ...] = ()  # isolated steps that raised (review RT-3)


@dataclass(frozen=True)
class StepReport:
    """One A3 step: the net-premium tape or the ticker-info job."""

    requests: int
    written: int
    degraded_fetches: int
    skipped_non_critical: bool


@dataclass(frozen=True)
class _Leg:
    ticker: str
    symbol: str
    critical: bool


@dataclass(frozen=True)
class _Board:
    run_id: str | None
    prints: list[BoardPrint]
    rows: list[BoardRow]


@dataclass
class _Tally:
    requests: int = 0
    quotes_written: int = 0
    depths_written: int = 0
    degraded: int = 0
    skipped: bool = False


def is_key_failure(error: UnusualWhalesAuthError) -> bool:
    """True for HTTP 401/403, or an auth error whose status cannot be read; False for another 4xx."""
    found = _HTTP_STATUS.search(str(error))
    return found is None or int(found.group(1)) in _KEY_FAILURE_STATUSES


def _warn_degraded_4xx(what: str, error: UnusualWhalesAuthError) -> None:
    _logger.warning("board refresher: %s rejected by UW (%s); marked degraded, continuing", what, error)


# Any: arguments are forwarded unchanged to fn, as asyncio.to_thread's own signature does.
async def _off_loop(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a synchronous database call in a worker thread so the shared event loop keeps serving."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# Any: a coroutine's send and yield types play no part here; only its result type is kept.
async def _isolated(step: str, work: Coroutine[Any, Any, _T], cadence_seconds: int) -> _T | None:
    """Await one refresher step. Daily-limit and key failures propagate; any other error is logged."""
    try:
        return await work
    except (UnusualWhalesDailyLimitError, UnusualWhalesAuthError):
        raise
    except Exception:
        _logger.exception(
            "board refresher cycle failed at step %s; next attempt in %ss", step, cadence_seconds,
        )
        return None


def current_live_run(engine: Engine, now: datetime) -> str | None:
    """The live run holding today's prints, or None.

    It is the ``live-*`` run whose newest print is the newest overall, kept
    only when that print falls on ``now``'s ET trading date. This matches the
    page's default run (the newest run by print time) and survives a worker
    that kept writing into yesterday's run id past a UTC midnight.
    """
    stmt = (
        select(SignalRow.run_id, func.max(SignalRow.ts))
        .where(SignalRow.run_id.like(f"{_LIVE_RUN_PREFIX}%"))
        .group_by(SignalRow.run_id)
    )
    with Session(engine) as session:
        latest = [(ts, run_id) for run_id, ts in session.execute(stmt) if isinstance(ts, datetime)]
    if not latest:
        return None
    newest_ts, run_id = max((_as_utc(ts), run_id) for ts, run_id in latest)
    return run_id if trade_date_et(newest_ts) == trade_date_et(now) else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _load_board(
    engine: Engine, settings: BoardSettings, reader: BoardSignalReader, now: datetime,
) -> _Board:
    run_id = current_live_run(engine, now)
    if run_id is None:
        return _Board(run_id=None, prints=[], rows=[])
    prints = reader.load_run(run_id)
    return _Board(run_id=run_id, prints=prints, rows=build_board_rows(prints, settings.aggregation))


def _expiry(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) < _ISO_DATE_LENGTH:
        return None
    try:
        return date.fromisoformat(text[:_ISO_DATE_LENGTH])
    except ValueError:
        return None


def _strike(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if not math.isfinite(raw) or raw <= 0:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def journal_legs(trades: Iterable[TradeRow], today: date) -> list[tuple[str, str]]:
    """(ticker, symbol) for each open, unexpired option leg; anything unparseable is skipped."""
    legs: list[tuple[str, str]] = []
    for trade in trades:
        ticker = trade.ticker.strip().upper() if isinstance(trade.ticker, str) else ""
        kind = trade.instrument.strip().lower() if isinstance(trade.instrument, str) else ""
        expiry = _expiry(trade.expiry)
        strike = _strike(trade.strike)
        if not ticker or kind not in _OPTION_INSTRUMENTS or expiry is None or strike is None:
            continue
        if expiry < today:
            continue
        symbol = occ_symbol(ticker, expiry, kind, strike)
        if symbol is not None:
            legs.append((ticker, symbol))
    return legs


def signal_legs(rows: Sequence[BoardRow]) -> list[tuple[str, str]]:
    """(ticker, symbol): each row's dominant contract in row order, then every other contract."""
    ordered: dict[str, str] = {}
    for row in rows:
        symbol = dominant_symbol(row)
        if symbol is not None:
            ordered.setdefault(symbol, row.ticker)
    for row in rows:
        for contract in row.contracts[1:]:
            symbol = contract_symbol(row.ticker, contract)
            if symbol is not None:
                ordered.setdefault(symbol, row.ticker)
    return [(ticker, symbol) for symbol, ticker in ordered.items()]


def _plan(
    critical: Sequence[tuple[str, str]], signal: Sequence[tuple[str, str]],
) -> dict[str, list[_Leg]]:
    by_symbol: dict[str, _Leg] = {}
    for ticker, symbol in critical:
        by_symbol.setdefault(symbol, _Leg(ticker=ticker, symbol=symbol, critical=True))
    for ticker, symbol in signal:
        by_symbol.setdefault(symbol, _Leg(ticker=ticker, symbol=symbol, critical=False))
    plan: dict[str, list[_Leg]] = {}
    for leg in by_symbol.values():
        plan.setdefault(leg.ticker, []).append(leg)
    return plan


def board_order(
    engine: Engine,
    settings: BoardSettings,
    run_id: str,
    rows: Sequence[BoardRow],
    prints: Sequence[BoardPrint],
    *,
    legacy_scores: LegacyScores | None,
    now: datetime,
) -> list[BoardRow]:
    """Rows in the board's evidence order; total premium order when the evidence cannot be read."""
    if not rows:
        return []
    requests = [request_for(row) for row in rows]
    try:
        inputs = read_evidence_inputs(engine, run_id, requests)
    except Exception:
        _logger.warning(
            "board refresher: evidence tables unreadable; top-K depth keeps total premium order",
            exc_info=True,
        )
        return list(rows)
    signals = {p.event_id: p.signal for p in prints}
    keys = [
        evidence_sort_key(
            build_row_evidence(
                row,
                settings=settings,
                now=now,
                signal=signals.get(request.event_id),
                telemetry=inputs.telemetry.get(request.event_id),
                tape=inputs.tapes.get((request.ticker, request.trade_date)),
                ticker_info=inputs.infos.get(request.ticker),
                legacy_scores=legacy_scores,
            ).counts,
            row.total_premium,
        )
        for row, request in zip(rows, requests, strict=True)
    ]
    order = sorted(range(len(rows)), key=lambda i: (keys[i], rows[i].ticker, rows[i].direction))
    return [rows[i] for i in order]


def _warn_soft_cap(soft_cap: SoftCap, cap: int | None, what: str) -> None:
    _logger.warning(
        "board refresher: daily request count %s reached the soft cap %s; %s paused",
        soft_cap.count, cap, what,
    )


async def run_quotes_cycle(
    client: BoardClient,
    engine: Engine,
    settings: BoardSettings,
    *,
    reader: BoardSignalReader,
    journal: OpenTrades,
    soft_cap: SoftCap,
    clock: Callable[[], datetime],
    legacy_scores: LegacyScores | None = None,
) -> CycleReport:
    """One quotes-and-depth cycle. Daily-limit errors and key failures propagate.

    A failure while loading the board run propagates too. The quotes step and
    the depth step are isolated from each other: an error in one is logged and
    listed in ``failed_steps``.
    """
    now = clock()
    refresh = settings.refresh
    board = await _off_loop(_load_board, engine, settings, reader, now)
    tally = _Tally()
    failed: list[str] = []
    quotes_step = _quotes_step(
        client, engine, board, journal, soft_cap, clock, tally,
        max_symbols=refresh.max_symbols_per_request, now=now,
    )
    if await _isolated("quotes", quotes_step, refresh.cadence_seconds) is None:
        failed.append("quotes")
    depth_step = _depth_step(
        client, engine, settings, board, soft_cap, clock, tally, legacy_scores=legacy_scores, now=now,
    )
    if await _isolated("depth", depth_step, refresh.cadence_seconds) is None:
        failed.append("depth")
    if tally.skipped:
        _warn_soft_cap(soft_cap, refresh.daily_request_soft_cap, "non-critical fetches")
    return CycleReport(
        run_id=board.run_id,
        requests=tally.requests,
        quotes_written=tally.quotes_written,
        depths_written=tally.depths_written,
        degraded_fetches=tally.degraded,
        skipped_non_critical=tally.skipped,
        tickers=tuple(dict.fromkeys(row.ticker for row in board.rows)),
        failed_steps=tuple(failed),
    )


async def _quotes_step(
    client: BoardClient,
    engine: Engine,
    board: _Board,
    journal: OpenTrades,
    soft_cap: SoftCap,
    clock: Callable[[], datetime],
    tally: _Tally,
    *,
    max_symbols: int,
    now: datetime,
) -> bool:
    open_trades = await _off_loop(journal.list, status=_OPEN_STATUS)
    plan = _plan(journal_legs(open_trades, now.date()), signal_legs(board.rows))
    for ticker, legs in plan.items():
        pending = list(legs)
        while pending:
            if soft_cap.reached(clock()):
                critical_only = [leg for leg in pending if leg.critical]
                tally.skipped = tally.skipped or len(critical_only) < len(pending)
                pending = critical_only
                if not pending:
                    break
            chunk = pending[:max_symbols]
            pending = pending[max_symbols:]
            fetch: QuoteFetch
            try:
                fetch = await fetch_contract_quotes(client, ticker, [leg.symbol for leg in chunk])
            except UnusualWhalesAuthError as exc:
                if is_key_failure(exc):
                    raise
                _warn_degraded_4xx(f"option-contracts for {ticker}", exc)
                fetch = QuoteFetch(quotes=(), degraded=True)
            tally.requests += 1
            soft_cap.observe(client.last_daily_request_count, clock())
            if fetch.degraded:
                tally.degraded += 1
                continue
            tally.quotes_written += await _off_loop(upsert_quotes, engine, fetch.quotes, fetched_at=clock())
    return True


async def _depth_step(
    client: BoardClient,
    engine: Engine,
    settings: BoardSettings,
    board: _Board,
    soft_cap: SoftCap,
    clock: Callable[[], datetime],
    tally: _Tally,
    *,
    legacy_scores: LegacyScores | None,
    now: datetime,
) -> bool:
    if board.run_id is None or not board.rows:
        return True
    ranked = await _off_loop(
        board_order, engine, settings, board.run_id, board.rows, board.prints,
        legacy_scores=legacy_scores, now=now,
    )
    top_symbols = list(
        dict.fromkeys(
            symbol
            for symbol in (dominant_symbol(row) for row in ranked[: settings.refresh.exit_depth_top_k])
            if symbol is not None
        ),
    )
    for symbol in top_symbols:
        if soft_cap.reached(clock()):
            tally.skipped = True
            break
        depth: DepthFetch
        try:
            depth = await fetch_contract_depth(client, symbol)
        except UnusualWhalesAuthError as exc:
            if is_key_failure(exc):
                raise
            _warn_degraded_4xx(f"flow for {symbol}", exc)
            depth = DepthFetch(depth=None, degraded=True)
        tally.requests += 1
        soft_cap.observe(client.last_daily_request_count, clock())
        if depth.degraded:
            tally.degraded += 1
            continue
        if depth.depth is not None:
            tally.depths_written += await _off_loop(upsert_depths, engine, [depth.depth], fetched_at=clock())
    return True


async def run_tape_cycle(
    client: BoardClient,
    engine: Engine,
    *,
    tickers: Sequence[str],
    soft_cap: SoftCap,
    clock: Callable[[], datetime],
) -> StepReport:
    """One ``net-prem-ticks`` call per ticker (non-critical). Daily-limit errors and key failures propagate."""
    requests = written = degraded = 0
    skipped = False
    for ticker in dict.fromkeys(t.strip().upper() for t in tickers if t.strip()):
        if soft_cap.reached(clock()):
            skipped = True
            break
        fetch: TapeFetch
        try:
            fetch = await fetch_net_prem_ticks(client, ticker)
        except UnusualWhalesAuthError as exc:
            if is_key_failure(exc):
                raise
            _warn_degraded_4xx(f"net-prem-ticks for {ticker}", exc)
            fetch = TapeFetch(ticker=ticker, minutes=(), degraded=True)
        requests += 1
        soft_cap.observe(client.last_daily_request_count, clock())
        if fetch.degraded:
            degraded += 1
            continue
        written += await _off_loop(upsert_tape, engine, fetch.ticker, fetch.minutes, fetched_at=clock())
    if skipped:
        _warn_soft_cap(soft_cap, None, "the net-premium tape")
    return StepReport(requests=requests, written=written, degraded_fetches=degraded, skipped_non_critical=skipped)


async def run_ticker_info_job(
    client: BoardClient,
    engine: Engine,
    *,
    tickers: Sequence[str],
    soft_cap: SoftCap,
    clock: Callable[[], datetime],
) -> StepReport:
    """``/info`` for tickers not fetched today (non-critical). Daily-limit errors and key failures propagate."""
    requests = written = degraded = 0
    skipped = False
    for ticker in await _off_loop(tickers_needing_info, engine, tickers, today=clock().date()):
        if soft_cap.reached(clock()):
            skipped = True
            break
        fetch: TickerInfoFetch
        try:
            fetch = await fetch_ticker_info(client, ticker)
        except UnusualWhalesAuthError as exc:
            if is_key_failure(exc):
                raise
            _warn_degraded_4xx(f"info for {ticker}", exc)
            fetch = TickerInfoFetch(ticker=ticker, snapshot=None, degraded=True)
        requests += 1
        soft_cap.observe(client.last_daily_request_count, clock())
        if fetch.degraded or fetch.snapshot is None:
            degraded += 1
            continue
        written += await _off_loop(upsert_ticker_infos, engine, [fetch.snapshot], fetched_at=clock())
    if skipped:
        _warn_soft_cap(soft_cap, None, "ticker info")
    return StepReport(requests=requests, written=written, degraded_fetches=degraded, skipped_non_critical=skipped)


async def board_refresh_loop(
    *,
    database_url: str,
    profile_path: Path = _LIVE_CALIBRATION_PROFILE,
    board_profile_path: Path = DEFAULT_BOARD_PROFILE,
    client_factory: Callable[[UnusualWhalesSettings], BoardClient] | None = None,
    journal_factory: Callable[[str], OpenTrades] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Refresh quotes, exit depth, the tape and ticker info until cancelled. Runs under ``_supervise``."""
    profile = load_profile(profile_path)
    settings = load_board_settings(board_profile_path)
    legacy_scores = LegacyScores.from_profile(profile)
    refresh = settings.refresh
    now_fn: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
    pause: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
    engine = make_engine(database_url)
    client: BoardClient | None = None
    try:
        ensure_quotes_tables(engine)
        ensure_netprem_tables(engine)
        ensure_ticker_info_tables(engine)
        reader = BoardSignalReader(engine=engine)
        journal: OpenTrades = (
            journal_factory(database_url) if journal_factory is not None else JournalRepo(database_url)
        )
        uw_settings = profile.data_sources.unusual_whales
        client = (
            client_factory(uw_settings)
            if client_factory is not None
            else UnusualWhalesClient(
                api_key=Credentials().require_unusual_whales_api_key(), settings=uw_settings,
            )
        )
        soft_cap = SoftCap(refresh.daily_request_soft_cap)
        _logger.info("board refresher started (cadence %ss)", refresh.cadence_seconds)
        while True:
            if not is_market_open(now_fn()):
                await pause(refresh.closed_market_sleep_seconds)
                continue
            if client.circuit_breaker.is_open():
                _logger.warning("board refresher: UW circuit breaker is open; skipping this cycle")
                await pause(refresh.cadence_seconds)
                continue
            try:
                report = await run_quotes_cycle(
                    client, engine, settings,
                    reader=reader, journal=journal, soft_cap=soft_cap, clock=now_fn,
                    legacy_scores=legacy_scores,
                )
                tape = await _isolated(
                    "tape",
                    run_tape_cycle(client, engine, tickers=report.tickers, soft_cap=soft_cap, clock=now_fn),
                    refresh.cadence_seconds,
                )
                info = await _isolated(
                    "ticker info",
                    run_ticker_info_job(client, engine, tickers=report.tickers, soft_cap=soft_cap, clock=now_fn),
                    refresh.cadence_seconds,
                )
            except UnusualWhalesDailyLimitError:
                _logger.warning(
                    "board refresher: UW daily request limit reached; backing off %ss",
                    refresh.daily_limit_backoff_seconds,
                )
                await pause(refresh.daily_limit_backoff_seconds)
                continue
            except UnusualWhalesAuthError:
                _logger.exception(
                    "board refresher: UW auth error; next attempt in %ss", refresh.cadence_seconds,
                )
            except Exception:
                _logger.exception("board refresher cycle failed; next attempt in %ss", refresh.cadence_seconds)
            else:
                _logger.info(
                    "board refresher %s: %d requests, %d quotes, %d depths, %d degraded, non-critical paused=%s",
                    report.run_id, report.requests, report.quotes_written, report.depths_written,
                    report.degraded_fetches, report.skipped_non_critical,
                )
                for step, step_report in (("tape", tape), ("ticker info", info)):
                    if step_report is not None:
                        _logger.info(
                            "board refresher %s: %s %d requests, %d written, %d degraded",
                            report.run_id, step, step_report.requests, step_report.written,
                            step_report.degraded_fetches,
                        )
            await pause(refresh.cadence_seconds)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        with contextlib.suppress(Exception):
            engine.dispose()
