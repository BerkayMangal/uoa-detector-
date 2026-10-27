"""Availability-aware database adapters. Every research read is bounded by as-of."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.model import NewsItem
from webapp.alpha_lab import db
from webapp.alpha_lab.flow import ObservedAlert, PollSample, timestamp, utc
from webapp.alpha_lab.news import CapturedPrice, NewsAudit, annotate
from webapp.alpha_lab.settings import LabSettings
from webapp.board.atm import AlfaAtm
from webapp.board.daily_close import AlfaDailyBar, AlfaDailyClose
from webapp.board.db import session_factory
from webapp.live_alpha.inputs import SpotRead
from webapp.live_alpha.store import LiveNews, LiveNewsCheck
from webapp.ohlc import RegularBar


def bars_at(
    engine: Engine, tickers: Sequence[str], now: datetime
) -> dict[str, tuple[RegularBar, ...]]:
    if not tickers or not inspect(engine).has_table("alfa_daily_bar"):
        return {}
    result: dict[str, list[RegularBar]] = defaultdict(list)
    with session_factory(engine)() as s:
        for row in s.scalars(
            select(AlfaDailyBar)
            .where(AlfaDailyBar.ticker.in_(tickers), AlfaDailyBar.fetched_at <= now)
            .order_by(AlfaDailyBar.day)
        ):

            def finite(x: float | None) -> float | None:
                return x if x is not None and math.isfinite(x) else None

            result[row.ticker].append(
                RegularBar(
                    row.day, finite(row.open), finite(row.high), finite(row.low), finite(row.close)
                )
            )
    return {t: tuple(rows) for t, rows in result.items()}


def spots_at(engine: Engine, tickers: Sequence[str], now: datetime) -> dict[str, SpotRead]:
    if not tickers or not inspect(engine).has_table("alfa_atm"):
        return {}
    result: dict[str, SpotRead] = {}
    with session_factory(engine)() as s:
        for row in s.scalars(
            select(AlfaAtm)
            .where(AlfaAtm.ticker.in_(tickers), AlfaAtm.fetched_at <= now)
            .order_by(AlfaAtm.fetched_at)
        ):
            if (
                row.stock_price is not None
                and math.isfinite(row.stock_price)
                and row.stock_price > 0
            ):
                result[row.ticker] = SpotRead(row.stock_price, utc(row.fetched_at), row.trade_date)
    return result


def alerts_at(
    engine: Engine, tickers: Sequence[str], now: datetime, cfg: LabSettings
) -> dict[str, list[ObservedAlert]]:
    result: dict[str, list[ObservedAlert]] = defaultdict(list)
    # A recent first_seen revision may refer to an old alert; the flow calculator
    # also filters PROVIDER time, never treating its fetch as a fresh trade.
    with session_factory(engine)() as s:
        ids = list(
            s.scalars(
                select(db.Alert.alert_id)
                .where(
                    db.Alert.ticker.in_(tickers),
                    db.Alert.observed_at <= now,
                    db.Alert.observed_at >= now - timedelta(seconds=cfg.flow_window_seconds),
                )
                .distinct()
            )
        )
        # Include the original version even when only its revision was fetched recently.
        rows = s.scalars(
            select(db.Alert).where(db.Alert.alert_id.in_(ids), db.Alert.observed_at <= now)
        )
        for row in rows:
            result[row.ticker].append(
                ObservedAlert(row.alert_id, utc(row.observed_at), json.loads(row.payload_json))
            )
    return result


def priced_peers(
    engine: Engine, peers: Sequence[str], start_day: date, end_day: date, seen_at: datetime
) -> tuple[str, ...]:
    if not inspect(engine).has_table("alfa_daily_close"):
        return ()
    dates: dict[str, set[date]] = defaultdict(set)
    with session_factory(engine)() as s:
        rows = s.scalars(
            select(AlfaDailyClose).where(
                AlfaDailyClose.ticker.in_(peers),
                AlfaDailyClose.fetched_at <= seen_at,
                AlfaDailyClose.day.in_([start_day, end_day]),
            )
        )
        for row in rows:
            if math.isfinite(row.close) and row.close > 0:
                dates[row.ticker].add(row.day)
    return tuple(p for p in peers if dates[p] == {start_day, end_day})


def polls_at(engine: Engine, now: datetime, cfg: LabSettings) -> dict[str, list[PollSample]]:
    out: dict[str, list[PollSample]] = defaultdict(list)
    with session_factory(engine)() as s:
        rows = s.scalars(
            select(db.Poll).where(
                db.Poll.observed_at <= now,
                db.Poll.observed_at
                >= now - timedelta(seconds=cfg.flow_window_seconds + cfg.poll_max_age_seconds),
            )
        )
        for r in rows:
            payload = json.loads(r.payload_json)
            out[r.ticker].append(
                PollSample(
                    utc(r.observed_at),
                    bool(payload.get("saturated")),
                    timestamp(payload.get("oldest")),
                )
            )
    return out


def news_at(engine: Engine, ticker: str, now: datetime, cfg: LabSettings) -> NewsAudit:
    if not inspect(engine).has_table("alfa_live_news_check") or not inspect(engine).has_table(
        "alfa_live_news"
    ):
        return annotate((), "not_checked", now, cfg.news_window_hours)
    with session_factory(engine)() as s:
        check = s.scalar(
            select(LiveNewsCheck)
            .where(LiveNewsCheck.ticker == ticker, LiveNewsCheck.checked_at <= now)
            .order_by(LiveNewsCheck.checked_at.desc())
            .limit(1)
        )
        if check is None:
            return annotate((), "not_checked", now, cfg.news_window_hours)
        state = check.state
        if (now - utc(check.checked_at)).total_seconds() > cfg.news_max_age_seconds:
            state = "stale"
        refs = json.loads(check.refs_json)
        rows = s.scalars(
            select(LiveNews).where(LiveNews.ref.in_(refs), LiveNews.first_seen_at <= now)
        )
        items = [
            NewsItem(
                r.headline,
                r.source,
                utc(r.provider_created_at),
                utc(r.first_seen_at),
                r.sentiment,
                r.is_major,
                tuple(json.loads(r.tags_json)),
                tuple(json.loads(r.tickers_json)),
                r.ref,
            )
            for r in rows
        ]
    return replace(
        annotate(items, state, now, cfg.news_window_hours),
        checked_at=utc(check.checked_at).isoformat(),
        detail=check.detail,
    )


def price_history(
    engine: Engine, tickers: Sequence[str], now: datetime, cfg: LabSettings
) -> dict[str, list[CapturedPrice]]:
    out: dict[str, list[CapturedPrice]] = defaultdict(list)
    with session_factory(engine)() as s:
        rows = s.scalars(
            select(db.Price).where(
                db.Price.ticker.in_(tickers),
                db.Price.observed_at <= now,
                db.Price.observed_at
                >= now - timedelta(hours=cfg.news_window_hours, seconds=cfg.spot_max_age_seconds),
            )
        )
        for r in rows:
            out[r.ticker].append(
                CapturedPrice(utc(r.observed_at), utc(r.source_at), r.value, r.session_day)
            )
    return out
