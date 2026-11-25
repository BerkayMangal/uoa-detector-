"""Replay each newly completed Live Alpha scan into the forward PAPER ledger."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from weakref import WeakSet

from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import select

from uoa_detector.live_alpha.calendar import MarketMode, classify, sessions_between
from uoa_detector.live_alpha.flow import qualifying_direction
from uoa_detector.live_alpha.model import FlowSummary
from uoa_detector.live_alpha.settings import LiveAlphaSettings
from webapp.board.db import session_factory
from webapp.failure_logging import log_failure
from webapp.live_alpha import inputs
from webapp.live_alpha.store import LiveScan, ScanRow
from webapp.paper_calibration import store
from webapp.paper_calibration.model import Position, Quote, Seed, State, Tick, advance, start
from webapp.paper_calibration.reference import Mark, Pair, Reference, advance_reference
from webapp.paper_calibration.settings import Settings, settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from uoa_detector.live_alpha.calendar import Session

_logger = logging.getLogger(__name__)
_RUNNING = threading.Lock()
_READY: WeakSet[Engine] = WeakSet()
_FLOW = TypeAdapter(FlowSummary)
_ACTIVE = frozenset({State.PENDING, State.OPEN, State.EXIT_PENDING})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class StockPlan(BaseModel):
    entry_ref: float
    stop: float
    target: float
    chase_limit: float
    shares: int


class Instrument(BaseModel):
    preferred: str


class SourceCard(BaseModel):
    opportunity_id: str
    ticker: str
    direction: str
    recommendation: str
    readiness: str
    rec_id: str
    path: str
    headline: str
    decision_as_of: AwareDatetime
    policy_version: str
    profile_sha256: str
    instrument: Instrument
    stock_plan: StockPlan | None
    why_today: str = ""
    price_evidence: str = ""
    option_evidence: str = ""
    news_evidence: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.direction == "up" and self.recommendation == "BUY" and self.readiness == "READY"
            and self.instrument.preferred == "stock" and self.stock_plan is not None
        )


class SourcePrice(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    spot: float | None
    source_at: AwareDatetime | None
    session: date | None


class Markout(_Frozen):
    minutes: int
    state: Literal["pending", "priced", "unknown"] = "pending"
    reason: str = ""
    at: AwareDatetime | None = None
    pair: Pair | None = None
    relative_pp: float | None = None


class Case(_Frozen):
    seed: Seed
    source: SourceCard
    source_started_at: AwareDatetime
    initial_pair: Pair | None
    positions: tuple[Position, ...]
    markouts: tuple[Markout, ...]
    reference: Reference | None = None

    @property
    def active(self) -> bool:
        return (
            any(p.state in _ACTIVE for p in self.positions)
            or any(m.state == "pending" for m in self.markouts)
            or (self.reference is not None and self.reference.pending)
        )

    def record(self) -> store.CaseRecord:
        return store.CaseRecord(
            opportunity_id=self.seed.opportunity_id, ticker=self.seed.ticker, session=self.seed.session,
            published_at=self.seed.published_at, document=self.model_dump_json(), active=self.active,
        )


class Action(_Frozen):
    arm: str
    action: str
    reason: str


class Observation(_Frozen):
    at: AwareDatetime
    source_started_at: AwareDatetime
    recovered: bool
    scan_id: str
    market_open: bool
    entry_check: str
    source_recommendation: str | None
    source_readiness: str | None
    source_preferred: str | None
    quote_source: str | None
    quote: Quote | None
    spy: Quote | None
    flow_state: str
    opposite_flow: bool
    actions: tuple[Action, ...]
    markouts: tuple[Markout, ...]
    reference: Reference | None = None


def _pair(stock: Quote | None, spy: Quote | None, cfg: Settings) -> Pair | None:
    if stock is None or spy is None or abs((stock.at - spy.at).total_seconds()) > cfg.markout_pair_skew_seconds:
        return None
    return Pair(stock=stock, spy=spy)


def _markout(item: Markout, case: Case, pair: Pair | None, cfg: Settings, now: datetime, live: LiveAlphaSettings) -> Markout:
    if item.state != "pending":
        return item
    initial = case.initial_pair
    if initial is None:
        return item.model_copy(update={"state": "unknown", "reason": "reference_missing"})
    target = case.seed.published_at + timedelta(minutes=item.minutes)
    end = target + timedelta(seconds=cfg.markout_grace_seconds)
    if classify(target, live.calendar).mode is not MarketMode.LIVE:
        return item.model_copy(update={"state": "unknown", "reason": "outside_session"})
    if (pair is not None and target <= pair.stock.at <= end and target <= pair.spy.at <= end
            and now <= end and now >= target):
        relative = (pair.stock.price / initial.stock.price - pair.spy.price / initial.spy.price) * 100
        return item.model_copy(update={
            "state": "priced", "at": now, "pair": pair, "relative_pp": round(relative, 6),
        })
    if now > end:
        return item.model_copy(update={"state": "unknown", "reason": "paired_quote_missing"})
    return item


class SourceScan:
    def __init__(self, row: ScanRow, live: LiveAlphaSettings, cfg: Settings, *, recovered: bool) -> None:
        self.row = row
        self.live = live
        self.cfg = cfg
        self.recovered = recovered
        self.at = row.finished_at
        self.session = classify(self.at, live.calendar)
        started = classify(row.started_at, live.calendar)
        self.market_open = (
            self.at >= row.started_at and row.market_mode == MarketMode.LIVE
            and self.session.mode is MarketMode.LIVE and started.session_date == self.session.session_date
        )
        self.current_flow = (
            self.market_open and row.snapshot.get("flow_current") is True
            and row.snapshot.get("policy_version") == cfg.source_policy
            and row.snapshot.get("profile_sha256") == live.profile_sha256 == cfg.source_profile_sha256
        )
        cards: dict[str, SourceCard] = {}
        if self.current_flow:
            for raw in row.snapshot.get("cards", ()):
                try:
                    card = SourceCard.model_validate(raw)
                except ValidationError:
                    continue
                identity = f"{self.session.session_date}:{card.ticker}:{card.direction}"
                if (card.opportunity_id == identity and card.policy_version == cfg.source_policy
                        and card.profile_sha256 == live.profile_sha256 and card.decision_as_of == row.started_at):
                    cards[card.opportunity_id] = card
        self.cards = cards

    def _valid_quote(self, at: datetime | None, price: float | None, day: date | None) -> Quote | None:
        if (not self.market_open or day != self.session.session_date or at is None or price is None
                or at > self.row.started_at or not 0 <= (self.at - at).total_seconds() <= self.cfg.max_quote_age_seconds
                or classify(at, self.live.calendar).mode is not MarketMode.LIVE):
            return None
        try:
            return Quote(at=at, price=price)
        except ValidationError:
            return None

    def quotes(
        self, engine: Engine, tickers: tuple[str, ...], *, recovered: bool,
    ) -> dict[str, Quote]:
        values: dict[str, Quote] = {}
        if not self.market_open:
            return values
        needed = set(tickers) | {card.ticker for card in self.cards.values() if card.ready}
        needed.add(self.cfg.benchmark)
        archive = self.row.snapshot.get("price_observations", {})
        absent = needed.difference(archive)
        for ticker in needed.intersection(archive):
            try:
                source = SourcePrice.model_validate(archive[ticker])
            except ValidationError:
                continue
            day = source.session
            if day is None and source.source_at is not None:
                day = classify(source.source_at, self.live.calendar).session_date
            quote = self._valid_quote(source.source_at, source.spot, day)
            if quote is not None:
                values[ticker] = quote
        cached = {} if recovered else inputs.latest_spots(engine, sorted(absent))
        for ticker, read in cached.items():
            day = read.trade_date or classify(read.fetched_at, self.live.calendar).session_date
            quote = self._valid_quote(read.fetched_at, read.spot, day)
            if quote is not None:
                values[ticker] = quote
        return values

    def seed(self, card: SourceCard) -> Seed | None:
        plan = card.stock_plan
        day = self.session.session_date
        if not card.ready or plan is None or day is None:
            return None
        raw = self.row.snapshot.get("news_research_context", {}).get("prices", {}).get(card.ticker, {})
        if not isinstance(raw, dict):
            return None
        try:
            return Seed(
                opportunity_id=card.opportunity_id, ticker=card.ticker, session=day,
                published_at=self.at, source_scan_id=self.row.scan_id, rec_id=card.rec_id,
                source_path=card.path, headline=card.headline, reference=plan.entry_ref,
                stop=plan.stop, target=plan.target, chase=plan.chase_limit, atr=raw.get("atr"), shares=plan.shares,
            )
        except ValidationError:
            return None

    def flow(self, ticker: str) -> str:
        if not self.current_flow:
            return "unavailable"
        try:
            summary = _FLOW.validate_python(self.row.snapshot.get("flow_observations", {}).get(ticker))
        except ValidationError:
            return "unavailable"
        direction, _ = qualifying_direction(summary, self.live.flow)
        return direction or "below_threshold"


def _entry_check(card: SourceCard | None) -> str:
    if card is None:
        return "missing"
    if card.ready:
        return "ready"
    return "watch" if card.recommendation == "WATCH" else "other"


def _event(scan: SourceScan, case: Case, quote: Quote | None, spy: Quote | None, flow: str,
           actions: tuple[Action, ...]) -> store.EventRecord:
    card = scan.cards.get(case.seed.opportunity_id)
    observation = Observation(
        at=scan.at, source_started_at=scan.row.started_at, recovered=scan.recovered, scan_id=scan.row.scan_id,
        market_open=scan.market_open, entry_check=_entry_check(card), quote=quote, spy=spy,
        source_recommendation=card.recommendation if card else None,
        source_readiness=card.readiness if card else None, source_preferred=card.instrument.preferred if card else None,
        quote_source=(("source_scan" if case.seed.ticker in scan.row.snapshot.get("price_observations", {})
                       else "stored_spot") if quote is not None else None),
        flow_state=flow, opposite_flow=flow == "down", actions=actions, markouts=case.markouts,
        reference=case.reference,
    )
    return store.EventRecord(
        opportunity_id=case.seed.opportunity_id, scan_id=scan.row.scan_id, at=scan.at,
        document=observation.model_dump_json(),
    )


def _advance(scan: SourceScan, case: Case, session: Session, quote: Quote | None,
             spy: Quote | None, cfg: Settings) -> tuple[Case, store.EventRecord]:
    day = session.session_date
    assert day is not None
    flow = scan.flow(case.seed.ticker)
    check = _entry_check(scan.cards.get(case.seed.opportunity_id))
    actions = []
    positions = []
    held = sessions_between(case.seed.session, day, scan.live.calendar)
    for arm, position in zip(cfg.arms, case.positions, strict=True):
        tick = Tick(
            at=scan.at, session=day, market_open=scan.market_open, quote=quote, entry_check=check,
            opposite_flow=flow == "down", horizon_reached=scan.market_open and held == cfg.horizon_sessions,
            horizon_missed=held is not None and held > cfg.horizon_sessions,
        )
        step = advance(case.seed, arm, cfg, position, tick)
        positions.append(step.position)
        if step.action != "noop":
            actions.append(Action(arm=arm.key, action=step.action, reason=step.reason))
    pair = _pair(quote, spy, cfg)
    markouts = tuple(_markout(m, case, pair, cfg, scan.at, scan.live) for m in case.markouts)
    reference = case.reference
    if reference is not None and reference.pending:
        mark = Mark(pair=pair, scan_id=scan.row.scan_id, observed_at=scan.at,
                    recovered=scan.recovered) if pair is not None and scan.market_open else None
        reference = advance_reference(reference, case.seed, cfg, scan.live.calendar, scan.at, mark)
    updated = case.model_copy(update={"positions": tuple(positions), "markouts": markouts,
                                      "reference": reference})
    return updated, _event(scan, updated, quote, spy, flow, tuple(actions))


def _ensure(engine: Engine) -> None:
    if engine not in _READY:
        store.ensure(engine)
        _READY.add(engine)


def observe(
    engine: Engine, row: ScanRow, live: LiveAlphaSettings, cfg: Settings, *, recovered: bool = False,
) -> None:
    """Persist one completed scan, using historical snapshots for recovery."""
    scan = SourceScan(row, live, cfg, recovered=recovered)
    day = scan.session.session_date
    if row.finished_at < row.started_at:
        return
    _ensure(engine)
    tickers = store.active_tickers(engine, cfg.study_key) if scan.market_open else ()
    quotes = scan.quotes(engine, tickers, recovered=recovered)
    with store.begin_scan(engine, cfg.study_key, row.scan_id, scan.at) as writer:
        if writer is None or day is None:
            return
        active = writer.active_cases()
        existing = writer.existing(day)
        spy = quotes.get(cfg.benchmark)
        for record in active:
            case = Case.model_validate_json(record.document)
            updated, event = _advance(scan, case, scan.session, quotes.get(case.seed.ticker), spy, cfg)
            if updated != case:
                writer.put(updated.record())
            if scan.market_open or updated != case:
                writer.add_event(event)
        for key, card in scan.cards.items():
            if key in existing:
                continue
            seed = scan.seed(card)
            if seed is None:
                continue
            quote = quotes.get(seed.ticker)
            pair = _pair(quote, spy, cfg)
            positions = tuple(start(seed, arm, cfg) for arm in cfg.arms)
            markouts = tuple(Markout(minutes=minutes) for minutes in cfg.markout_minutes)
            reference = None
            if seed.ticker != cfg.benchmark:
                reference = (Reference(state="unknown", reason="reference_late_enrollment") if scan.recovered
                             else advance_reference(Reference(), seed, cfg, live.calendar, scan.at, None))
            case = Case(seed=seed, source=card, source_started_at=row.started_at,
                        initial_pair=pair, positions=positions, markouts=markouts, reference=reference)
            if pair is None:
                case = case.model_copy(update={"markouts": tuple(
                    m.model_copy(update={"state": "unknown", "reason": "reference_missing"}) for m in markouts
                )})
            actions = tuple(Action(arm=p.arm_key,
                                   action="waiting" if p.state == State.PENDING else "unfilled",
                                   reason="first_plan" if p.state == State.PENDING else "risk_blocked")
                            for p in positions)
            writer.put(case.record())
            writer.add_event(_event(scan, case, quote, spy, scan.flow(seed.ticker), actions))


def _missed_scans(
    engine: Engine, last: datetime, current: ScanRow, limit: int,
) -> tuple[ScanRow, ...]:
    stmt = (
        select(LiveScan)
        .where(
            LiveScan.started_at >= last - timedelta(days=1),
            LiveScan.finished_at > last, LiveScan.finished_at < current.finished_at,
        )
        .order_by(LiveScan.finished_at, LiveScan.scan_id)
        .limit(limit)
    )
    with session_factory(engine)() as session:
        return tuple(ScanRow(
            scan_id=scan.scan_id, started_at=inputs.as_utc(scan.started_at),
            finished_at=inputs.as_utc(scan.finished_at), market_mode=scan.market_mode,
            status=scan.status, run_id=scan.run_id, snapshot=json.loads(scan.snapshot_json),
        ) for scan in session.scalars(stmt))


def _run(engine: Engine, row: ScanRow, live: LiveAlphaSettings, cfg: Settings) -> None:
    if not _RUNNING.acquire(blocking=False):
        return
    try:
        _ensure(engine)
        last = store.latest_observed_at(engine, cfg.study_key)
        if last is None:
            # Write an activation cursor before observing, so storage retry
            # can recover the first scan too. Older source days stay excluded.
            with store.begin_scan(engine, cfg.study_key, f"activation:{row.scan_id}", row.started_at):
                pass
        elif last < row.finished_at:
            missed = _missed_scans(engine, last, row, cfg.recovery_batch)
            for saved in missed:
                observe(engine, saved, live, cfg, recovered=True)
            if len(missed) == cfg.recovery_batch:
                return
        observe(engine, row, live, cfg)
    finally:
        _RUNNING.release()


async def run(engine: Engine, row: ScanRow, live: LiveAlphaSettings) -> None:
    """Isolate storage and timeout failures from the source scanner."""
    try:
        cfg = settings()
        await asyncio.wait_for(asyncio.to_thread(_run, engine, row, live, cfg), cfg.cycle_timeout_seconds)
    except Exception as exc:
        log_failure(_logger, "paper-calibration", exc, "observation")
