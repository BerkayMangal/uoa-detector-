"""Notifications require a committed current opening BUY; recorded exits survive."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from sqlalchemy.engine import Engine

from webapp.alpha_lab.db import digest
from webapp.live_alpha import alerts as legacy
from webapp.opening import view


def feed(engine: Engine, now: datetime) -> legacy.Feed:
    old = legacy.feed(engine, now)
    desk = view.build(engine, now)
    exits = tuple(s for s in old.signals if s.event_type == "exit")
    signals = list(exits)
    for statement in desk.statements:
        a = statement.advice
        if not statement.current or a.action != "BUY" or a.plan is None or a.expires_at is None:
            continue
        if old.scan_id != statement.scan_id:
            continue
        plan = a.plan
        # Same opportunity key across scans prevents repeat toasts for a price update.
        signals.append(
            legacy.Signal(
                key=digest(
                    ("opening_desk_v1", desk.clock.day, a.ticker, a.direction, a.instrument)
                ),
                ticker=a.ticker,
                instrument=cast(
                    Literal["stock", "long_call", "bull_call_debit", "long_put", "bear_put_debit"],
                    a.instrument,
                ),
                label=a.headline,
                contracts=plan.contracts,
                underlying_ref=plan.entry,
                underlying_stop=plan.stop,
                underlying_target=plan.target,
                as_of=statement.at,
                expires_at=a.expires_at,
            )
        )
    return legacy.Feed(
        server_time=now,
        status="current"
        if desk.current or exits
        else old.status
        if old.status != "current"
        else "stale",
        detail="Kayıtlı açılış planları ve pozisyon çıkışları."
        if desk.current
        else "Güncel açılış planı bekleniyor; yeni giriş uyarısı yok.",
        scan_id=old.scan_id,
        signals=tuple(signals),
        exit_status=old.exit_status,
    )
