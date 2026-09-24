"""The Live Alpha cycle and its supervised loop (contract §5, §8, §9).

One cycle: read the stored flow/spot/bars → news for qualifying tickers → decide →
quotes for tickers that need an option plan → cards → immutable records → PAPER
tracking → one ``alfa_live_scan`` snapshot + a heartbeat. The page reads only the
newest snapshot, so a slow provider never slows the page.

Every UW call reserves budget first (``uw.Budget``). A cycle that fails half-way
still writes a snapshot that says what failed; it never leaves the previous
snapshot looking current without a heartbeat saying otherwise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.calendar import MarketMode, Session, classify, sessions_between
from uoa_detector.live_alpha.decide import Decision, build_card, evaluate, sort_cards
from uoa_detector.live_alpha.flow import qualifying_direction, summarise, tickers_in
from uoa_detector.live_alpha.model import (
    Card,
    CheckState,
    FlowSummary,
    NewsCheck,
    OptionQuote,
    OptionStructureView,
    PriceContext,
    Readiness,
    Recommendation,
    Tracking,
    to_jsonable,
)
from uoa_detector.live_alpha.option_plan import (
    build_structures,
    candidate_strikes,
    occ_symbol,
    pick_expiry,
    price_view,
)
from uoa_detector.live_alpha.settings import LiveAlphaSettings, load_live_alpha_settings
from uoa_detector.options_alpha.settings import OptionsAlphaSettings
from uoa_detector.options_alpha.settings import load_settings as load_options_settings
from webapp.board.daily_close import load_bars_by_ticker
from webapp.board.settings import BoardSettings
from webapp.live_alpha import inputs, store, uw
from webapp.live_alpha.uw import Budget, ContractSpec, JsonClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.engine import Engine

    from webapp.board.signals import BoardSignalReader

_logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

PAPER_ACTIVE = (Tracking.PAPER_PENDING.value, Tracking.PAPER_OPEN.value, Tracking.EXIT_SIGNALLED.value)

# The last cycle's outcome, in memory, for the open /health answer: time, status, mode and
# counts only — no ticker, no price, no key. Railway's healthcheck only gates deploy
# activation; this is what lets anyone see the job is still cycling.
LAST_CYCLE: dict[str, object] = {}


@dataclass
class CycleContext:
    engine: Engine
    reader: BoardSignalReader
    client: JsonClient
    settings: LiveAlphaSettings
    board: BoardSettings
    costs: OptionsAlphaSettings
    now: datetime
    session: Session
    budget: Budget
    notes: list[str] = field(default_factory=list)
    quotes_cache: dict[str, dict[str, OptionQuote]] = field(default_factory=dict)


def load_costs() -> OptionsAlphaSettings:
    from uoa_detector.live_alpha.settings import profile_path

    return load_options_settings(profile_path("options_alpha_v1.yaml"))


# ---------------------------------------------------------------------------
# Option plan for one decision
# ---------------------------------------------------------------------------


def contract_specs(
    ticker: str, direction: str, spot: float, target: float, expiry: Any, ctx: CycleContext,
) -> list[ContractSpec]:
    right = "call" if direction == "up" else "put"
    strikes = candidate_strikes(
        spot, target, ctx.settings.option_plan.strike_grid_candidates,
        ctx.settings.option_plan.max_symbols_per_request,
    )
    return [
        ContractSpec(symbol=occ_symbol(ticker, expiry, right, s), right=right, strike=Decimal(str(s)), expiry=expiry)
        for s in strikes
    ]


async def option_structures(
    d: Decision, expiries: Sequence[Any], ctx: CycleContext,
) -> tuple[tuple[OptionStructureView, ...], str]:
    plan = d.stock_plan
    spot = d.price.spot
    if plan is None or spot is None:
        return (), "hisse planı yok, opsiyon kurulmadı"
    today = ctx.session.et_now.date()
    expiry = pick_expiry(expiries, today, ctx.settings.option_plan)
    if expiry is None:
        return (), (
            f"{ctx.settings.option_plan.min_dte}-{ctx.settings.option_plan.max_dte} gün aralığında "
            "listelenmiş vade bulunamadı (alfa_atm_expiry)"
        )
    ref = plan.entry_ref
    specs = contract_specs(d.ticker, d.direction, ref, plan.target, expiry, ctx)
    got = await uw.quotes_for(
        ctx.client, ctx.budget, d.ticker, specs, ctx.now,
        in_session=ctx.session.mode is MarketMode.LIVE,
    )
    ctx.quotes_cache[d.ticker] = {**ctx.quotes_cache.get(d.ticker, {}), **got.quotes}
    if not got.quotes:
        return (), got.state
    views = build_structures(
        d.direction, got.quotes, ref, plan.target, ctx.now, today,
        ctx.settings.option_plan, ctx.costs, ctx.settings.stock_plan.r_usd,
    )
    return views, ""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def inputs_hash(card: Card) -> str:
    basis = {
        "flow": card.option_evidence, "news": card.news_evidence, "price": card.price_evidence,
        "rec": card.recommendation.value, "ready": card.readiness.value,
    }
    return hashlib.sha256(store.dumps(basis).encode()).hexdigest()[:24]


def record_changes(engine: Engine, cards: Sequence[Card], scan_id: str, now: datetime) -> dict[str, str]:
    """Append a record for every card whose (recommendation, readiness) changed; return rec ids."""
    heads = store.latest_recs(engine, [c.opportunity_id for c in cards])
    rec_ids: dict[str, str] = {}
    for c in cards:
        head = heads.get(c.opportunity_id)
        if head is not None and head.recommendation == c.recommendation.value and head.readiness == c.readiness.value:
            rec_ids[c.opportunity_id] = head.rec_id
            continue
        rec_id = store.new_id()
        store.append_rec(engine, store.LiveRec(
            rec_id=rec_id, opportunity_id=c.opportunity_id, ticker=c.ticker, direction=c.direction,
            created_at=now, recommendation=c.recommendation.value, readiness=c.readiness.value,
            path=c.path.value, policy_version=c.policy_version, profile_sha256=c.profile_sha256,
            inputs_hash=inputs_hash(c), supersedes=head.rec_id if head is not None else None,
            scan_id=scan_id, card_json=store.dumps(to_jsonable(c)),
        ))
        store.append_event(
            engine, at=now, opportunity_id=c.opportunity_id, kind="recommendation",
            actor="job", policy_version=c.policy_version,
            detail={"rec_id": rec_id, "recommendation": c.recommendation.value,
                    "readiness": c.readiness.value, "supersedes": head.rec_id if head else None},
        )
        rec_ids[c.opportunity_id] = rec_id
    return rec_ids


# ---------------------------------------------------------------------------
# PAPER
# ---------------------------------------------------------------------------


def _paper_plan(card: Card) -> dict[str, Any]:
    preferred = card.instrument.preferred
    structure = next((v for v in card.options if v.kind == preferred), None)
    return {
        "instrument": preferred,
        "stock_plan": to_jsonable(card.stock_plan),
        "structure": to_jsonable(structure) if structure is not None else None,
        "decision_as_of": card.decision_as_of.isoformat(),
        "policy_version": card.policy_version,
        # the one session in which this PAPER may fill; after it, the pending position expires
        "fill_session": card.opportunity_id.split(":", 1)[0],
    }


def open_papers_for_new_buys(ctx: CycleContext, cards: Sequence[Card], rec_ids: dict[str, str]) -> int:
    if not ctx.settings.paper.enabled:
        return 0
    opened = 0
    for c in cards:
        if c.recommendation is not Recommendation.BUY or c.readiness is not Readiness.READY:
            continue
        if c.instrument.preferred == "none" or c.stock_plan is None:
            continue
        paper = store.LivePaper(
            paper_id=store.new_id(), opportunity_id=c.opportunity_id, rec_id=rec_ids[c.opportunity_id],
            ticker=c.ticker, direction=c.direction, instrument=c.instrument.preferred,
            plan_json=store.dumps(_paper_plan(c)), state=Tracking.PAPER_PENDING.value, created_at=ctx.now,
        )
        if store.create_paper(ctx.engine, paper):
            opened += 1
            store.append_event(
                ctx.engine, at=ctx.now, opportunity_id=c.opportunity_id, kind="paper_pending",
                actor="job", policy_version=ctx.settings.policy_version,
                detail={"paper_id": paper.paper_id, "instrument": paper.instrument,
                        "note": "öneri yayımlandı; simüle giriş tetik ve taze fiyatı bekler"},
            )
    return opened


def _fresh_spot(price: PriceContext | None, ctx: CycleContext) -> float | None:
    if price is None or price.spot is None or price.spot_fetched_at is None:
        return None
    if ctx.session.mode is not MarketMode.LIVE:
        return None
    age = (ctx.now - price.spot_fetched_at).total_seconds()
    return price.spot if 0 <= age <= ctx.settings.price.spot_max_age_seconds else None


async def _structure_now(paper: store.LivePaper, plan: dict[str, Any], ctx: CycleContext) -> OptionStructureView | None:
    """Re-price the frozen structure's legs on fresh quotes (fetching them if needed)."""
    structure = plan.get("structure") or {}
    legs = structure.get("legs") or []
    if not legs:
        return None
    cached = ctx.quotes_cache.get(paper.ticker, {})
    missing = [leg for leg in legs if leg["option_symbol"] not in cached]
    if missing:
        specs = [
            ContractSpec(symbol=leg["option_symbol"], right=leg["right"],
                         strike=Decimal(str(leg["strike"])), expiry=datetime.fromisoformat(leg["expiry"]).date())
            for leg in legs
        ]
        got = await uw.quotes_for(ctx.client, ctx.budget, paper.ticker, specs, ctx.now,
                                  in_session=ctx.session.mode is MarketMode.LIVE)
        cached = {**cached, **got.quotes}
        ctx.quotes_cache[paper.ticker] = cached
    quotes = [cached.get(leg["option_symbol"]) for leg in legs]
    if any(q is None for q in quotes):
        return None
    pairs = [(q, bool(leg["is_long"])) for q, leg in zip(quotes, legs, strict=True) if q is not None]
    return price_view(
        str(structure.get("kind")), pairs, ctx.now, ctx.session.et_now.date(),
        ctx.settings.option_plan, ctx.costs, ctx.settings.stock_plan.r_usd,
    )


