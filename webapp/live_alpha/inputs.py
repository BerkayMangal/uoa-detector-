"""Database reads behind one Live Alpha cycle (contract §3). No Unusual Whales call.

Everything the engine needs from tables other jobs already fill: the newest
``live-*`` run's prints (live worker), the spot (board refresher, ``alfa_atm``),
daily bars (daily_close job, ``alfa_daily_bar``) and listed expiries
(``alfa_atm_expiry``). Each read is one query for all tickers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.live_alpha.model import CheckState, FlowPrint, PriceContext
from webapp.board.atm import AlfaAtm, AlfaAtmExpiry
from webapp.board.daily_close import load_bars_by_ticker
from webapp.board.db import session_factory
from webapp.board.spot import Bar, true_ranges, wilder_atr

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.signals import BoardSignalReader
    from webapp.ohlc import RegularBar

_ET = ZoneInfo("America/New_York")
LIVE_RUN_PREFIX = "live-"


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on timezone-aware columns; every stored time is UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def latest_live_run(engine: Engine) -> tuple[str, datetime] | None:
    """The ``live-*`` run holding the newest print, with that print's time."""
    stmt = (
        select(SignalRow.run_id, func.max(SignalRow.ts))
        .where(SignalRow.run_id.like(f"{LIVE_RUN_PREFIX}%"))
        .group_by(SignalRow.run_id)
    )
    with session_factory(engine)() as s:
        rows = [(as_utc(ts), run_id) for run_id, ts in s.execute(stmt) if isinstance(ts, datetime)]
    if not rows:
        return None
    ts, run_id = max(rows)
    return run_id, ts


def flow_prints(reader: BoardSignalReader, run_id: str) -> list[FlowPrint]:
    out: list[FlowPrint] = []
    for bp in reader.load_run(run_id):
        sig = bp.signal
        out.append(FlowPrint(
            event_id=bp.event_id,
            ticker=sig.ticker.upper(),
            ts=as_utc(sig.timestamp),
            option_type=str(sig.option_type),
            strike=sig.strike,
            expiry=sig.expiry,
            premium=sig.premium,
            fill_side=bp.meta.fill_side if bp.meta is not None else None,
            option_chain=bp.meta.option_chain if bp.meta is not None else None,
        ))
    return out


@dataclass(frozen=True)
class SpotRead:
    spot: float | None
    fetched_at: datetime
    trade_date: date | None


def latest_spots(engine: Engine, tickers: Sequence[str]) -> dict[str, SpotRead]:
    """The newest ``alfa_atm`` stock price per ticker (all expiries share one fetch)."""
    symbols = sorted({t.upper() for t in tickers})
    if not symbols:
        return {}
    stmt = (
        select(AlfaAtm.ticker, AlfaAtm.stock_price, AlfaAtm.fetched_at, AlfaAtm.trade_date)
        .where(AlfaAtm.ticker.in_(symbols))
    )
    out: dict[str, SpotRead] = {}
    with session_factory(engine)() as s:
        for ticker, price, fetched, trade_date in s.execute(stmt).all():
            if price is None or price <= 0:
                continue
            at = as_utc(fetched)
            if ticker not in out or at > out[ticker].fetched_at:
                out[ticker] = SpotRead(spot=float(price), fetched_at=at, trade_date=trade_date)
    return out


def listed_expiries(engine: Engine, tickers: Sequence[str]) -> dict[str, list[date]]:
    symbols = sorted({t.upper() for t in tickers})
    if not symbols:
        return {}
    stmt = select(AlfaAtmExpiry.ticker, AlfaAtmExpiry.expiry).where(AlfaAtmExpiry.ticker.in_(symbols))
    out: dict[str, list[date]] = defaultdict(list)
    with session_factory(engine)() as s:
        for ticker, expiry in s.execute(stmt).all():
            out[ticker].append(expiry)
    return {k: sorted(v) for k, v in out.items()}


