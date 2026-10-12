"""Automatic peers: the names that move most like a ticker (Phase 5.27).

Contract: ``docs/phase-5.27-auto-peers-hotlist-acceptance.md`` §3.1 and §3.2.

A hand-written basket (``baskets.py``) says what trades together because the
owner wrote it. This module answers the same question for every other row of
the board from stored daily closes alone: the peers of a ticker are the names
whose daily returns, in excess of the market proxy, correlated best with its own
over the last ``corr_sessions`` sessions. The measured gap is then the 5.26
residual against those peers, computed by ``baskets.basket_lags``.

Two rules keep the comparison honest:

- **Excess returns.** Raw returns make every stock a peer of every other because
  they all move with the market. Subtracting the proxy leaves what the two names
  share beyond that.
- **Days are never bridged.** A daily return exists only when both of its closes
  are stored on consecutive proxy sessions. A hole is skipped, not interpolated.

Everything here is arithmetic over closes already in memory, plus reading one
profile. A peer set computed on a date uses history up to that date, so it is
replay-safe; the record of 5.27 §3.7 stores the set that was used.
"""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML
from sqlalchemy import select

from uoa_detector.live_alpha.settings import profile_path
from webapp.board.baskets import Basket, BasketLag, BasketSettings, basket_lags
from webapp.board.daily_close import AlfaDailyClose, ClosePoint
from webapp.board.db import session_factory

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date

    from sqlalchemy.engine import Engine

PEERS_PROFILE_NAME = "peers_v1.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PeerSettings(_Strict):
    """``profiles/peers_v1.yaml``: how automatic peers are chosen (§3.1)."""

    peers_version: str = Field(min_length=1)
    corr_sessions: int = Field(ge=20)
    min_overlap: int = Field(ge=10)
    peer_count: int = Field(ge=3)
    min_corr: float = Field(gt=0, lt=1)
    min_peers: int = Field(ge=3)
    market_proxy: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> PeerSettings:
        if self.min_overlap > self.corr_sessions:
            msg = "min_overlap cannot exceed corr_sessions"
            raise ValueError(msg)
        if self.min_peers > self.peer_count:
            msg = "min_peers cannot exceed peer_count"
            raise ValueError(msg)
        if self.market_proxy != self.market_proxy.strip().upper():
            return self.model_copy(update={"market_proxy": self.market_proxy.strip().upper()})
        return self


def load_peer_settings(path: pathlib.Path | None = None) -> PeerSettings:
    """Load and validate the peers profile. Raises on a missing file or invalid content."""
    target = path if path is not None else profile_path(PEERS_PROFILE_NAME)
    raw = YAML(typ="safe").load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"peers profile {target} must be a mapping"
        raise ValueError(msg)
    return PeerSettings.model_validate(raw)


def _daily_returns(points: Iterable[ClosePoint], sessions: Sequence[date]) -> dict[date, float]:
    """Return on each session whose own close and previous-session close are both stored."""
    by_day = {point.day: point.close for point in points if point.close > 0}
    out: dict[date, float] = {}
    for previous, day in pairwise(sessions):
        first, last = by_day.get(previous), by_day.get(day)
        if first is not None and last is not None:
            out[day] = last / first - 1.0
    return out


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Correlation of two equal-length series, None when it is undefined (no spread)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def excess_returns(
    closes: Mapping[str, Sequence[ClosePoint]], cfg: PeerSettings,
) -> dict[str, dict[date, float]]:
    """Every name's daily return in excess of the proxy, over the proxy's last sessions.

    Empty when the proxy has too little stored history to define the sessions:
    without it there is no market to subtract and raw-return peers are refused
    (§3.1), so every ticker will answer ``no_peers`` rather than a weaker number.
    """
    proxy_days = sorted({p.day for p in closes.get(cfg.market_proxy, ()) if p.close > 0})
    sessions = proxy_days[-(cfg.corr_sessions + 1):]
    if len(sessions) < cfg.min_overlap + 1:
        return {}
    market = _daily_returns(closes[cfg.market_proxy], sessions)
    out: dict[str, dict[date, float]] = {}
    for ticker, points in closes.items():
        if ticker == cfg.market_proxy:
            continue
        own = _daily_returns(points, sessions)
        out[ticker] = {day: ret - market[day] for day, ret in own.items() if day in market}
    return out


def select_peers(
    ticker: str, excess: Mapping[str, Mapping[date, float]], cfg: PeerSettings,
) -> tuple[str, ...]:
    """The ticker's peers, best first, or ``()`` when fewer than ``min_peers`` qualify (§3.1)."""
    mine = excess.get(ticker)
    if not mine or len(mine) < cfg.min_overlap:
        return ()
    scored: list[tuple[float, str]] = []
    for candidate, theirs in excess.items():
        if candidate == ticker:
            continue
        shared = [day for day in mine if day in theirs]
        if len(shared) < cfg.min_overlap:
            continue
        corr = _pearson([mine[d] for d in shared], [theirs[d] for d in shared])
        if corr is not None and corr >= cfg.min_corr:
            scored.append((corr, candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked = tuple(name for _, name in scored[: cfg.peer_count])
    return picked if len(picked) >= cfg.min_peers else ()


def _no_peers(cfg: BasketSettings) -> BasketLag:
    return BasketLag(
        basket_id="auto", basket_name="otomatik akran", state="no_peers",
        start=None, end=None, window_sessions=cfg.window_sessions,
        members_priced=0, members_total=0, source="auto",
    )


def auto_lags(
    tickers: Iterable[str],
    closes: Mapping[str, Sequence[ClosePoint]],
    peer_cfg: PeerSettings,
    basket_cfg: BasketSettings,
) -> dict[str, BasketLag]:
    """One reading per ticker against its automatic peers. Pure; no I/O.

    A ticker the closes cannot place (no history, no proxy, too few similar
    names) answers ``no_peers`` and carries no number. The residual itself is
    ``baskets.basket_lags`` over the ticker and its peers, so the arithmetic, the
    shared anchors and the three no-number branches are the 5.26 ones.
    """
    excess = excess_returns(closes, peer_cfg)
    out: dict[str, BasketLag] = {}
    for raw in tickers:
        ticker = raw.strip().upper()
        if not ticker or ticker in out:
            continue
        peers = select_peers(ticker, excess, peer_cfg)
        if not peers:
            out[ticker] = _no_peers(basket_cfg)
            continue
        group = Basket(id=f"auto-{ticker}", name="otomatik akran", members=(ticker, *peers))
        reading = basket_lags(group, closes, basket_cfg).get(ticker)
        if reading is None:
            out[ticker] = _no_peers(basket_cfg)
            continue
        out[ticker] = replace(reading, basket_id="auto", source="auto", peers=peers)
    return out


def load_universe_closes(engine: Engine, *, since: date) -> dict[str, tuple[ClosePoint, ...]]:
    """Every stored close on or after ``since``, grouped by ticker, oldest first. Read-only.

    One query: the universe is the closes table itself, so a name the daily-close
    job has ever stored can be somebody's peer (§3.3).
    """
    stmt = (
        select(AlfaDailyClose.ticker, AlfaDailyClose.day, AlfaDailyClose.close)
        .where(AlfaDailyClose.day >= since)
        .order_by(AlfaDailyClose.ticker, AlfaDailyClose.day)
    )
    grouped: dict[str, list[ClosePoint]] = {}
    with session_factory(engine)() as session:
        for ticker, day, close in session.execute(stmt).all():
            grouped.setdefault(ticker, []).append(ClosePoint(day=day, close=close))
    return {ticker: tuple(points) for ticker, points in grouped.items()}