def _event(ctx: CycleContext, paper: store.LivePaper, kind: str, detail: dict[str, Any]) -> None:
    store.append_event(
        ctx.engine, at=ctx.now, opportunity_id=paper.opportunity_id, kind=kind, actor="job",
        policy_version=ctx.settings.policy_version, detail={"paper_id": paper.paper_id, **detail},
    )


def _bar_exit(
    up: bool, stop: float, target: float, entry_day: Any, bars: Sequence[Any], horizon: int, ctx: CycleContext,
) -> tuple[str, float] | None:
    """Exit from completed daily bars when no fresh spot exists (master prompt §15).

    Only sessions strictly after the entry day are read (the entry day's intrabar
    order is unknown). When one bar crosses both levels the STOP is taken, never the
    target. A gap through a level exits at the open, not at the level.
    """
    today = ctx.session.session_date or ctx.session.et_now.date()
    held = 0
    for bar in bars:
        if bar.day <= entry_day or bar.day >= today:
            continue
        if bar.low is None or bar.high is None or bar.open is None or bar.close is None:
            return None     # a gap in the bars: do not guess across it
        held += 1
        hit_stop = bar.low <= stop if up else bar.high >= stop
        hit_target = bar.high >= target if up else bar.low <= target
        if hit_stop:
            price = min(bar.open, stop) if up else max(bar.open, stop)
            return f"stop (günlük bar {bar.day:%d.%m})", price
        if hit_target:
            price = max(bar.open, target) if up else min(bar.open, target)
            return f"hedef (günlük bar {bar.day:%d.%m})", price
        if held >= horizon:
            return f"süre doldu ({held} seans, günlük kapanış {bar.day:%d.%m})", bar.close
    return None


