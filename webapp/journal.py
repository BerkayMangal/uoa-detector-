"""Trade journal — the forward edge-measurement instrument (Phase 4.7).

The backtest proved the raw signal has no mechanical edge. The only open
question is whether Berkay's *discretionary* read has edge. This journal is
how we would ever know — and it is built with the same anti-self-deception
discipline as the backtest:

  * **Market-neutral.** A trade can be "up" only because the market went up
    (the drift illusion we caught). So the skill metric is the underlying's
    return in the thesis direction, MINUS the market's (SPY) return over the
    same window. Positive = the name moved your way beyond the market.
  * **Significance-gated.** No verdict before N>=10 trades, and "edge" is only
    claimed when the t-stat clears |t|>2. Small samples stay "no conclusion".
  * **Logs winners and losers.** Every trade, so selection isn't cherry-picked.

The actual option P&L (the money, with theta/IV noise) is tracked and shown
alongside the clean directional signal — a thesis can be right while the
option bleeds, or wrong while a gamma pop pays. Both truths are reported.
"""

from __future__ import annotations

import math
import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Float, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from webapp.repo import _normalize_url

if TYPE_CHECKING:
    from collections.abc import Sequence

_MIN_SAMPLE = 10  # no verdict below this many closed trades


class _Base(DeclarativeBase):
    pass


class TradeRow(_Base):
    """One logged trade. Metrics are computed on read, never stored stale."""

    __tablename__ = "trade"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column()
    entry_ts: Mapped[datetime] = mapped_column()
    ticker: Mapped[str] = mapped_column(String, index=True)
    direction: Mapped[str] = mapped_column(String)  # bullish | bearish
    instrument: Mapped[str] = mapped_column(String)  # call | put | shares
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiry: Mapped[str | None] = mapped_column(String, nullable=True)
    contracts: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)  # premium/share at entry
    entry_underlying_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_spy_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Screener context this trade came from (nullable — manual trades allowed).
    signal_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_label: Mapped[str | None] = mapped_column(String, nullable=True)
    thesis: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="open", index=True)
    exit_ts: Mapped[datetime | None] = mapped_column(nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_underlying_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_spy_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)


# --------------------------------------------------------------------------
# Metrics — pure functions, unit-tested. This is where we avoid fooling
# ourselves, so it lives apart from the DB and the UI.
# --------------------------------------------------------------------------


def _multiplier(instrument: str) -> float:
    return 1.0 if instrument == "shares" else 100.0


def option_pnl_usd(t: TradeRow) -> float | None:
    """Realised P&L in dollars (long premium / long shares)."""
    if t.exit_price is None:
        return None
    return (t.exit_price - t.entry_price) * t.contracts * _multiplier(t.instrument)


def option_return_pct(t: TradeRow) -> float | None:
    if t.exit_price is None or t.entry_price == 0:
        return None
    return (t.exit_price - t.entry_price) / t.entry_price


def directional_excess(t: TradeRow) -> float | None:
    """Market-neutral excess return of the underlying, in the thesis direction.

    ``dir * ((U_exit/U_entry - 1) - (SPY_exit/SPY_entry - 1))``. Beta is taken
    as 1 (simple market-neutral) — the same control the backtest used. Returns
    None unless all four prices are present.
    """
    px = (t.entry_underlying_px, t.exit_underlying_px, t.entry_spy_px, t.exit_spy_px)
    if any(p is None or p == 0 for p in px):
        return None
    u_entry, u_exit, s_entry, s_exit = px  # type: ignore[misc]
    r_underlying = u_exit / u_entry - 1.0
    r_market = s_exit / s_entry - 1.0
    sign = 1.0 if t.direction == "bullish" else -1.0
    return sign * (r_underlying - r_market)


@dataclass(frozen=True)
class EdgeStats:
    n_closed: int
    # Directional skill (market-neutral excess return)
    excess_n: int
    excess_mean: float | None
    excess_t: float | None
    # Actual option money
    pnl_total: float
    pnl_win_rate: float | None
    pnl_profit_factor: float | None
    verdict_tier: str  # building | none | edge | negative
    verdict: str


def _t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    if sd == 0:
        return None
    return statistics.mean(values) / (sd / math.sqrt(len(values)))


def aggregate(trades: Sequence[TradeRow]) -> EdgeStats:
    closed = [t for t in trades if t.status == "closed"]
    excess = [e for t in closed if (e := directional_excess(t)) is not None]
    pnls = [p for t in closed if (p := option_pnl_usd(t)) is not None]

    excess_mean = statistics.mean(excess) if excess else None
    excess_t = _t_stat(excess)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = (len(wins) / len(pnls)) if pnls else None
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    tier, verdict = _verdict(len(closed), len(excess), excess_mean, excess_t)
    return EdgeStats(
        n_closed=len(closed),
        excess_n=len(excess),
        excess_mean=excess_mean,
        excess_t=excess_t,
        pnl_total=sum(pnls),
        pnl_win_rate=win_rate,
        pnl_profit_factor=profit_factor,
        verdict_tier=tier,
        verdict=verdict,
    )


def _verdict(
    n_closed: int, excess_n: int, mean: float | None, t: float | None,
) -> tuple[str, str]:
    if excess_n < _MIN_SAMPLE or mean is None or t is None:
        return "building", (
            f"Building sample ({excess_n}/{_MIN_SAMPLE} trades with price data). "
            "No conclusion yet — keep logging every trade, winners and losers. "
            "Edge can't be read off a handful of trades."
        )
    if abs(t) < 2.0:
        return "none", (
            f"No edge detected yet. Your picks' market-neutral excess is "
            f"{mean:+.2%} per trade (t={t:.2f}, not significant) — not beating "
            "the market beyond noise. Keep logging; the picture may sharpen."
        )
    if t >= 2.0 and mean > 0:
        return "edge", (
            f"Possible edge. Your names move your way {mean:+.2%} beyond the "
            f"market per trade (t={t:.2f}, significant). Promising — keep "
            "validating and watch for regime change before sizing up."
        )
    return "negative", (
        f"Warning: negative skill. Your picks underperform the market by "
        f"{mean:.2%} per trade (t={t:.2f}). The selection is costing you "
        "versus simply holding SPY. Re-examine the read before risking more."
    )


# --------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------


class JournalRepo:
    def __init__(self, database_url: str | None = None) -> None:
        import os
        url = _normalize_url(
            database_url or os.environ.get("DATABASE_URL", "sqlite:///webapp/seed.db"),
        )
        self._engine = create_engine(url, future=True)
        _Base.metadata.create_all(self._engine)
        self._session = sessionmaker(self._engine, future=True)

    def add(self, **fields: object) -> str:
        trade_id = uuid.uuid4().hex
        row = TradeRow(
            id=trade_id,
            created_at=datetime.now(UTC),
            status="open",
            **fields,  # type: ignore[arg-type]
        )
        with self._session() as s:
            s.add(row)
            s.commit()
        return trade_id

    def get(self, trade_id: str) -> TradeRow | None:
        with self._session() as s:
            return s.get(TradeRow, trade_id)

    def list(self, status: str | None = None) -> list[TradeRow]:
        stmt = select(TradeRow).order_by(TradeRow.created_at.desc())
        if status:
            stmt = stmt.where(TradeRow.status == status)
        with self._session() as s:
            return list(s.execute(stmt).scalars().all())

    def close(self, trade_id: str, **fields: object) -> bool:
        with self._session() as s:
            row = s.get(TradeRow, trade_id)
            if row is None or row.status == "closed":
                return False
            for key, value in fields.items():
                setattr(row, key, value)
            row.status = "closed"
            s.commit()
            return True
