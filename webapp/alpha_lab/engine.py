"""Point-in-time pilot observations; a separate path from live recommendations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Table, func, inspect, select
from sqlalchemy.engine import Engine

from uoa_detector.live_alpha.calendar import MarketMode, classify, is_session_day
from uoa_detector.live_alpha.settings import (
    CalendarSettings,
    load_live_alpha_settings,
    profile_path,
)
from webapp.alpha_lab import db, readers
from webapp.alpha_lab.flow import coverage, measure, utc
from webapp.alpha_lab.news import with_context
from webapp.alpha_lab.research import following_sessions, outcome
from webapp.alpha_lab.settings import LabSettings, load_settings
from webapp.board.baskets import load_basket_settings
from webapp.board.db import session_factory
from webapp.board.lag_record import AlfaLagRecord
from webapp.live_alpha.inputs import price_context
from webapp.live_alpha.store import ScanRow
from webapp.ohlc import RegularBar

STATE: dict[str, object] = {}


def _contiguous(
    bars: Sequence[RegularBar], today: datetime, cal: CalendarSettings
) -> tuple[RegularBar, ...]:
    by_day = {b.day: b for b in bars}
    day = today.date()
    out: list[RegularBar] = []
    while True:
        day -= timedelta(days=1)
        valid = is_session_day(day, cal)
        if valid is None:
            break
        if not valid:
            continue
        bar = by_day.get(day)
        if bar is None:
            break
        out.append(bar)
    return tuple(reversed(out))


def _version(cfg: LabSettings) -> str:
    return db.digest(
        (
            cfg.profile_hash,
            profile_path("baskets_v1.yaml").read_text(),
            profile_path("peers_v1.yaml").read_text(),
            profile_path("live_alpha_v1.yaml").read_text(),
        )
    )


def observe(
    engine: Engine, scan: ScanRow, now: datetime, cfg: LabSettings | None = None
) -> dict[str, object]:
    cfg = cfg or load_settings()
    now = utc(now)
    live = load_live_alpha_settings()
    current = classify(now, live.calendar)
    today = current.et_now.date()
    db.ensure(engine)
    version = _version(cfg)
    polls = readers.polls_at(engine, now, cfg)
    # The original lag record keeps membership/anchors and its availability time.
    with session_factory(engine)() as s:
        lag_rows = (
            list(
                s.scalars(
                    select(AlfaLagRecord).where(
                        AlfaLagRecord.day == today, AlfaLagRecord.recorded_at <= now
                    )
                )
            )
            if inspect(engine).has_table("alfa_lag_record")
            else []
        )
        lags = {r.ticker: r for r in lag_rows}
    tickers = sorted(set(polls) | set(lags) | set(scan.snapshot.get("universe", [])))
    observations = readers.alerts_at(engine, tickers, now, cfg)
    prices = readers.spots_at(engine, [*tickers, cfg.benchmark], now)
    bars = readers.bars_at(engine, [*tickers, cfg.benchmark], now)
    basket = load_basket_settings()
    history = readers.price_history(engine, tickers, now, cfg)
    audits: list[dict[str, Any]] = []
    cohorts: list[dict[str, Any]] = []
    eligible = treated = 0
    for ticker in tickers:
        reasons: list[str] = []
        covered, capture_reason = coverage(polls.get(ticker, ()), now, cfg)
        flow = measure(observations.get(ticker, ()), now, cfg)
        news = readers.news_at(engine, ticker, now, cfg)
        spot = prices.get(ticker)
        news = with_context(
            news, observations.get(ticker, ()), history.get(ticker, ()), spot, now, cfg
        )
        price = price_context(
            ticker,
            spot,
            _contiguous(bars.get(ticker, ()), current.et_now, live.calendar),
            None,
            atr_period=cfg.atr_period,
            atr_min_sessions=cfg.atr_min_sessions,
        )
        if current.mode is not MarketMode.LIVE:
            reasons.append("not_live_session")
        if not covered:
            reasons.append(capture_reason)
        if not flow.clean_control:
            reasons.append("ambiguous_flow")
        if (
            spot is None
            or spot.trade_date != today
            or not (0 <= (now - spot.fetched_at).total_seconds() <= cfg.spot_max_age_seconds)
        ):
            reasons.append("spot_unavailable_or_stale")
        if price.move_atr is None or not (
            cfg.recovery_min_atr <= price.move_atr <= cfg.chase_max_atr
        ):
            reasons.append("no_recovery_or_chasing")
        lag = lags.get(ticker)
        peers: tuple[str, ...] = ()
        if (
            lag is None
            or not lag.lagging
            or lag.peers_pct < basket.min_basket_move_pct
            or lag.end >= today
        ):
            reasons.append("no_prior_close_lag_in_rising_group")
        else:
            peers = readers.priced_peers(
                engine, lag.peers.split(","), lag.start, lag.end, utc(lag.recorded_at)
            )
            if len(peers) + 1 < basket.min_members_with_data:
                reasons.append("peer_anchor_coverage")
        if not following_sessions(
            today, max((cfg.primary_sessions, *cfg.secondary_sessions)), live.calendar
        ):
            reasons.append("calendar_unknown")
        arm = "excluded" if reasons else "flow" if flow.qualified else "control"
        payload: dict[str, Any] = {
            "policy": cfg.policy_version,
            "profile_hash": cfg.profile_hash,
            "version": version,
            "day": today.isoformat(),
            "ticker": ticker,
            "as_of": now.isoformat(),
            "legacy_scan": scan.scan_id,
            "flow": asdict(flow),
            "capture": capture_reason,
            "news": asdict(news),
            "spot": asdict(spot) if spot else None,
            "move_atr": price.move_atr,
            "lag_version": lag.version if lag else None,
            "peers": peers,
            "lag_start": lag.start.isoformat() if lag else None,
            "lag_end": lag.end.isoformat() if lag else None,
            "lag_residual_pct": lag.residual_pct if lag else None,
            "lag_available_at": utc(lag.recorded_at).isoformat() if lag else None,
            "arm": arm,
            "reasons": reasons,
            "raw_refs": sorted({o.alert_id for o in observations.get(ticker, ())}),
            "primary_sessions": cfg.primary_sessions,
            "horizons": [cfg.primary_sessions, *cfg.secondary_sessions],
            "slippage_bps": cfg.slippage_bps_per_side,
            "benchmark": cfg.benchmark,
        }
        payload["input_hash"] = db.digest(payload)
        audits.append(
            dict(
                key=db.digest((version, scan.scan_id, ticker)),
                ticker=ticker,
                observed_at=now,
                payload_json=db.dumps(payload),
            )
        )
        if arm != "excluded":
            eligible += 1
            treated += arm == "flow"
            cohorts.append(
                dict(
                    key=db.digest((version, today.isoformat(), ticker)),
                    ticker=ticker,
                    version=version,
                    arm=arm,
                    horizon_count=len(payload["horizons"]),
                    observed_at=now,
                    payload_json=db.dumps(payload),
                )
            )
    db.append(
        engine,
        cast(Table, db.Price.__table__),
        [
            dict(
                key=db.digest((ticker, p.fetched_at.isoformat())),
                ticker=ticker,
                observed_at=now,
                source_at=p.fetched_at,
                value=p.spot,
                session_day=p.trade_date.isoformat(),
            )
            for ticker, p in prices.items()
            if p.trade_date is not None
            and p.spot is not None
            and 0 <= (now - p.fetched_at).total_seconds() <= cfg.spot_max_age_seconds
        ],
    )
    db.append(engine, cast(Table, db.Audit.__table__), audits)
    db.append(engine, cast(Table, db.Cohort.__table__), cohorts)
    progress = update_outcomes(engine, now, cfg)
    state: dict[str, object] = {
        "status": "ok",
        "at": now.isoformat(),
        "candidates": len(tickers),
        "eligible_this_cycle": eligible,
        "flow_this_cycle": treated,
        **progress,
    }
    db.append(
        engine,
        cast(Table, db.Cycle.__table__),
        [
            dict(
                key=db.digest((version, scan.scan_id)),
                observed_at=now,
                payload_json=db.dumps(state),
            )
        ],
    )
    STATE.clear()
    STATE.update(state)
    return state


def update_outcomes(
    engine: Engine, now: datetime, cfg: LabSettings | None = None
) -> dict[str, object]:
    cfg = cfg or load_settings()
    live = load_live_alpha_settings()
    with session_factory(engine)() as s:
        complete = (
            select(func.count(db.Outcome.key))
            .where(db.Outcome.cohort_key == db.Cohort.key)
            .correlate(db.Cohort)
            .scalar_subquery()
        )
        query = (
            select(db.Cohort)
            .where(complete < db.Cohort.horizon_count, db.Cohort.observed_at <= now)
            .order_by(db.Cohort.observed_at, db.Cohort.key)
        )
        total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
        latest = s.scalar(select(db.Cycle).order_by(db.Cycle.observed_at.desc()).limit(1))
        cursor = (
            int(json.loads(latest.payload_json).get("outcome_cursor", 0)) % total
            if latest and total
            else 0
        )
        cohorts = list(s.scalars(query.offset(cursor).limit(cfg.outcome_batch_size)))
        known = set(
            s.scalars(
                select(db.Outcome.key).where(db.Outcome.cohort_key.in_([r.key for r in cohorts]))
            )
        )
    symbols: set[str] = set()
    pending = []
    for row in cohorts:
        p = json.loads(row.payload_json)
        for horizon in p["horizons"]:
            key = db.digest((row.key, horizon))
            if key not in known:
                pending.append((row, p, horizon, key))
                symbols.update((row.ticker, p["benchmark"], *p["peers"]))
    bars = readers.bars_at(engine, sorted(symbols), now) if pending else {}
    writes = []
    counts = {"priced_new": 0, "pending": 0, "unknown_prices": 0, "calendar_unknown": 0}
    for row, p, horizon, key in pending:
        signal_day = datetime.fromisoformat(p["day"]).date()
        result = outcome(
            signal_day,
            row.ticker,
            p["peers"],
            p["benchmark"],
            horizon,
            bars,
            now,
            live.calendar,
            p["slippage_bps"],
        )
        if result.state != "priced":
            counts[result.state] += 1
            continue
        counts["priced_new"] += 1
        writes.append(
            dict(
                key=key,
                ticker=row.ticker,
                cohort_key=row.key,
                version=row.version,
                horizon=horizon,
                arm=row.arm,
                stock_net_pct=result.stock_net_pct,
                peer_excess_pp=result.peer_excess_pp,
                spy_excess_pp=result.spy_excess_pp,
                observed_at=now,
                payload_json=db.dumps(
                    {
                        "cohort_key": row.key,
                        "arm": p["arm"],
                        "version": p["version"],
                        "policy": p["policy"],
                        "horizon": horizon,
                        "primary": horizon == p["primary_sessions"],
                        "signal_day": p["day"],
                        "peers": p["peers"],
                        "model": "stock open/close + adverse slippage; fees excluded",
                        **asdict(result),
                    }
                ),
            )
        )
    counts["priced_new"] = db.append(engine, cast(Table, db.Outcome.__table__), writes)
    return {**counts, "outcome_backlog": total, "outcome_cursor": cursor + len(cohorts)}