async def track_papers(
    ctx: CycleContext, prices: dict[str, PriceContext], flows: dict[str, FlowSummary],
) -> dict[str, int]:
    counts = {"filled": 0, "closed": 0, "expired": 0, "unresolved": 0, "exit_signalled": 0}
    bps = ctx.settings.stock_plan.slippage_bps / 10_000
    active = store.papers(ctx.engine, list(PAPER_ACTIVE))
    bars_by_ticker = load_bars_by_ticker(ctx.engine, sorted({p.ticker for p in active})) if active else {}
    today = ctx.session.session_date
    for paper in active:
        plan: dict[str, Any] = json.loads(paper.plan_json)
        sp = plan.get("stock_plan") or {}
        price = prices.get(paper.ticker)
        spot = _fresh_spot(price, ctx)
        is_stock = paper.instrument == "stock"
        up = paper.direction == "up"
        stop = float(sp.get("stop", 0.0))
        target = float(sp.get("target", 0.0))

        if paper.state == Tracking.PAPER_PENDING.value:
            fill_session = str(plan.get("fill_session") or paper.opportunity_id.split(":", 1)[0])
            if today is not None and fill_session < today.isoformat():
                if store.move_paper(ctx.engine, paper.paper_id, paper.state, state=Tracking.CLOSED_SIMULATED.value,
                                    exit_at=ctx.now, exit_reason=f"dolmadı: tetik {fill_session} seansında gelmedi"):
                    counts["expired"] += 1
                    _event(ctx, paper, "paper_expired", {"reason": f"tetik {fill_session} seansında gelmedi"})
                continue
            if spot is None or price is None or today is None or fill_session != today.isoformat():
                continue
            seen_at = price.spot_fetched_at
            if seen_at is None or seen_at <= inputs.as_utc(paper.created_at):
                continue   # a fill needs a price observed AFTER the recommendation was published
            limit = float(sp.get("chase_limit", 0.0))
            inside = spot <= limit if up else spot >= limit
            above_stop = spot > stop if up else spot < stop
            if not (inside and above_stop):
                continue
            if is_stock:
                qty = int(sp.get("shares", 0))
                fill = round(spot * (1 + bps if up else 1 - bps), 4)
                if qty > 0 and store.move_paper(ctx.engine, paper.paper_id, paper.state,
                                                state=Tracking.PAPER_OPEN.value, entry_at=ctx.now,
                                                entry_price=fill, quantity=qty, last_mark=spot, last_mark_at=ctx.now):
                    counts["filled"] += 1
                    _event(ctx, paper, "paper_fill", {"price": fill, "quantity": qty,
                                                      "basis": f"son fiyat {spot} + {ctx.settings.stock_plan.slippage_bps:g} bps"})
                continue
            view = await _structure_now(paper, plan, ctx)
            if view is None or view.readiness is not Readiness.READY or view.entry_debit is None:
                continue
            if store.move_paper(ctx.engine, paper.paper_id, paper.state, state=Tracking.PAPER_OPEN.value,
                                entry_at=ctx.now, entry_price=view.entry_debit, quantity=view.lots,
                                last_mark=view.exit_credit, last_mark_at=ctx.now):
                counts["filled"] += 1
                _event(ctx, paper, "paper_fill", {"price": view.entry_debit, "quantity": view.lots,
                                                  "basis": "uzun bacak ask, kısa bacak bid, %2 gecikme payı"})
            continue

        # PAPER_OPEN or EXIT_SIGNALLED. An exit signal is sticky: once given, its reason stands
        # and the position closes at the first available price, never re-opened by a bounce.
        entry_day = inputs.as_utc(paper.entry_at).astimezone(_ET).date() if paper.entry_at else None
        reason = paper.exit_reason if paper.state == Tracking.EXIT_SIGNALLED.value and paper.exit_reason else ""
        bar_exit: tuple[str, float] | None = None
        if not reason and spot is not None:
            if (up and spot <= stop) or (not up and spot >= stop):
                reason = "stop"
            elif (up and spot >= target) or (not up and spot <= target):
                reason = "hedef"
        if not reason and spot is None and entry_day is not None:
            bar_exit = _bar_exit(up, stop, target, entry_day, bars_by_ticker.get(paper.ticker, ()),
                                 ctx.settings.stock_plan.horizon_sessions, ctx)
            if bar_exit is not None:
                reason = bar_exit[0]
        flow = flows.get(paper.ticker)
        if not reason and flow is not None:
            d, _ = qualifying_direction(flow, ctx.settings.flow)
            if d is not None and d != paper.direction:
                reason = "tez bozuldu: karşı yönde akış eşikleri geçti"
        if not reason and entry_day is not None and today is not None:
            held = sessions_between(entry_day, today, ctx.settings.calendar)
            if held is not None and held >= ctx.settings.stock_plan.horizon_sessions:
                reason = f"süre doldu ({held} seans)"
        exit_price: float | None = None
        if is_stock:
            if spot is not None:
                exit_price = round(spot * (1 - bps if up else 1 + bps), 4)
                store.move_paper(ctx.engine, paper.paper_id, paper.state, last_mark=spot, last_mark_at=ctx.now)
            elif bar_exit is not None:
                exit_price = round(bar_exit[1] * (1 - bps if up else 1 + bps), 4)
        elif reason or ctx.session.mode is MarketMode.LIVE:
            view = await _structure_now(paper, plan, ctx)
            if (
                view is not None and view.exit_credit is not None
                and view.readiness in (Readiness.READY, Readiness.RISK_BLOCKED)
            ):
                exit_price = view.exit_credit
                store.move_paper(ctx.engine, paper.paper_id, paper.state, last_mark=exit_price, last_mark_at=ctx.now)
        if not reason:
            continue
        if exit_price is None:
            if paper.state != Tracking.EXIT_SIGNALLED.value:
                if store.move_paper(ctx.engine, paper.paper_id, paper.state, state=Tracking.EXIT_SIGNALLED.value,
                                    exit_reason=reason, exit_at=ctx.now):
                    counts["exit_signalled"] += 1
                    _event(ctx, paper, "exit_signalled", {"reason": reason, "note": "çıkış fiyatı yok; çözülmedi"})
            elif paper.exit_at is not None and today is not None:
                signalled_day = inputs.as_utc(paper.exit_at).astimezone(_ET).date()
                if signalled_day < today and store.move_paper(
                    ctx.engine, paper.paper_id, paper.state, state=Tracking.UNRESOLVED.value,
                ):
                    counts["unresolved"] += 1
                    _event(ctx, paper, "unresolved", {
                        "reason": reason, "note": "çıkış sinyalinden sonraki seansta da çıkış fiyatı yok",
                    })
            continue
        entry = float(paper.entry_price or 0.0)
        qty = int(paper.quantity or 0)
        if is_stock:
            pnl = (exit_price - entry) * qty if up else (entry - exit_price) * qty
        else:
            structure = plan.get("structure") or {}
            mult = int(structure.get("multiplier") or 100)
            commission = float(structure.get("commission_usd") or 0.0)
            pnl = (exit_price - entry) * mult * qty - commission * qty
        if store.move_paper(ctx.engine, paper.paper_id, paper.state, state=Tracking.CLOSED_SIMULATED.value,
                            exit_at=ctx.now, exit_price=exit_price, exit_reason=reason, pnl_usd=round(pnl, 2)):
            counts["closed"] += 1
            _event(ctx, paper, "paper_exit", {"reason": reason, "price": exit_price, "pnl_usd": round(pnl, 2)})
    return counts


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


