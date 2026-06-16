"""Self-derived IV-regime (M24) + OI-delta (M27) providers (Phase 3.6.4).

Wrap a ``ChainHistory`` to satisfy the ``IVHistoryProvider`` and
``OpenInterestProvider`` Protocols the fusion stages already consume — the
data comes from the ThetaData chain snapshots, no Unusual Whales. The
M24/M27 stage scoring is unchanged; these only supply the snapshots.

IV rank is ticker-level (current ATM IV vs its trailing range); the
contract's own IV is reported as ``implied_volatility``. iv_percentile and
the intraday-change fields are left None (the daily snapshot has no
intraday resolution) — M24 keys off iv_rank.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from uoa_detector.providers.iv_history import IVRankSnapshot
from uoa_detector.providers.open_interest import OpenInterestSnapshot

if TYPE_CHECKING:
    from datetime import date
    from decimal import Decimal

    from uoa_detector.sources.thetadata_derived.chain_history import ChainHistory

_OptionType = Literal["call", "put"]


class ThetaDataOpenInterestProvider:
    """``OpenInterestProvider`` backed by the ThetaData chain snapshots."""

    def __init__(self, history: ChainHistory) -> None:
        self._h = history

    async def at(
        self, ticker: str, strike: Decimal, expiry: date,
        option_type: _OptionType, when: datetime,
    ) -> OpenInterestSnapshot | None:
        res = self._h.oi_at(ticker, strike, expiry, option_type, when)
        if res is None:
            return None
        _, oi = res
        return OpenInterestSnapshot(
            ticker=ticker, strike=strike, expiry=expiry,
            option_type=option_type, as_of=when, open_interest=oi,
        )

    async def next_day(
        self, ticker: str, strike: Decimal, expiry: date,
        option_type: _OptionType, trade_date: date,
    ) -> OpenInterestSnapshot | None:
        res = self._h.oi_next_day(ticker, strike, expiry, option_type, trade_date)
        if res is None:
            return None
        day, oi = res
        as_of = datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC)
        return OpenInterestSnapshot(
            ticker=ticker, strike=strike, expiry=expiry,
            option_type=option_type, as_of=as_of, open_interest=oi,
        )


class ThetaDataIVHistoryProvider:
    """``IVHistoryProvider`` backed by the ThetaData chain snapshots."""

    def __init__(self, history: ChainHistory) -> None:
        self._h = history

    async def iv_rank_at(
        self, ticker: str, strike: Decimal, expiry: date,
        option_type: _OptionType, at: datetime,
    ) -> IVRankSnapshot | None:
        iv_res = self._h.iv_at(ticker, strike, expiry, option_type, at)
        if iv_res is None:
            return None
        _, iv = iv_res
        rank = self._h.atm_iv_rank(ticker, at)
        return IVRankSnapshot(
            ticker=ticker, strike=strike, expiry=expiry,
            option_type=option_type, as_of=at,
            implied_volatility=iv, iv_rank_252d=rank,
        )
