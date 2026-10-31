"""SELECT-only adapter. It neither creates schema nor retrieves vendor data."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import Table, inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab import db
from webapp.alpha_lab.flow import timestamp
from webapp.board.db import session_factory
from webapp.event_calibration.features import session as event_session
from webapp.event_calibration.model import (
    Alert,
    Dataset,
    Decision,
    Poll,
    Price,
    Structure,
    digest,
    settings,
    utc,
)
from webapp.live_alpha.store import LiveScan


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def export(
    engine: Engine, asof: datetime, *, since: datetime | None = None, tickers: tuple[str, ...] = ()
) -> Dataset:
    asof = utc(asof)
    names = set(inspect(engine).get_table_names())
    limit = settings().row_limit
    rejected: Counter[str] = Counter()
    alerts: list[Alert] = []
    polls: list[Poll] = []
    prices: dict[tuple[str, datetime], Price] = {}
    decisions: list[Decision] = []
    sessions = set()
    base = load_live_alpha_settings()
    # JSON is untyped only at the immutable storage boundary.
    with session_factory(engine)() as s:
        for model in (db.Alert, db.Poll, db.Price, LiveScan):
            if model.__tablename__ not in names:
                continue
            column = cast(Table, model.__table__).c[
                "finished_at" if model is LiveScan else "observed_at"
            ]
            statement = select(model).where(column <= asof)
            if since is not None:
                statement = statement.where(column >= utc(since))
            if tickers and model is not LiveScan:
                statement = statement.where(
                    cast(Table, model.__table__).c.ticker.in_((*tickers, "SPY"))
                )
            rows = list(s.scalars(statement.order_by(column).limit(limit + 1)))
            if len(rows) > limit:
                raise ValueError(
                    "archive row limit reached; export a bounded date range before calibration"
                )
            for row in rows:
                if isinstance(row, db.Alert):
                    alerts.append(
                        Alert(
                            ticker=row.ticker,
                            key=row.alert_id,
                            known_at=utc(row.observed_at),
                            payload=json.loads(row.payload_json),
                        )
                    )
                elif isinstance(row, db.Poll):
                    p = json.loads(row.payload_json)
                    if not isinstance(p.get("saturated"), bool):
                        rejected["unknown_poll_shape"] += 1
                        continue
                    polls.append(
                        Poll(ticker=row.ticker, at=utc(row.observed_at), saturated=p["saturated"])
                    )
                elif isinstance(row, db.Price):
                    try:
                        point = Price(
                            ticker=row.ticker,
                            at=utc(row.source_at),
                            known_at=utc(row.observed_at),
                            value=row.value,
                        )
                    except ValidationError:
                        rejected["invalid_price"] += 1
                        continue
                    key = point.ticker, point.at
                    if key in prices and prices[key].value != point.value:
                        raise ValueError("conflicting archived source price")
                    if key not in prices or point.known_at < prices[key].known_at:
                        prices[key] = point
                elif isinstance(row, LiveScan):
                    at = utc(row.finished_at)
                    if row.market_mode != "LIVE":
                        continue
                    raw: dict[str, Any] = json.loads(row.snapshot_json)
                    valid_profile = (
                        raw.get("profile_sha256") == base.profile_sha256
                        and raw.get("policy_version") == base.policy_version
                    )
                    source_usable = (
                        valid_profile
                        and utc(row.started_at) <= at
                        and row.status in ("ok", "degraded")
                    )
                    if not source_usable:
                        rejected["incomplete_source"] += 1
                    clock = event_session(at)
                    if clock:
                        sessions.add(clock[0])
                    raw_cards = raw.get("cards", [])
                    seen = {
                        card["ticker"]
                        for card in raw_cards
                        if isinstance(card, dict) and isinstance(card.get("ticker"), str)
                    }
                    missing = [
                        {"ticker": ticker}
                        for ticker in raw.get("universe", [])
                        if isinstance(ticker, str) and ticker not in seen
                    ]
                    for card in [*raw_cards, *missing]:
                        if not isinstance(card, dict) or not isinstance(card.get("ticker"), str):
                            rejected["invalid_card"] += 1
                            continue
                        ticker = card["ticker"]
                        if tickers and ticker not in tickers:
                            continue
                        card_time = timestamp(card.get("decision_as_of"))
                        usable = bool(
                            source_usable
                            and card_time is not None
                            and utc(row.started_at) <= card_time <= at
                        )
                        risk = None
                        plan = card.get("stock_plan")
                        if isinstance(plan, dict):
                            entry, stop = number(plan.get("entry_ref")), number(plan.get("stop"))
                            if entry and stop and 0 < stop < entry:
                                risk = (entry - stop) / entry
                        options = []
                        for option in card.get("options", []):
                            if not isinstance(option, dict) or option.get("kind") not in (
                                "long_call",
                                "bull_call_debit",
                            ):
                                continue
                            try:
                                structure = Structure(
                                    kind=option["kind"],
                                    multiplier=option["multiplier"],
                                    legs=tuple(
                                        {
                                            k: leg[k]
                                            for k in (
                                                "strike",
                                                "expiry",
                                                "right",
                                                "is_long",
                                                "bid",
                                                "ask",
                                                "fetched_at",
                                            )
                                        }
                                        | {"symbol": leg["option_symbol"]}
                                        for leg in option["legs"]
                                    ),
                                )
                                if any(leg.fetched_at > at for leg in structure.legs):
                                    usable = False
                                    rejected["future_quote"] += 1
                                else:
                                    options.append(structure)
                            except (KeyError, TypeError, ValidationError):
                                rejected["unusable_quote_structure"] += 1
                        news = raw.get("news_observations", {}).get(ticker, {})
                        news_at = timestamp(news.get("checked_at"))
                        if news_at is not None and news_at > at:
                            usable = False
                        news_known = (
                            news_at is not None
                            and 0
                            <= (at - news_at).total_seconds()
                            <= load_live_alpha_settings().news.refresh_seconds
                        )
                        try:
                            decisions.append(
                                Decision(
                                    key=digest((row.scan_id, ticker)),
                                    ticker=ticker,
                                    at=at,
                                    usable=usable,
                                    source_reason="" if usable else "incomplete_original_snapshot",
                                    baseline=bool(
                                        usable
                                        and card.get("market_mode") == "LIVE"
                                        and card.get("policy_version") == base.policy_version
                                        and card.get("direction") == "up"
                                        and card.get("recommendation") == "BUY"
                                        and card.get("readiness") == "READY"
                                        and card.get("profile_sha256") == base.profile_sha256
                                    ),
                                    risk_fraction=risk,
                                    news=(news.get("state") == "checked_found")
                                    if news_known
                                    and news.get("state") in ("checked_found", "checked_none")
                                    else None,
                                    options=tuple(options),
                                )
                            )
                        except ValidationError:
                            rejected["invalid_decision"] += 1
    return Dataset(
        origin="captured_sql_archive",
        asof=asof,
        sessions=tuple(sorted(sessions)),
        decisions=tuple(decisions),
        prices=tuple(prices.values()),
        alerts=tuple(alerts),
        polls=tuple(polls),
        provenance={
            "rejected": json.dumps(dict(rejected), sort_keys=True),
            "requested_tickers": ",".join(tickers) if tickers else "all recorded",
            "since": utc(since).isoformat() if since else "archive start",
            "publication_evidence": "recorded scan completion; no post-commit publication proof",
            "option_quality": "B / receipt-dated; no exchange quote time",
            "universe": "as recorded in original scans, not a reconstructed full market",
        },
    )
