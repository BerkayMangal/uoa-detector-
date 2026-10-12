"""The hot list: names the market-wide unusual-flow feed says are active (Phase 5.27).

Contract: ``docs/phase-5.27-auto-peers-hotlist-acceptance.md`` §3.3 to §3.6.

The system reads ``GET /api/option-trades/flow-alerts`` with no ticker (market-wide,
newest first), adds up each name's premium per ET session, and keeps the maximum
it has observed per (session, ticker). The feed window slides, so adding refreshes
together would count an alert once per refresh; the maximum is a lower bound of the
session total and never double-counts.

Two readers use what is stored:

- the live worker polls ``LIVE_TICKERS`` plus the top ``poll_top_n`` names of the
  newest session with data (``load_hot_tickers``);
- the daily-close job covers every name seen recently (``load_universe``), so the
  automatic peers of ``peers.py`` have a field that grows by itself.

Nothing here edits ``LIVE_TICKERS``. A failing refresh costs the list its newest
read and nothing else; the board, the cycle and the worker run on the last list.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Final, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from sqlalchemy import Date, DateTime, Float, Integer, String, Table, func, select
from sqlalchemy.orm import Mapped, mapped_column

from uoa_detector.live_alpha.settings import profile_path
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from uoa_detector.sources.unusual_whales.flow_alerts import FLOW_ALERTS_PAGE_LIMIT, FLOW_ALERTS_PATH
from uoa_detector.sources.unusual_whales.live import _parse_iso_utc
from webapp.board.daily_close import AlfaDailyClose, run_daily_close_job
from webapp.board.daily_jobs import DailyJob, JobContext
from webapp.board.db import AlfaBase, session_factory
from webapp.board.uw_errors import JsonClient, is_key_failure

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.engine import Engine

HOTLIST_PROFILE_NAME: Final = "hotlist_v1.yaml"
HOT_FLOW_JOB: Final = "hot_flow"
CLOSE_CATCHUP_JOB: Final = "close_catchup"

_ET: Final = ZoneInfo("America/New_York")
_logger = logging.getLogger(__name__)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HotlistSettings(_Strict):
    """``profiles/hotlist_v1.yaml`` (§3.3)."""

    hotlist_version: str = Field(min_length=1)
    poll_top_n: int = Field(ge=0)
    poll_min_alerts: int = Field(ge=1)
    universe_sessions: int = Field(ge=1)
    universe_min_alerts: int = Field(ge=1)
    universe_cap: int = Field(ge=1)
    pages_per_tick: int = Field(ge=1, le=10)
    job_time_et: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    catchup_time_et: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    catchup_max: int = Field(ge=0)
    exclude: tuple[str, ...] = ()

    def excluded(self, ticker: str) -> bool:
        return ticker.strip().upper() in {t.strip().upper() for t in self.exclude}


def load_hotlist_settings(path: pathlib.Path | None = None) -> HotlistSettings:
    """Load and validate the hot-list profile. Raises on a missing file or invalid content."""
    target = path if path is not None else profile_path(HOTLIST_PROFILE_NAME)
    raw = YAML(typ="safe").load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"hotlist profile {target} must be a mapping"
        raise ValueError(msg)
    return HotlistSettings.model_validate(raw)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class AlfaHotFlow(AlfaBase):
    """The largest premium sum and alert count observed per (ET session, ticker)."""

    __tablename__ = "alfa_hot_flow"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    premium: Mapped[float] = mapped_column(Float)
    alerts: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def ensure_hotlist_tables(engine: Engine) -> None:
    """Create ``alfa_hot_flow`` when missing. Never alters or drops anything."""
    cast(Table, AlfaHotFlow.__table__).create(engine, checkfirst=True)


@dataclass(frozen=True)
class HotFlow:
    day: date
    ticker: str
    premium: float
    alerts: int


def aggregate_alerts(rows: Iterable[Mapping[str, object]], cfg: HotlistSettings) -> list[HotFlow]:
    """Premium sum and alert count per (ET session, ticker). Pure.

    An alert is counted once even when two pages overlap (key: option chain + time).
    A row without a usable ticker, premium or time is skipped, and excluded products
    are ignored (§3.3).
    """
    seen: set[str] = set()
    totals: dict[tuple[date, str], tuple[float, int]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or cfg.excluded(ticker):
            continue
        try:
            premium = float(str(row["total_premium"]))
            when = _parse_iso_utc(str(row["created_at"]))
        except (KeyError, ValueError, TypeError):
            continue
        if not math.isfinite(premium) or premium < 0:
            continue
        key = f"{row.get('option_chain', ticker)}|{row['created_at']}|{row.get('total_size', '')}"
        if key in seen:
            continue
        seen.add(key)
        slot = (when.astimezone(_ET).date(), ticker)
        total, count = totals.get(slot, (0.0, 0))
        totals[slot] = (total + premium, count + 1)
    return [HotFlow(day=d, ticker=t, premium=p, alerts=n) for (d, t), (p, n) in sorted(totals.items())]


def upsert_hot_flow(engine: Engine, flows: Iterable[HotFlow], *, now: datetime) -> int:
    """Keep the maximum observed premium and alert count per key. Returns rows written."""
    written = 0
    with session_factory(engine)() as session:
        for flow in flows:
            row = session.get(AlfaHotFlow, (flow.day, flow.ticker))
            if row is None:
                session.add(AlfaHotFlow(day=flow.day, ticker=flow.ticker, premium=flow.premium, alerts=flow.alerts, updated_at=now))
                written += 1
            elif flow.premium > row.premium or flow.alerts > row.alerts:
                row.premium = max(row.premium, flow.premium)
                row.alerts = max(row.alerts, flow.alerts)
                row.updated_at = now
                written += 1
        session.commit()
    return written


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HotRefresh:
    status: str  # ok | no_data | degraded
    alerts: int = 0
    pages: int = 0
    written: int = 0


async def _fetch_market_alerts(client: JsonClient, cfg: HotlistSettings) -> tuple[list[dict[str, Any]], int]:
    """Up to ``pages_per_tick`` market-wide pages, newest first. Bounded on purpose (§3.3)."""
    rows: list[dict[str, Any]] = []
    cursor: int | None = None
    pages = 0
    while pages < cfg.pages_per_tick:
        params: dict[str, Any] = {"limit": FLOW_ALERTS_PAGE_LIMIT}
        if cursor is not None:
            params["older_than"] = cursor
        response = await client.request_json(FLOW_ALERTS_PATH, params=params)
        pages += 1
        data = response.get("data") if isinstance(response, dict) else None
        page = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        rows.extend(page)
        if len(page) < FLOW_ALERTS_PAGE_LIMIT:
            break
        stamps: list[datetime] = []
        for row in page:
            try:
                stamps.append(_parse_iso_utc(str(row["created_at"])))
            except (KeyError, ValueError):
                continue
        if not stamps:
            break
        nxt = math.floor(min(stamps).timestamp())
        if cursor is not None and nxt >= cursor:
            break  # the cursor did not move back in time: stop rather than loop
        cursor = nxt
    return rows, pages


async def refresh_hot_flow(
    client: JsonClient, engine: Engine, cfg: HotlistSettings, *, now: datetime,
) -> HotRefresh:
    """Read the market-wide feed once and store what it shows.

    A daily-limit error and a key failure propagate (the refresher already backs off
    on them); every other UW problem is one degraded refresh, not an exception.
    """
    ensure_hotlist_tables(engine)
    try:
        rows, pages = await _fetch_market_alerts(client, cfg)
    except UnusualWhalesNotFoundError:
        return HotRefresh(status="no_data")
    except UnusualWhalesDailyLimitError:
        raise  # a subclass of the rate-limit error, and the one case that must not be swallowed
    except UnusualWhalesAuthError as exc:
        if is_key_failure(exc):
            raise
        _logger.warning("hot flow fetch degraded: %s", exc)
        return HotRefresh(status="degraded")
    except (UnusualWhalesRateLimitError, UnusualWhalesTransientError, CircuitBreakerOpenError) as exc:
        _logger.warning("hot flow fetch degraded: %s", exc)
        return HotRefresh(status="degraded")
    flows = aggregate_alerts(rows, cfg)
    written = upsert_hot_flow(engine, flows, now=now)
    return HotRefresh(status="ok" if rows else "no_data", alerts=len(rows), pages=pages, written=written)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def load_hot_tickers(engine: Engine, today: date, cfg: HotlistSettings) -> tuple[str, ...]:
    """The top ``poll_top_n`` names of the newest session with data, premium first (§3.3).

    Empty when nothing is stored yet. The newest session at or before ``today`` is
    used, so before the open the list is yesterday's and the board keeps following
    the names that were active.
    """
    if cfg.poll_top_n == 0:
        return ()
    with session_factory(engine)() as session:
        latest = session.execute(select(func.max(AlfaHotFlow.day)).where(AlfaHotFlow.day <= today)).scalar()
        if latest is None:
            return ()
        rows = session.execute(
            select(AlfaHotFlow.ticker)
            .where(AlfaHotFlow.day == latest, AlfaHotFlow.alerts >= cfg.poll_min_alerts)
            .order_by(AlfaHotFlow.premium.desc(), AlfaHotFlow.ticker)
        ).scalars().all()
    return tuple(t for t in rows if not cfg.excluded(t))[: cfg.poll_top_n]


def load_universe(engine: Engine, today: date, cfg: HotlistSettings) -> tuple[str, ...]:
    """Names with enough alerts in the last ``universe_sessions`` stored sessions, capped (§3.3)."""
    with session_factory(engine)() as session:
        days = session.execute(
            select(AlfaHotFlow.day).where(AlfaHotFlow.day <= today).distinct()
            .order_by(AlfaHotFlow.day.desc()).limit(cfg.universe_sessions)
        ).scalars().all()
        if not days:
            return ()
        rows = session.execute(
            select(AlfaHotFlow.ticker, func.max(AlfaHotFlow.premium))
            .where(AlfaHotFlow.day.in_(days), AlfaHotFlow.alerts >= cfg.universe_min_alerts)
            .group_by(AlfaHotFlow.ticker)
            .order_by(func.max(AlfaHotFlow.premium).desc(), AlfaHotFlow.ticker)
        ).all()
    return tuple(t for t, _ in rows if not cfg.excluded(t))[: cfg.universe_cap]


def hot_close_tickers(engine: Engine, now: datetime, cfg: HotlistSettings) -> tuple[str, ...]:
    """What the close job should cover beyond its own list: the universe. Never raises."""
    try:
        return load_universe(engine, now.astimezone(_ET).date(), cfg)
    except Exception:
        _logger.warning("hot universe unreadable; no hot closes this tick", exc_info=True)
        return ()


# ---------------------------------------------------------------------------
# Daily jobs
# ---------------------------------------------------------------------------


def _without_closes(engine: Engine, tickers: Sequence[str]) -> list[str]:
    """The tickers that have no stored close at all, input order kept."""
    wanted = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    if not wanted:
        return []
    with session_factory(engine)() as session:
        have = set(
            session.execute(select(AlfaDailyClose.ticker).where(AlfaDailyClose.ticker.in_(wanted)).distinct()).scalars()
        )
    return [t for t in wanted if t not in have]


def hot_daily_jobs(cfg: HotlistSettings) -> tuple[DailyJob, ...]:
    """The two jobs the hot list adds to the registry (§3.3, §3.6)."""

    async def run_hot_flow(ctx: JobContext) -> str:
        result = await refresh_hot_flow(ctx.client, ctx.engine, cfg, now=ctx.now)
        return f"status={result.status} alerts={result.alerts} pages={result.pages} written={result.written}"

    async def run_close_catchup(ctx: JobContext) -> str:
        names = (*ctx.close_extra, *ctx.tickers)
        missing = _without_closes(ctx.engine, names)[: cfg.catchup_max]
        if not missing:
            return "nothing missing"
        result = await run_daily_close_job(ctx.client, ctx.engine, missing, now=ctx.now)
        inserted = sum(r.inserted for r in result.tickers)
        return f"tickers={len(missing)} closes={inserted} degraded={len(result.degraded)}"

    return (
        DailyJob(name=HOT_FLOW_JOB, et_time=time.fromisoformat(cfg.job_time_et), run=run_hot_flow),
        DailyJob(name=CLOSE_CATCHUP_JOB, et_time=time.fromisoformat(cfg.catchup_time_et), run=run_close_catchup),
    )