def _session_view(session: Session, now: datetime) -> dict[str, Any]:
    return {
        "mode": session.mode.value,
        "reason": session.reason,
        "et_now": session.et_now.strftime("%Y-%m-%d %H:%M ET"),
        "tr_now": now.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M TR"),
        "session_date": session.session_date.isoformat() if session.session_date else None,
        "close_et": session.close_et.strftime("%H:%M") if session.close_et else None,
        "next_session": session.next_session.isoformat() if session.next_session else None,
    }


async def run_cycle(
    engine: Engine, reader: BoardSignalReader, client: JsonClient, settings: LiveAlphaSettings,
    board: BoardSettings, costs: OptionsAlphaSettings, now: datetime,
) -> store.ScanRow:
    t0 = perf_counter()
    session = classify(now, settings.calendar)
    budget = Budget(
        engine=engine, day=session.et_now.date(), cap=int(board.refresh.daily_request_soft_cap), client=client,
    )
    ctx = CycleContext(engine=engine, reader=reader, client=client, settings=settings, board=board,
                       costs=costs, now=now, session=session, budget=budget)
    scan_id = store.new_id()
    status = "ok"
    snapshot: dict[str, Any] = {"session": _session_view(session, now), "policy_version": settings.policy_version,
                                "profile_sha256": settings.profile_sha256}
    run = inputs.latest_live_run(engine)
    if run is None:
        snapshot.update({"status_text": "akış verisi yok: hiçbir live-* run bulunamadı", "cards": [],
                         "funnel": {}, "flow_run": None})
        status = "degraded"
        row = store.ScanRow(scan_id=scan_id, started_at=now, finished_at=datetime.now(UTC),
                            market_mode=session.mode.value, status=status, run_id=None, snapshot=snapshot)
        store.write_scan(engine, row, settings.policy_version)
        return row
    run_id, last_print_ts = run
    prints = inputs.flow_prints(reader, run_id)
    tickers = tickers_in(prints)
    flows = {t: summarise(t, run_id, prints, settings.flow) for t in tickers}
    paper_tickers = [p.ticker for p in store.papers(engine, list(PAPER_ACTIVE))]
    prices = inputs.price_contexts(
        engine, [*tickers, *paper_tickers], settings.price.benchmark,
        atr_period=board.spot.atr_period, atr_min_sessions=board.spot.atr_min_sessions,
        max_age_seconds=settings.price.spot_max_age_seconds,
    )
    today = session.session_date
    flow_current = (
        today is not None
        and run_id == f"{inputs.LIVE_RUN_PREFIX}{today.isoformat()}"
        and last_print_ts.astimezone(_ET).date() == today
    )
    expiries = inputs.listed_expiries(engine, tickers)

    news: dict[str, NewsCheck] = {}
    for t in tickers:
        qualifies, _ = qualifying_direction(flows[t], settings.flow)
        if qualifies is None:
            news[t] = NewsCheck(ticker=t, state=CheckState.NOT_CHECKED, checked_at=None,
                                detail="akış eşiği geçilmedi; haber çağrısı yapılmadı")
            continue
        news[t] = await uw.news_for(engine, client, budget, t, now,
                                    refresh_seconds=settings.news.refresh_seconds, limit=settings.news.limit)

    decisions = [
        evaluate(flows[t], prices[t], news[t], session, now, settings, flow_current=flow_current)
        for t in tickers
    ]
    cards: list[Card] = []
    for d in decisions:
        structures: tuple[OptionStructureView, ...] = ()
        quotes_state = "bu karar için opsiyon planı gerekmiyor"
        if d.needs_options:
            structures, quotes_state = await option_structures(d, expiries.get(d.ticker, []), ctx)
        cards.append(build_card(d, structures, quotes_state, settings))
    cards = sort_cards(cards)
    rec_ids = record_changes(engine, cards, scan_id, now)
    opened = open_papers_for_new_buys(ctx, cards, rec_ids)
    paper_counts = await track_papers(ctx, prices, flows)

    funnel = {
        "taranan": len(tickers),
        "akış_uygun": sum(1 for t in tickers if qualifying_direction(flows[t], settings.flow)[0] is not None),
        "fiyat_uygun": sum(1 for t in tickers if prices[t].state is CheckState.CHECKED_FOUND),
        "plan": sum(1 for c in cards if c.stock_plan is not None and c.recommendation is not Recommendation.WATCH),
        "giriş_hazır": sum(1 for c in cards if c.readiness is Readiness.READY
                           and c.recommendation in (Recommendation.BUY, Recommendation.BEARISH_SETUP)),
    }
    news_failed = [t for t, n in news.items() if n.state is CheckState.FAILED]
    if news_failed:
        status = "degraded"
    spot_ages = {
        t: int((now - p.spot_fetched_at).total_seconds()) for t, p in prices.items() if p.spot_fetched_at is not None
    }
    snapshot.update({
        "flow_run": run_id,
        "flow_last_print": last_print_ts.isoformat(),
        "flow_current": flow_current,
        "universe": tickers,
        "cards": [to_jsonable(c) | {"rec_id": rec_ids.get(c.opportunity_id)} for c in cards],
        "funnel": funnel,
        "paper": {"opened": opened, **paper_counts},
        "requests": {"reserved": budget.reserved, "refused": budget.refused, "cap": budget.cap},
        "news_failed": news_failed,
        "spot_age_seconds": spot_ages,
        "timing_seconds": round(perf_counter() - t0, 2),
        "status_text": "" if status == "ok" else f"haber kontrol edilemedi: {', '.join(news_failed)}",
    })
    row = store.ScanRow(scan_id=scan_id, started_at=now, finished_at=datetime.now(UTC),
                        market_mode=session.mode.value, status=status, run_id=run_id, snapshot=snapshot)
    store.write_scan(engine, row, settings.policy_version)
    return row


