"""The "Hisse panosu": one plain row per ticker, measured facts only (display, no decision).

Per ticker:
- price and today's move (the cycle's price context);
- where the option money is betting today (the cycle's flow summary);
- how busy today's flow is against the ticker's own normal: prints so far today
  versus the median of prior live sessions up to the SAME time of day (a full past
  day is never compared with a half day);
- the move the options price in: ATM straddle mid / spot for the first stored
  expiry at least ``move_min_dte`` away (``alfa_atm``), with its quote time;
- whether implied vol is rich or cheap: IV rank from the gamma snapshot;
- the next earnings date.

Every cutoff that picks a word is in ``profiles/live_board_v1.yaml``.
"""

from __future__ import annotations

import io
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from sqlalchemy import select

from uoa_detector.backtest.sqlite_models import SignalRow
from uoa_detector.live_alpha.plain import money
from uoa_detector.live_alpha.settings import profile_path
from webapp.board.atm import AlfaAtm
from webapp.board.db import session_factory
from webapp.live_alpha.inputs import LIVE_RUN_PREFIX, as_utc

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine

    from uoa_detector.live_alpha.model import FlowSummary, PriceContext

_ET = ZoneInfo("America/New_York")


class BoardSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    board_version: str
    iv_rich_rank: float = Field(gt=0, le=1)
    iv_cheap_rank: float = Field(ge=0, lt=1)
    flow_busy_multiple: float = Field(gt=1)
    flow_quiet_multiple: float = Field(gt=0, lt=1)
    flow_history_sessions: int = Field(ge=1)
    flow_min_history: int = Field(ge=1)
    move_min_dte: int = Field(ge=0)
    earnings_warn_days: int = Field(ge=0)


def load_board_settings() -> BoardSettings:
    raw = profile_path("live_board_v1.yaml").read_bytes()
    data: Any = YAML(typ="safe").load(io.BytesIO(raw))
    return BoardSettings(**data)


@dataclass(frozen=True)
class FlowPace:
    today: int
    normal: float | None       # median of prior sessions at the same time of day
    sessions: int

    @property
    def multiple(self) -> float | None:
        if self.normal is None or self.normal <= 0:
            return None
        return self.today / self.normal


def flow_pace(
    engine: Engine, tickers: Sequence[str], today_run: str, now: datetime, cfg: BoardSettings,
) -> dict[str, FlowPace]:
    """Prints so far today vs the median of prior live sessions up to the same ET time."""
    stmt = (
        select(SignalRow.run_id)
        .where(SignalRow.run_id.like(f"{LIVE_RUN_PREFIX}%"))
        .group_by(SignalRow.run_id)
        .order_by(SignalRow.run_id.desc())
        .limit(cfg.flow_history_sessions + 1)
    )
    with session_factory(engine)() as s:
        runs = [r for (r,) in s.execute(stmt)]
        prior = [r for r in runs if r < today_run][: cfg.flow_history_sessions]
        wanted = [today_run, *prior]
        rows = s.execute(
            select(SignalRow.run_id, SignalRow.ticker, SignalRow.ts).where(SignalRow.run_id.in_(wanted)),
        ).all()
    cutoff = now.astimezone(_ET).time()
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for run_id, ticker, ts in rows:
        if run_id != today_run and as_utc(ts).astimezone(_ET).time() > cutoff:
            continue
        counts[(run_id, ticker.upper())] += 1
    out: dict[str, FlowPace] = {}
    for t in tickers:
        history = [counts.get((r, t), 0) for r in prior]
        normal = statistics.median(history) if len(history) >= cfg.flow_min_history else None
        out[t] = FlowPace(today=counts.get((today_run, t), 0), normal=normal, sessions=len(history))
    return out


@dataclass(frozen=True)
class ExpectedMove:
    expiry: date
    pct: float
    straddle: float
    fetched_at: datetime


