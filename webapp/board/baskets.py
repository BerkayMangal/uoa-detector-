"""Peer baskets and the basket-relative lag (Phase 5.26).

Contract: ``docs/phase-5.26-basket-relative-acceptance.md``.

One measured number per ticker: its return minus its peers' return over the
same two sessions. Nothing here predicts anything, scores anything or decides
anything — ``webapp/live_alpha/board.py`` turns the number into one column of
the "Hisse panosu" and that is its only consumer.

Why a hand-written basket rather than the vendor's sector (contract §2): the
vendor answers one of eleven coarse buckets and ranks its peers by market cap,
so COHR's "peers" are the largest technology names; and that membership is
current-as-of-fetch, which the provider itself refuses for replay
(``sources/unusual_whales/providers/sector_peer.py:85-88``). A basket committed
to git carries the date it was written.

Two rules make the number a comparison rather than a coincidence:

- **Self is excluded from the peer mean** (§3.2). Including it dilutes the
  name's own deviation by ``1/n``. Ranking is the same either way, so this sets
  only the magnitude, and the self-excluded magnitude is the one that answers
  "this name against its peers".
- **Both anchor dates are shared** (§3.3). A member is priced on *exactly* the
  two anchors or it is dropped from the mean and counted as dropped. "On or
  before" would measure different windows for different members and present
  the result as a comparison.

This module loads a profile and does arithmetic. It performs no I/O beyond
reading that profile once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

from uoa_detector.live_alpha.settings import profile_path

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date

    from webapp.board.daily_close import ClosePoint

BASKET_PROFILE_NAME = "baskets_v1.yaml"

_logger = logging.getLogger(__name__)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Basket(_Strict):
    """One hand-written peer group. ``members`` is upper-cased on load."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    members: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _normalise(self) -> Basket:
        upper = tuple(member.strip().upper() for member in self.members)
        if any(not member for member in upper):
            msg = f"basket {self.id}: a member must not be blank"
            raise ValueError(msg)
        if len(set(upper)) != len(upper):
            msg = f"basket {self.id}: a member must not repeat"
            raise ValueError(msg)
        if upper != self.members:
            return self.model_copy(update={"members": upper})
        return self


class BasketSettings(_Strict):
    """``profiles/baskets_v1.yaml``: the baskets plus the display cutoffs (§3.5)."""

    basket_version: str = Field(min_length=1)
    window_sessions: int = Field(ge=1)
    min_members_with_data: int = Field(ge=3)
    lag_flag_pct: float = Field(gt=0)
    min_basket_move_pct: float = Field(ge=0)
    baskets: tuple[Basket, ...] = Field(default=())

    @model_validator(mode="after")
    def _distinct_ids(self) -> BasketSettings:
        ids = [basket.id for basket in self.baskets]
        if len(set(ids)) != len(ids):
            msg = "baskets must not repeat an id"
            raise ValueError(msg)
        return self

    def basket_of(self, ticker: str) -> Basket | None:
        """The first basket holding ``ticker``, or None.

        A name in two baskets is answered with the first, in file order, so the
        column is deterministic. The file is hand-written and small enough that
        the owner can see the overlap and decide.
        """
        symbol = ticker.strip().upper()
        for basket in self.baskets:
            if symbol in basket.members:
                return basket
        return None

    def members(self) -> tuple[str, ...]:
        """Every basket member, de-duplicated, in file order (§3.6)."""
        out: list[str] = []
        for basket in self.baskets:
            for member in basket.members:
                if member not in out:
                    out.append(member)
        return tuple(out)


def load_basket_settings(path: pathlib.Path | None = None) -> BasketSettings:
    """Load and validate the basket profile. Raises on a missing file or invalid content."""
    target = path if path is not None else profile_path(BASKET_PROFILE_NAME)
    raw = YAML(typ="safe").load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"basket profile {target} must be a mapping"
        raise ValueError(msg)
    return BasketSettings.model_validate(raw)


def basket_close_tickers() -> tuple[str, ...]:
    """Every basket member, for the daily-close job (§3.6). Never raises.

    A profile that cannot be read costs the basket members their closes for this
    tick and nothing else: the daily jobs are shared with the outcome and delayed
    work, and a hand-edited file must not be able to stop those. The same file is
    validated at startup, so this is the belt behind the braces, not the check.
    """
    try:
        return load_basket_settings().members()
    except Exception:
        _logger.warning("basket profile unreadable; basket members get no daily close", exc_info=True)
        return ()