def atm_strikes(engine: Engine, tickers: Sequence[str]) -> dict[str, list[float]]:
    """Listed ATM strikes per ticker (one per stored expiry) — known-listed strikes."""
    symbols = sorted({t.upper() for t in tickers})
    if not symbols:
        return {}
    stmt = select(AlfaAtm.ticker, AlfaAtm.strike).where(AlfaAtm.ticker.in_(symbols))
    out: dict[str, list[float]] = defaultdict(list)
    with session_factory(engine)() as s:
        for ticker, strike in s.execute(stmt).all():
            if strike:
                out[ticker].append(float(strike))
    return dict(out)


def _session_date(spot: SpotRead) -> date:
    return spot.trade_date or spot.fetched_at.astimezone(_ET).date()


def price_context(
    ticker: str,
    spot: SpotRead | None,
    bars: Sequence[RegularBar],
    benchmark_move: float | None,
    *,
    atr_period: int,
    atr_min_sessions: int,
) -> PriceContext:
    """Spot against the last completed session before the spot's own session."""
    if spot is None or spot.spot is None:
        return PriceContext(
            ticker=ticker, spot=None, spot_fetched_at=None, prev_close=None, prev_close_day=None,
            atr=None, atr_sessions=0, benchmark_move=benchmark_move, state=CheckState.FAILED,
            blocker="spot yok (alfa_atm boş)",
        )
    session_day = _session_date(spot)
    before = [b for b in bars if b.day < session_day]
    prev = next((b for b in reversed(before) if b.close is not None and b.close > 0), None)
    ranges = true_ranges([Bar(high=b.high, low=b.low, close=b.close) for b in before])
    usable = sum(1 for r in ranges if r is not None)
    atr = wilder_atr(ranges, atr_period) if usable >= atr_min_sessions else None
    missing = []
    if prev is None:
        missing.append("önceki kapanış yok")
    if atr is None:
        missing.append(f"ATR için yeterli seans yok ({usable} < {atr_min_sessions})")
    return PriceContext(
        ticker=ticker,
        spot=spot.spot,
        spot_fetched_at=spot.fetched_at,
        prev_close=prev.close if prev is not None else None,
        prev_close_day=prev.day if prev is not None else None,
        atr=atr,
        atr_sessions=usable,
        benchmark_move=benchmark_move,
        state=CheckState.CHECKED_FOUND if not missing else CheckState.FAILED,
        blocker="; ".join(missing),
    )


def price_contexts(
    engine: Engine,
    tickers: Sequence[str],
    benchmark: str,
    *,
    atr_period: int,
    atr_min_sessions: int,
    max_age_seconds: int,
) -> dict[str, PriceContext]:
    """Price context per ticker; the benchmark move is used only when it is comparable.

    Comparable means: the benchmark spot belongs to the same session as the ticker's
    spot and was fetched within ``max_age_seconds`` of it. A stale or other-day SPY
    move is never set against today's stock move (review 2026-09-24, HIGH 3).
    """
    symbols = sorted({t.upper() for t in tickers} | {benchmark.upper()})
    spots = latest_spots(engine, symbols)
    bars = load_bars_by_ticker(engine, symbols)
    bench_spot = spots.get(benchmark.upper())
    bench = price_context(
        benchmark.upper(), bench_spot, bars.get(benchmark.upper(), ()), None,
        atr_period=atr_period, atr_min_sessions=atr_min_sessions,
    )

    def _bench_move_for(spot: SpotRead | None) -> float | None:
        if spot is None or bench_spot is None or bench.move is None:
            return None
        if _session_date(spot) != _session_date(bench_spot):
            return None
        if abs((spot.fetched_at - bench_spot.fetched_at).total_seconds()) > max_age_seconds:
            return None
        return bench.move

    return {
        t: price_context(
            t, spots.get(t), bars.get(t, ()), _bench_move_for(spots.get(t)),
            atr_period=atr_period, atr_min_sessions=atr_min_sessions,
        )
        for t in symbols
    }