async def live_alpha_loop(
    *,
    database_url: str,
    client_factory: Callable[[], JsonClient] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> None:
    """Run cycles until cancelled (under ``_supervise``)."""
    from uoa_detector.calibration import load_profile
    from uoa_detector.config.credentials import Credentials
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient
    from webapp.board.db import make_engine
    from webapp.board.quota_ledger import ensure_quota_tables
    from webapp.board.refresher import _LIVE_CALIBRATION_PROFILE
    from webapp.board.settings import load_board_settings
    from webapp.board.signals import BoardSignalReader

    settings = load_live_alpha_settings()
    board = load_board_settings()
    costs = load_costs()
    engine = make_engine(database_url)
    store.ensure_live_tables(engine)
    ensure_quota_tables(engine)
    reader = BoardSignalReader(engine=engine)
    if client_factory is not None:
        client = client_factory()
    else:
        profile = load_profile(_LIVE_CALIBRATION_PROFILE)
        client = UnusualWhalesClient(
            api_key=Credentials().require_unusual_whales_api_key(),
            settings=profile.data_sources.unusual_whales,
        )
    now_fn = clock or (lambda: datetime.now(UTC))
    pause = sleep or asyncio.sleep
    cycles = 0
    while True:
        now = now_fn()
        status, detail = "ok", {}
        try:
            row = await run_cycle(engine, reader, client, settings, board, costs, now)
            status = row.status
            detail = {"scan_id": row.scan_id, "cards": len(row.snapshot.get("cards", [])),
                      "requests": row.snapshot.get("requests"), "mode": row.market_mode}
        except Exception as exc:
            _logger.exception("live alpha cycle failed")
            status, detail = "failed", {"error": type(exc).__name__, "message": str(exc)[:300]}
        try:
            store.beat(engine, now, status, detail)
        except Exception:
            _logger.exception("live alpha heartbeat write failed")
        _logger.warning("live alpha cycle: status=%s %s", status, detail)
        LAST_CYCLE.clear()
        LAST_CYCLE.update({
            "at": now.isoformat(timespec="seconds"), "status": status,
            "mode": detail.get("mode"), "cards": detail.get("cards"),
        })
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        mode = classify(now_fn(), settings.calendar).mode
        wait = settings.cycle.live_seconds if mode in (MarketMode.LIVE, MarketMode.PREMARKET) else settings.cycle.closed_seconds
        await pause(wait)