def expected_moves(engine: Engine, tickers: Sequence[str], today: date, cfg: BoardSettings) -> dict[str, ExpectedMove]:
    symbols = sorted({t.upper() for t in tickers})
    stmt = select(AlfaAtm).where(AlfaAtm.ticker.in_(symbols)).order_by(AlfaAtm.ticker, AlfaAtm.expiry)
    out: dict[str, ExpectedMove] = {}
    with session_factory(engine)() as s:
        for row in s.execute(stmt).scalars():
            if row.ticker in out or (row.expiry - today).days < cfg.move_min_dte:
                continue
            legs = (row.call_bid, row.call_ask, row.put_bid, row.put_ask)
            if row.stock_price is None or row.stock_price <= 0 or any(v is None or v <= 0 for v in legs):
                continue
            cb, ca, pb, pa = (float(v) for v in legs)  # type: ignore[arg-type]
            if ca < cb or pa < pb:
                continue
            straddle = (cb + ca) / 2 + (pb + pa) / 2
            out[row.ticker] = ExpectedMove(
                expiry=row.expiry, pct=straddle / float(row.stock_price), straddle=straddle,
                fetched_at=as_utc(row.fetched_at),
            )
    return out


def _bet_text(f: FlowSummary | None) -> tuple[str, str]:
    if f is None or f.side_aware_premium <= 0:
        return "kayda değer işlem yok", "gray"
    up_share = f.share("up")
    if up_share >= 0.65:
        return f"yükselişe %{up_share * 100:.0f} ({money(f.premium_up)})", "green"
    if up_share <= 0.35:
        return f"düşüşe %{(1 - up_share) * 100:.0f} ({money(f.premium_down)})", "red"
    return f"kararsız: ↑{money(f.premium_up)} / ↓{money(f.premium_down)}", "gray"


def build_rows(
    tickers: Sequence[str],
    flows: Mapping[str, FlowSummary],
    prices: Mapping[str, PriceContext],
    pace: Mapping[str, FlowPace],
    moves: Mapping[str, ExpectedMove],
    gamma: Mapping[str, Any],
    verdicts: Mapping[str, tuple[str, str, str]],
    today: date,
    cfg: BoardSettings,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in tickers:
        p = prices.get(t)
        bet, bet_tone = _bet_text(flows.get(t))
        fp = pace.get(t)
        if fp is None or fp.multiple is None:
            pace_txt = f"bugün {fp.today} işlem (karşılaştırma için yeterli geçmiş yok)" if fp else "—"
            pace_tone = "gray"
        else:
            m = fp.multiple
            word = "yoğun" if m >= cfg.flow_busy_multiple else ("sakin" if m <= cfg.flow_quiet_multiple else "normal")
            pace_txt = f"{word}: normalin {m:.1f} katı"
            pace_tone = "amber" if word == "yoğun" else "gray"
        mv = moves.get(t)
        move_txt = (
            f"±%{mv.pct * 100:.1f} ({mv.expiry:%d.%m} vadesine kadar)" if mv is not None else "—"
        )
        g = gamma.get(t)
        iv_rank = getattr(g, "iv_pct", None) if g is not None else None
        if iv_rank is None:
            iv_txt, iv_tone = "—", "gray"
        elif iv_rank >= cfg.iv_rich_rank:
            iv_txt, iv_tone = f"pahalı (yıllık sıralama %{iv_rank * 100:.0f})", "amber"
        elif iv_rank <= cfg.iv_cheap_rank:
            iv_txt, iv_tone = f"ucuz (yıllık sıralama %{iv_rank * 100:.0f})", "green"
        else:
            iv_txt, iv_tone = f"normal (yıllık sıralama %{iv_rank * 100:.0f})", "gray"
        earn = getattr(g, "next_earnings", None) if g is not None else None
        if isinstance(earn, date) and earn >= today:
            days = (earn - today).days
            earn_txt = f"{earn:%d.%m} ({days} gün)"
            earn_warn = days <= cfg.earnings_warn_days
        else:
            earn_txt, earn_warn = "—", False
        label, reason, tone = verdicts.get(t, ("—", "", "gray"))
        rows.append({
            "ticker": t,
            "price": p.spot if p else None,
            "move_pct": p.move * 100 if p and p.move is not None else None,
            "bet": bet, "bet_tone": bet_tone,
            "pace": pace_txt, "pace_tone": pace_tone,
            "implied_move": move_txt,
            "move_as_of": mv.fetched_at.astimezone(_ET).strftime("%d.%m %H:%M ET") if mv else None,
            "iv": iv_txt, "iv_tone": iv_tone,
            "earnings": earn_txt, "earnings_warn": earn_warn,
            "verdict": label, "verdict_reason": reason, "verdict_tone": tone,
        })
    return rows