# ---------------------------------------------------------------------------
# The residual
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BasketLag:
    """One ticker's measured position inside its basket over two shared sessions.

    ``residual_pct`` is None exactly when ``state`` is not ``"measured"``; the
    state says which of the contract's §3.4 branches answered.
    """

    basket_id: str
    basket_name: str
    state: str  # measured | thin_data | flat_basket
    start: date | None
    end: date | None
    window_sessions: int  # sessions actually spanned by the two anchors
    members_priced: int  # members with a close on BOTH anchors, self included
    members_total: int
    residual_pct: float | None = None
    peers_pct: float | None = None  # the peer mean excluding self
    own_pct: float | None = None
    rank: int | None = None  # 1 = strongest priced member of the basket
    lagging: bool = False
    leading: bool = False


def anchor_dates(
    closes: Mapping[str, Sequence[ClosePoint]],
    members: Iterable[str],
    window_sessions: int,
) -> tuple[date, date] | None:
    """The two shared anchor dates for a basket, or None when the history is too short.

    ``end`` is the newest stored session across the members. ``start`` is
    ``window_sessions`` stored sessions before it, counted in the union of the
    members' date sets — a union, not one member's series, so a name added to
    the basket last week does not shorten the window for the rest (§3.3).
    """
    if window_sessions < 1:
        return None
    days: set[date] = set()
    for member in members:
        for point in closes.get(member, ()):
            if point.close > 0:
                days.add(point.day)
    ordered = sorted(days)
    if len(ordered) <= window_sessions:
        return None
    return ordered[-1 - window_sessions], ordered[-1]


def _close_on(points: Sequence[ClosePoint], day: date) -> float | None:
    """The close stored for exactly ``day``, or None. Not "on or before" (§3.3)."""
    for point in points:
        if point.day == day and point.close > 0:
            return point.close
    return None


def basket_lags(
    basket: Basket,
    closes: Mapping[str, Sequence[ClosePoint]],
    cfg: BasketSettings,
) -> dict[str, BasketLag]:
    """Every member's state, keyed by ticker. Pure; no I/O.

    A member is never answered with a zero residual when its data is missing:
    an unpriced member carries ``thin_data`` whatever the rest of the basket
    did, and every row carries ``members_priced`` against ``members_total`` so
    thin coverage is visible on the member that still has a number.

    Empty only when the basket has no two anchor dates at all.
    """
    total = len(basket.members)
    anchors = anchor_dates(closes, basket.members, cfg.window_sessions)
    if anchors is None:
        return {}
    start, end = anchors
    returns: dict[str, float] = {}
    for member in basket.members:
        points = closes.get(member, ())
        first, last = _close_on(points, start), _close_on(points, end)
        if first is not None and last is not None:
            returns[member] = (last / first - 1.0) * 100.0
    priced = len(returns)

    def shell(state: str) -> BasketLag:
        return BasketLag(
            basket_id=basket.id, basket_name=basket.name, state=state,
            start=start, end=end, window_sessions=cfg.window_sessions,
            members_priced=priced, members_total=total,
        )

    # A member the anchors did not price has nothing to compare, whatever the
    # rest of the basket did; it says so for itself rather than borrowing the
    # group's state.
    out: dict[str, BasketLag] = {
        member: shell("thin_data") for member in basket.members if member not in returns
    }

    if priced < cfg.min_members_with_data:
        return {member: shell("thin_data") for member in basket.members}

    # 1 = strongest. Ties share the better rank, and the tie is broken by ticker
    # so the column is stable between renders rather than following dict order.
    ordered = sorted(returns.items(), key=lambda item: (-item[1], item[0]))
    ranks = {ticker: position for position, (ticker, _) in enumerate(ordered, start=1)}

    total_return = sum(returns.values())
    for member, own in returns.items():
        peers = (total_return - own) / (priced - 1)
        # A name cannot lag peers that did not move (contract 5.26 §3.4). The test is on the
        # peers, self excluded: judging the whole group lets the laggard's own fall drag the
        # mean under the cutoff and hide the biggest gaps (fixed in 5.27.2).
        if abs(peers) < cfg.min_basket_move_pct:
            out[member] = shell("flat_basket")
            continue
        residual = own - peers
        out[member] = BasketLag(
            basket_id=basket.id, basket_name=basket.name, state="measured",
            start=start, end=end, window_sessions=cfg.window_sessions,
            members_priced=priced, members_total=total,
            residual_pct=residual, peers_pct=peers, own_pct=own,
            rank=ranks[member],
            lagging=residual <= -cfg.lag_flag_pct,
            leading=residual >= cfg.lag_flag_pct,
        )
    return out


def lags_by_ticker(
    closes: Mapping[str, Sequence[ClosePoint]],
    cfg: BasketSettings,
) -> dict[str, BasketLag]:
    """Every basket's lags merged into one mapping, first basket wins a shared name.

    Matches ``BasketSettings.basket_of``, so the column and the number always
    name the same basket for a ticker that sits in two.
    """
    out: dict[str, BasketLag] = {}
    for basket in cfg.baskets:
        for ticker, lag in basket_lags(basket, closes, cfg).items():
            out.setdefault(ticker, lag)
    return out
