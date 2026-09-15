"""Alfa Board refresher: quotes, exit depth, net-premium tape, ticker info (Phase 5.2.A2, A3).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("Board refresher"),
§4.4 (request budget), §5 A2 and §5 A3; decisions P16 and P17.

One cycle runs every ``refresh.cadence_seconds`` during regular trading hours:

1. Today's live run (``live-<UTC date>``) is read from the database and
   aggregated into the same rows ``GET /alfa`` shows.
2. Contracts are quoted through ``/api/stock/{ticker}/option-contracts``:
   - open journal legs first (critical: the owner holds them);
   - then each row's dominant contract, in row order;
   - then every other contract the run references.
   One call per underlying, with at most ``refresh.max_symbols_per_request``
   symbols per call.
3. Exit depth comes from ``/api/option-contract/{symbol}/flow?limit=1``, for
   the dominant contracts of the top ``refresh.exit_depth_top_k`` rows.
4. A3: the net-premium tape, one ``/api/stock/{ticker}/net-prem-ticks`` call per
   board ticker (``webapp/board/netprem.py``).
5. A3: ticker info, one ``/api/stock/{ticker}/info`` call per board ticker with
   no row fetched on the current UTC day (``webapp/board/ticker_info.py``).
   This is the daily job. It runs inside the RTH loop, so each ticker is
   fetched on its first cycle of the day.

The steps run in this order inside one ``try``. An error in any step ends that
cycle; the loop logs it and waits one cadence.

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
- An auth error is logged at ERROR and the loop waits a full cadence rather
  than restarting, so a bad key is loud without burning quota in a crash
  loop. Any other cycle error is logged the same way.

``GET /alfa`` never calls Unusual Whales; it only reads what this loop writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

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
from webapp.board.netprem import ensure_netprem_tables, fetch_net_prem_ticks, upsert_tape
from webapp.board.quotes import (
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
    ensure_ticker_info_tables,
    fetch_ticker_info,
    tickers_needing_info,
    upsert_ticker_infos,
)
from webapp.journal import JournalRepo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    from sqlalchemy.engine import Engine

    from uoa_detector.calibration.profile import UnusualWhalesSettings
    from webapp.board.aggregate import BoardRow
    from webapp.board.settings import BoardSettings
    from webapp.journal import TradeRow

_logger = logging.getLogger(__name__)

_LIVE_CALIBRATION_PROFILE: Final = Path("profiles/v5_default.yaml")  # the live worker's profile
_LIVE_RUN_PREFIX: Final = "live-"
_ISO_DATE_LENGTH: Final = len("YYYY-MM-DD")
_OPEN_STATUS: Final = "open"
_OPTION_INSTRUMENTS: Final = frozenset({"call", "put"})


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
    run_id: str
    requests: int
    quotes_written: int
    depths_written: int
    degraded_fetches: int
    skipped_non_critical: bool
    tickers: tuple[str, ...] = ()  # the board's tickers in row order (A3: tape and ticker info)


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
) -> CycleReport:
    """One quotes-and-depth cycle. Daily-limit and auth errors propagate."""
    now = clock()
    run_id = f"{_LIVE_RUN_PREFIX}{now.date().isoformat()}"
    rows = build_board_rows(reader.load_run(run_id), settings.aggregation)
    plan = _plan(journal_legs(journal.list(status=_OPEN_STATUS), now.date()), signal_legs(rows))
    refresh = settings.refresh
    requests = quotes_written = depths_written = degraded = 0
    skipped = False

    for ticker, legs in plan.items():
        pending = list(legs)
        while pending:
            if soft_cap.reached(clock()):
                critical_only = [leg for leg in pending if leg.critical]
                skipped = skipped or len(critical_only) < len(pending)
                pending = critical_only
                if not pending:
                    break
            chunk = pending[: refresh.max_symbols_per_request]
            pending = pending[refresh.max_symbols_per_request :]
            fetch = await fetch_contract_quotes(client, ticker, [leg.symbol for leg in chunk])
            requests += 1
            soft_cap.observe(client.last_daily_request_count, clock())
            if fetch.degraded:
                degraded += 1
                continue
            quotes_written += upsert_quotes(engine, fetch.quotes, fetched_at=clock())

    top_symbols = list(
        dict.fromkeys(
            symbol
            for symbol in (dominant_symbol(row) for row in rows[: refresh.exit_depth_top_k])
            if symbol is not None
        ),
    )
    for symbol in top_symbols:
        if soft_cap.reached(clock()):
            skipped = True
            break
        depth = await fetch_contract_depth(client, symbol)
        requests += 1
        soft_cap.observe(client.last_daily_request_count, clock())
        if depth.degraded:
            degraded += 1
            continue
        if depth.depth is not None:
            depths_written += upsert_depths(engine, [depth.depth], fetched_at=clock())

    if skipped:
        _warn_soft_cap(soft_cap, refresh.daily_request_soft_cap, "non-critical fetches")
    return CycleReport(
        run_id=run_id,
        requests=requests,
        quotes_written=quotes_written,
        depths_written=depths_written,
        degraded_fetches=degraded,
        skipped_non_critical=skipped,
        tickers=tuple(dict.fromkeys(row.ticker for row in rows)),
    )


async def run_tape_cycle(
    client: BoardClient,
    engine: Engine,
    *,
    tickers: Sequence[str],
    soft_cap: SoftCap,
    clock: Callable[[], datetime],
) -> StepReport:
    """One ``net-prem-ticks`` call per ticker (non-critical). Daily-limit and auth errors propagate."""
    requests = written = degraded = 0
    skipped = False
    for ticker in dict.fromkeys(t.strip().upper() for t in tickers if t.strip()):
        if soft_cap.reached(clock()):
            skipped = True
            break
        fetch = await fetch_net_prem_ticks(client, ticker)
        requests += 1
        soft_cap.observe(client.last_daily_request_count, clock())
        if fetch.degraded:
            degraded += 1
            continue
        written += upsert_tape(engine, fetch.ticker, fetch.minutes, fetched_at=clock())
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
    """``/info`` for tickers not fetched today (non-critical). Daily-limit and auth errors propagate."""
    requests = written = degraded = 0
    skipped = False
    for ticker in tickers_needing_info(engine, tickers, today=clock().date()):
        if soft_cap.reached(clock()):
            skipped = True
            break
        fetch = await fetch_ticker_info(client, ticker)
        requests += 1
        soft_cap.observe(client.last_daily_request_count, clock())
        if fetch.degraded or fetch.snapshot is None:
            degraded += 1
            continue
        written += upsert_ticker_infos(engine, [fetch.snapshot], fetched_at=clock())
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
                )
                tape = await run_tape_cycle(
                    client, engine, tickers=report.tickers, soft_cap=soft_cap, clock=now_fn,
                )
                info = await run_ticker_info_job(
                    client, engine, tickers=report.tickers, soft_cap=soft_cap, clock=now_fn,
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
                _logger.info(
                    "board refresher %s: tape %d requests, %d minutes written, %d degraded; "
                    "ticker info %d requests, %d written, %d degraded",
                    report.run_id, tape.requests, tape.written, tape.degraded_fetches,
                    info.requests, info.written, info.degraded_fetches,
                )
            await pause(refresh.cadence_seconds)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        with contextlib.suppress(Exception):
            engine.dispose()
