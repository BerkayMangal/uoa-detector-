"""The lag record (Phase 5.27, contract §3.7 and §3.8): what the board measured, kept so it can be scored later.

``alfa_lag_record`` is append-only. A row holds one ``measured`` reading of the board: the
group it was measured against (a hand basket or automatic peers, the peers written down), the
two anchor sessions, the three returns, the flags, and the profile versions. Nothing reads the
table to decide anything; it exists so that, after the fact, flagged rows can be compared with
all measured rows. Selection bias is only measurable if both were stored.

Scoring (§3.8) is a pure function over stored closes. It reports a number or ``None``, and never
a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, cast

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.baskets import BasketLag, BasketSettings
    from webapp.board.daily_close import ClosePoint

# Not tunables: the horizons the contract names for scoring a record (§3.8).
FORWARD_SESSIONS: Final = (1, 3, 5)

_PEER_SEPARATOR: Final = ","


class AlfaLagRecord(AlfaBase):
    """One measured reading, never updated or deleted."""

    __tablename__ = "alfa_lag_record"

    day: Mapped[date] = mapped_column(Date, primary_key=True)  # the ET date of the reading
    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String)  # hand | auto
    basket_id: Mapped[str] = mapped_column(String)  # the basket id, or "auto"
    peers: Mapped[str] = mapped_column(String)  # comma separated, the names measured against
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    window_sessions: Mapped[int] = mapped_column(Integer)
    own_pct: Mapped[float] = mapped_column(Float)
    peers_pct: Mapped[float] = mapped_column(Float)
    residual_pct: Mapped[float] = mapped_column(Float)
    lagging: Mapped[bool] = mapped_column(Boolean)
    leading: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[str] = mapped_column(String)  # "<basket_version>+<peers_version>"
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_lag_record_tables(engine: Engine) -> None:
    """Create ``alfa_lag_record`` when missing. Never alters or drops anything."""
    cast(Table, AlfaLagRecord.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class LagRecord:
    day: date
    ticker: str
    source: str
    basket_id: str
    peers: tuple[str, ...]
    start: date
    end: date
    window_sessions: int
    own_pct: float
    peers_pct: float
    residual_pct: float
    lagging: bool
    leading: bool
    version: str


def records_from(
    day: date, lags: Mapping[str, BasketLag], baskets: BasketSettings, version: str,
) -> list[LagRecord]:
    """The ``measured`` readings of one cycle as records, in ticker order. Pure.

    A hand reading is measured against the other members of its basket; the basket is looked up
    in the profile, because the reading itself does not name them. Every other state (thin data,
    a flat group, no peers) says nothing about a gap and is not recorded.
    """
    out: list[LagRecord] = []
    for ticker, lag in sorted(lags.items()):
        if (
            lag.state != "measured" or lag.start is None or lag.end is None
            or lag.residual_pct is None or lag.own_pct is None or lag.peers_pct is None
        ):
            continue
        if lag.source == "auto":
            basket_id, peers = "auto", lag.peers
        else:
            basket = baskets.basket_of(ticker)
            if basket is None:
                continue
            basket_id, peers = basket.id, tuple(m for m in basket.members if m != ticker)
        out.append(LagRecord(
            day=day, ticker=ticker, source=lag.source, basket_id=basket_id, peers=peers,
            start=lag.start, end=lag.end, window_sessions=lag.window_sessions,
            own_pct=lag.own_pct, peers_pct=lag.peers_pct, residual_pct=lag.residual_pct,
            lagging=lag.lagging, leading=lag.leading, version=version,
        ))
    return out


def append_records(engine: Engine, records: Sequence[LagRecord], *, now: datetime) -> int:
    """Store the records that are new, return how many were written.

    A ticker is written at most once per ET date, and never again for an ``end`` anchor it already
    has a row for: a weekend or a holiday reads the same stored closes as the session before, and
    a second row for the same observation would count it twice when the record is scored. Stored
    rows are never touched.
    """
    if not records:
        return 0
    ensure_lag_record_tables(engine)
    tickers = sorted({r.ticker for r in records})
    with session_factory(engine)() as session:
        seen = {
            (row.day, row.ticker) for row in session.execute(
                select(AlfaLagRecord.day, AlfaLagRecord.ticker).where(AlfaLagRecord.ticker.in_(tickers)),
            )
        }
        seen_ends = {
            (row.ticker, row.end) for row in session.execute(
                select(AlfaLagRecord.ticker, AlfaLagRecord.end).where(AlfaLagRecord.ticker.in_(tickers)),
            )
        }
        written = 0
        for r in records:
            if (r.day, r.ticker) in seen or (r.ticker, r.end) in seen_ends:
                continue
            session.add(AlfaLagRecord(
                day=r.day, ticker=r.ticker, source=r.source, basket_id=r.basket_id,
                peers=_PEER_SEPARATOR.join(r.peers), start=r.start, end=r.end,
                window_sessions=r.window_sessions, own_pct=r.own_pct, peers_pct=r.peers_pct,
                residual_pct=r.residual_pct, lagging=r.lagging, leading=r.leading,
                version=r.version, recorded_at=now.astimezone(UTC),
            ))
            seen.add((r.day, r.ticker))
            seen_ends.add((r.ticker, r.end))
            written += 1
        try:
            session.commit()
        except IntegrityError:
            session.rollback()  # another writer stored the same key first; its row stands
            return 0
    return written


def load_records(engine: Engine, *, since: date | None = None) -> list[LagRecord]:
    """Stored records, oldest first, optionally from ``since`` on."""
    stmt = select(AlfaLagRecord).order_by(AlfaLagRecord.day, AlfaLagRecord.ticker)
    if since is not None:
        stmt = stmt.where(AlfaLagRecord.day >= since)
    with session_factory(engine)() as session:
        rows = session.execute(stmt).scalars().all()
        return [
            LagRecord(
                day=row.day, ticker=row.ticker, source=row.source, basket_id=row.basket_id,
                peers=tuple(p for p in row.peers.split(_PEER_SEPARATOR) if p), start=row.start,
                end=row.end, window_sessions=row.window_sessions, own_pct=row.own_pct,
                peers_pct=row.peers_pct, residual_pct=row.residual_pct, lagging=row.lagging,
                leading=row.leading, version=row.version,
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _close_on(points: Sequence[ClosePoint], day: date) -> float | None:
    """The close stored for exactly ``day``, or None. Not "on or before"."""
    for point in points:
        if point.day == day and point.close > 0:
            return point.close
    return None


def forward_result(
    record: LagRecord, closes: Mapping[str, Sequence[ClosePoint]], sessions: int,
) -> float | None:
    """Own return minus the same peers' mean return, from the record's ``end`` to ``sessions`` later (§3.8).

    Pure and read-only. In percent, like the residual it follows up. ``None`` while the stored
    closes do not reach ``sessions`` sessions past ``end``, and whenever the comparison cannot be
    made on exactly the names the reading used:

    - sessions are counted on the union of the dates stored for the name and its peers, the way
      the anchors were;
    - the peers are those the reading could price, meaning a close on both of its anchors;
    - the name and every one of those peers need a close on the target session. A peer with a
      hole there makes the result ``None`` instead of quietly shrinking the group.
    """
    if sessions < 1:
        return None
    group = (record.ticker, *record.peers)
    days = {p.day for name in group for p in closes.get(name, ()) if p.close > 0 and p.day > record.end}
    later = sorted(days)
    if len(later) < sessions:
        return None
    target = later[sessions - 1]

    own_end = _close_on(closes.get(record.ticker, ()), record.end)
    own_target = _close_on(closes.get(record.ticker, ()), target)
    if own_end is None or own_target is None:
        return None
    peer_returns: list[float] = []
    for peer in record.peers:
        points = closes.get(peer, ())
        first = _close_on(points, record.end)
        if first is None or _close_on(points, record.start) is None:
            continue  # not priced by the reading, so not part of its group
        last = _close_on(points, target)
        if last is None:
            return None
        peer_returns.append((last / first - 1.0) * 100.0)
    if not peer_returns:
        return None
    return (own_target / own_end - 1.0) * 100.0 - sum(peer_returns) / len(peer_returns)


def forward_results(
    record: LagRecord, closes: Mapping[str, Sequence[ClosePoint]],
) -> dict[int, float | None]:
    """``forward_result`` at the 1, 3 and 5 session horizons the contract names."""
    return {n: forward_result(record, closes, n) for n in FORWARD_SESSIONS}
