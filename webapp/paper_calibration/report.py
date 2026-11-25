"""Read-only dashboard projections for the stock PAPER experiment."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from math import fsum
from statistics import mean
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from uoa_detector.live_alpha.calendar import close_time
from uoa_detector.live_alpha.model import Path as SignalPath
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.paper_calibration import store
from webapp.paper_calibration.model import Arm, Position, State
from webapp.paper_calibration.observer import Case, Markout, Observation
from webapp.paper_calibration.reference import COSTS_BPS, Mark, Reference, net_usd
from webapp.paper_calibration.settings import Settings, settings

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.engine import Engine

PAGE_SIZE = 20
_PAGE_EVENTS = 100
_LONDON = ZoneInfo("Europe/London")
_STATE = {
    State.PENDING: "Giriş bekliyor",
    State.OPEN: "Açık",
    State.EXIT_PENDING: "Çıkış fiyatı bekliyor",
    State.CLOSED: "Kapandı",
    State.UNFILLED: "Dolmadı",
    State.UNKNOWN: "Sonuç belirsiz",
}
_ENTRY = {
    "ready": "AL / Hazır · hisse", "watch": "İZLE",
    "other": "Başka karar / araç", "missing": "Güncel kart yok",
}
_ACTION = {
    "waiting": "Bekliyor", "filled": "Giriş", "marked": "Fiyat gözlendi", "exit_pending": "Çıkış bekliyor",
    "closed": "Çıkış", "unfilled": "Dolmadı", "unknown": "Belirsiz",
}
_REASON = {
    "first_plan": "İlk uygun AL planı kaydedildi",
    "entry_watch": "Güncel sinyal İZLE",
    "entry_other": "Güncel AL / Hazır hisse koşulu eksik",
    "entry_missing": "Güncel AL kartı yok",
    "entry_confirmed": "Aynı taramada AL teyidi",
    "entry_legacy": "İlk AL planının giriş aralığı",
    "entry_price_outside_plan": "Fiyat ilk planın giriş sınırları dışında",
    "quote_prepublication": "Fiyat ilk plan yayınlanmadan önce gözlendi",
    "quote_unavailable": "Geçerli seans fiyatı gözlenmedi",
    "quote_observed": "Taze fiyat",
    "quote_unchanged": "Yeni fiyat gözlemi gelmedi",
    "exit_quote_unavailable": "Çıkış seansında geçerli fiyat gözlenmedi",
    "stop": "Stop",
    "target": "Hedef",
    "opposite_flow": "Ters opsiyon akışı",
    "horizon": "İzleme süresi doldu",
    "horizon_session_unobserved": "Süre dolum seansı gözlenemedi",
    "risk_blocked": "Bir hisse planlanan riske sığmadı",
    "market_closed": "Normal seans kapalı",
    "session_expired": "İlk seans sona erdi",
    "reference_missing": "Sinyal anında eşleşen hisse ve SPY fiyatı eksik",
    "outside_session": "Ölçüm saati normal seansın dışında",
    "paired_quote_missing": "Ölçüm penceresinde eşleşen hisse ve SPY fiyatı eksik",
    "reference_late_enrollment": "İlk kayıt kurtarma sırasında görüldü",
    "reference_outside_session": "60 dakika ölçümü normal seansa sığmadı",
    "reference_entry_missing": "Giriş penceresinde eşleşen hisse ve SPY fiyatı eksik",
    "reference_exit_missing": "60 dakika penceresinde eşleşen hisse ve SPY fiyatı eksik",
    "reference_notional": "Ayrılan bütçeye bir hisse sığmadı",
    "reference_invalid_return": "Getiri hesabı doğrulanamadı",
}
_FLOW = {"up": "Yukarı", "down": "Aşağı", "below_threshold": "Eşik altında", "unavailable": "Akış verisi yok"}
_REFERENCE_STATES = {
    "entry_pending": "Giriş fiyatı bekleniyor", "exit_pending": "60 dakika fiyatı bekleniyor",
    "priced": "Fiyat çifti tamam", "unknown": "Eksik ölçüm",
}
PRIMARY_COST_BPS = 10


class CaseFilter(StrEnum):
    ALL = "all"
    OPEN = "open"
    PENDING = "pending"
    EXIT_PENDING = "exit_pending"
    CLOSED = "closed"
    UNKNOWN = "unknown"


_FILTER_LABELS = {
    CaseFilter.ALL: "Tümü", CaseFilter.OPEN: "Açık", CaseFilter.PENDING: "Giriş bekliyor",
    CaseFilter.EXIT_PENDING: "Çıkış fiyatı bekliyor", CaseFilter.CLOSED: "Tamamlandı",
    CaseFilter.UNKNOWN: "Eksik ölçüm",
}


_POSITION_FILTERS = {
    State.OPEN: CaseFilter.OPEN,
    State.PENDING: CaseFilter.PENDING,
    State.EXIT_PENDING: CaseFilter.EXIT_PENDING,
    State.UNKNOWN: CaseFilter.UNKNOWN,
}


def _states(case: Case) -> frozenset[CaseFilter]:
    states = {_POSITION_FILTERS[row.state] for row in case.positions if row.state in _POSITION_FILTERS}
    if not case.active:
        states.add(CaseFilter.CLOSED)
    if any(item.state == "unknown" for item in case.markouts) or (
        case.reference is not None and case.reference.state == "unknown"
    ):
        states.add(CaseFilter.UNKNOWN)
    return frozenset(states)


@dataclass(frozen=True)
class ArmView:
    key: str
    label: str
    stop_atr: float
    control: str | None
    filled: int
    closed: int
    wins: int
    pending: int
    open: int
    exit_pending: int
    unfilled: int
    unknown: int
    net: float | None
    mean_r: float | None
    mean_mae_r: float | None
    mean_mfe_r: float | None
    paired: int
    paired_sessions: int
    paired_delta_r: float | None
    delta_bar_pct: float | None = None


@dataclass(frozen=True)
class MarkSummary:
    label: str
    priced: int
    total: int
    positive: int
    mean_pp: float | None


@dataclass(frozen=True)
class MarkView:
    label: str
    state: str
    state_label: str
    at: str | None
    price: float | None
    spy: float | None
    relative_pp: float | None
    reason: str | None


@dataclass(frozen=True)
class ReferenceMarkView:
    at: str | None
    stock_at: str | None
    spy_at: str | None
    stock: float
    spy: float


@dataclass(frozen=True)
class ReferenceView:
    state: str
    label: str
    reason: str | None
    entry: ReferenceMarkView | None
    exit: ReferenceMarkView | None
    net: float | None


@dataclass(frozen=True)
class BaselineCost:
    cost_bps: int
    p2_usd: float
    random_usd: float
    spy_usd: float


@dataclass(frozen=True)
class BaselineView:
    sessions: int
    complete_sessions: int
    candidates: int
    priced_candidates: int
    p2_candidates: int
    p2_entries: int
    p2_priced: int
    p2_pending: int
    p2_missing: int
    paired_p2: int
    notional: float
    costs: tuple[BaselineCost, ...]

    @property
    def primary_cost(self) -> BaselineCost | None:
        return next((cost for cost in self.costs if cost.cost_bps == PRIMARY_COST_BPS), None)


@dataclass(frozen=True)
class PositionView:
    arm: str
    label: str
    state: str
    state_label: str
    stop: float
    target: float
    quantity: int
    planned_risk: float
    fill_risk: float | None
    entry_at: str | None
    entry_price: float | None
    exit_at: str | None
    exit_price: float | None
    exit_reason: str | None
    pnl: float | None
    net_r: float | None
    mae_r: float | None
    mfe_r: float | None
    last_price: float | None


@dataclass(frozen=True)
class CaseView:
    id: str
    ticker: str
    published_at: str | None
    source_path: str
    headline: str
    reference: float
    original_stop: float
    target: float
    chase: float
    atr: float
    state: str
    state_label: str
    positions: tuple[PositionView, ...]
    markouts: tuple[MarkView, ...]
    hold_reference: ReferenceView | None


@dataclass(frozen=True)
class ActionView:
    arm: str
    label: str
    action: str
    reason: str


@dataclass(frozen=True)
class EventView:
    at: str | None
    scan_id: str
    entry_check: str
    quote_at: str | None
    quote_price: float | None
    spy_price: float | None
    opposite_flow: bool
    flow: str
    recovered: bool
    actions: tuple[ActionView, ...]


@dataclass(frozen=True)
class Report:
    ready: bool
    latest_at: str | None
    version: str
    total: int
    cohort_total: int
    sessions: int
    open_cases: int
    pending_cases: int
    exit_quote_cases: int
    unknown_cases: int
    ticker: str
    filter_state: str
    state_options: tuple[tuple[str, str], ...]
    filter_day: str
    filtered: bool
    page: int
    has_prev: bool
    has_next: bool
    arms: tuple[ArmView, ...]
    markouts: tuple[MarkSummary, ...]
    p2: BaselineView
    cases: tuple[CaseView, ...]
    detail: CaseView | None
    events: tuple[EventView, ...]
    events_truncated: bool
    detail_missing: bool


def _clock(value: datetime | None) -> str | None:
    return value.astimezone(_LONDON).strftime("%d.%m.%Y %H:%M:%S") if value else None


def _mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _resolved(position: Position) -> float | None:
    return 0.0 if position.state is State.UNFILLED else position.pnl_usd


def _arm_view(arm: Arm, control: Arm | None, cases: tuple[Case, ...], cfg: Settings) -> ArmView:
    per_case = [{row.arm_key: row for row in case.positions} for case in cases]
    rows = [case[arm.key] for case in per_case]
    closed = [row for row in rows if row.state is State.CLOSED]
    pnls = [row.pnl_usd for row in closed if row.pnl_usd is not None]
    paired_delta = []
    paired_days = set()
    if control is not None:
        for source, case in zip(cases, per_case, strict=True):
            newer, older = _resolved(case[arm.key]), _resolved(case[control.key])
            if newer is not None and older is not None:
                paired_delta.append((newer - older) / cfg.risk_usd)
                paired_days.add(source.seed.session)
    return ArmView(
        arm.key, arm.label, arm.stop_atr, control.label if control else None,
        sum(row.entry_fill is not None for row in rows), len(closed), sum(pnl > 0 for pnl in pnls),
        sum(row.state is State.PENDING for row in rows), sum(row.state is State.OPEN for row in rows),
        sum(row.state is State.EXIT_PENDING for row in rows),
        sum(row.state is State.UNFILLED for row in rows), sum(row.state is State.UNKNOWN for row in rows),
        round(sum(pnls), 2) if pnls else None, _mean([row.net_r for row in closed if row.net_r is not None]),
        _mean([row.mae_r for row in closed if row.mae_r is not None]),
        _mean([row.mfe_r for row in closed if row.mfe_r is not None]),
        len(paired_delta), len(paired_days), _mean(paired_delta),
    )


def _mark_view(m: Markout) -> MarkView:
    return MarkView(
        f"+{m.minutes} dk", m.state, {"pending": "Bekliyor", "priced": "Ölçüldü", "unknown": "Bilinmiyor"}[m.state],
        _clock(m.at), m.pair.stock.price if m.pair else None, m.pair.spy.price if m.pair else None,
        m.relative_pp, _REASON.get(m.reason),
    )


def _reference_mark(mark: Mark | None) -> ReferenceMarkView | None:
    if mark is None:
        return None
    return ReferenceMarkView(
        _clock(mark.observed_at), _clock(mark.pair.stock.at), _clock(mark.pair.spy.at),
        mark.pair.stock.price, mark.pair.spy.price,
    )


def _reference_view(ref: Reference | None, notional: float) -> ReferenceView | None:
    if ref is None:
        return None
    net = None
    if ref.entry is not None and ref.exit is not None:
        net = net_usd(ref.entry.pair.stock, ref.exit.pair.stock, notional, PRIMARY_COST_BPS)
    return ReferenceView(ref.state, _REFERENCE_STATES[ref.state], _REASON.get(ref.reason or ""),
                         _reference_mark(ref.entry), _reference_mark(ref.exit), net)


def _p2(case: Case) -> bool:
    return case.seed.source_path == SignalPath.P2_MARKET_RELATIVE.value


def _priced(case: Case) -> bool:
    return case.reference is not None and case.reference.state == "priced"


def _reference_nets(case: Case, notional: float, cost: int) -> tuple[float, float]:
    ref = case.reference
    assert ref is not None and ref.entry is not None and ref.exit is not None
    left, right = ref.entry.pair, ref.exit.pair
    return net_usd(left.stock, right.stock, notional, cost), net_usd(left.spy, right.spy, notional, cost)


def _baseline(cases: tuple[Case, ...], cfg: Settings, at: datetime | None, day: date | None) -> BaselineView:
    grouped: dict[date, list[Case]] = defaultdict(list)
    for case in cases:
        if case.seed.ticker != cfg.benchmark and (day is None or case.seed.session == day):
            grouped[case.seed.session].append(case)
    cohorts = {session: roster for session, roster in grouped.items()
               if any(case.reference is not None for case in roster)}
    p2 = [case.reference for roster in cohorts.values() for case in roster if _p2(case)]
    calendar = load_live_alpha_settings().calendar
    zone = ZoneInfo(calendar.timezone)
    complete = [roster for session, roster in cohorts.items() if at is not None
                and at >= datetime.combine(session, close_time(session, calendar), zone)
                and all(_priced(case) for case in roster)]
    costs = []
    for cost in COSTS_BPS if complete else ():
        p2_nets: list[float] = []
        random_nets: list[float] = []
        spy_nets: list[float] = []
        for roster in complete:
            records = [(case, *_reference_nets(case, cfg.max_notional_usd, cost)) for case in roster]
            eligible = [(stock, spy) for case, stock, spy in records if _p2(case)]
            p2_nets.extend(stock for stock, _ in eligible)
            spy_nets.extend(spy for _, spy in eligible)
            random_nets.append(len(eligible) * mean(stock for _, stock, _ in records))
        costs.append(BaselineCost(cost, fsum(p2_nets), fsum(random_nets), fsum(spy_nets)))
    return BaselineView(
        sessions=len(cohorts), complete_sessions=len(complete),
        candidates=sum(map(len, cohorts.values())), p2_candidates=len(p2),
        priced_candidates=sum(_priced(case) for roster in cohorts.values() for case in roster),
        p2_entries=sum(ref is not None and ref.entry is not None for ref in p2),
        p2_priced=sum(ref is not None and ref.state == "priced" for ref in p2),
        p2_pending=sum(ref is not None and ref.pending for ref in p2),
        p2_missing=sum(ref is None or ref.state == "unknown" for ref in p2),
        paired_p2=sum(_p2(case) for roster in complete for case in roster),
        notional=cfg.max_notional_usd, costs=tuple(costs),
    )


def _case_view(case: Case, arms: dict[str, Arm], notional: float) -> CaseView:
    seed = case.seed
    active_states = _states(case)
    priority = (CaseFilter.OPEN, CaseFilter.EXIT_PENDING, CaseFilter.PENDING, CaseFilter.UNKNOWN, CaseFilter.CLOSED)
    status = next((state for state in priority if state in active_states), None)
    return CaseView(
        seed.opportunity_id, seed.ticker, _clock(seed.published_at), seed.source_path, seed.headline,
        seed.reference, seed.stop, seed.target, seed.chase, seed.atr, status.value if status else "tracking",
        _FILTER_LABELS[status] if status else "Ölçüm bekliyor",
        tuple(PositionView(
            p.arm_key, arms[p.arm_key].label, p.state, _STATE[p.state], p.stop, p.target, p.quantity,
            p.planned_risk, p.fill_risk, _clock(p.entry_at), p.entry_fill, _clock(p.exit_at), p.exit_fill,
            _REASON.get(p.exit_reason or ""), p.pnl_usd, p.net_r, p.mae_r, p.mfe_r,
            p.last_quote.price if p.last_quote else None,
        ) for p in case.positions),
        tuple(_mark_view(mark) for mark in case.markouts),
        _reference_view(case.reference, notional),
    )


def _event_view(record: store.EventRecord, arms: dict[str, Arm]) -> EventView:
    e = Observation.model_validate_json(record.document)
    return EventView(
        _clock(e.at), e.scan_id, _ENTRY[e.entry_check], _clock(e.quote.at if e.quote else None),
        e.quote.price if e.quote else None, e.spy.price if e.spy else None, e.opposite_flow, _FLOW[e.flow_state], e.recovered,
        tuple(ActionView(a.arm, arms[a.arm].label, _ACTION[a.action], _REASON[a.reason]) for a in e.actions),
    )


def build(
    engine: Engine, ticker: str, page: int, opportunity: str,
    *, state: CaseFilter = CaseFilter.ALL, day: date | None = None,
) -> Report:
    cfg = settings()
    ticker = ticker.strip().upper()
    data = store.report_inputs(engine, cfg.study_key, ticker or None, page, PAGE_SIZE)
    cases = tuple(Case.model_validate_json(row.document) for row in data.all_cases)
    by_id = {case.seed.opportunity_id: case for case in cases}
    statuses = {case.seed.opportunity_id: _states(case) for case in cases}
    if state is CaseFilter.ALL and day is None:
        page_rows = tuple(by_id[row.opportunity_id] for row in data.cases)
        total = data.total
    else:
        filtered = tuple(case for case in cases if
                         (not ticker or case.seed.ticker == ticker) and
                         (day is None or case.seed.session == day) and
                         (state is CaseFilter.ALL or state in statuses[case.seed.opportunity_id]))
        offset = (page - 1) * PAGE_SIZE
        page_rows, total = filtered[offset:offset + PAGE_SIZE], len(filtered)
    arms = {arm.key: arm for arm in cfg.arms}
    original = next(arm for arm in cfg.arms if not arm.confirm_entry)
    confirmed = next(arm for arm in cfg.arms if arm.confirm_entry and arm.stop_atr == original.stop_atr)
    arm_views = tuple(_arm_view(arm, None if arm == original else original if arm == confirmed else confirmed,
                               cases, cfg) for arm in cfg.arms)
    max_delta = max((abs(a.paired_delta_r) for a in arm_views if a.paired_delta_r is not None), default=0.0)
    arm_views = tuple(
        replace(a, delta_bar_pct=(100 * abs(a.paired_delta_r) / max_delta if max_delta else 0.0))
        if a.paired_delta_r is not None else a
        for a in arm_views
    )
    summaries = []
    for horizon in cfg.markout_minutes:
        pp = [item.relative_pp for case in cases for item in case.markouts
              if item.minutes == horizon and item.relative_pp is not None]
        summaries.append(MarkSummary(f"+{horizon} dk", len(pp), len(cases), sum(p > 0 for p in pp), _mean(pp)))
    record, events = store.detail_inputs(engine, cfg.study_key, opportunity) if opportunity else (None, ())
    detail = (_case_view(Case.model_validate_json(record.document), arms, cfg.max_notional_usd)
              if record is not None else None)
    return Report(
        ready=data.ready, latest_at=_clock(data.latest_at), version=cfg.version,
        total=total, cohort_total=len(cases), sessions=len({c.seed.session for c in cases}),
        open_cases=sum(CaseFilter.OPEN in s for s in statuses.values()),
        pending_cases=sum(CaseFilter.PENDING in s for s in statuses.values()),
        exit_quote_cases=sum(CaseFilter.EXIT_PENDING in s for s in statuses.values()),
        unknown_cases=sum(CaseFilter.UNKNOWN in s for s in statuses.values()),
        ticker=ticker, filter_state=state.value,
        state_options=tuple((key.value, label) for key, label in _FILTER_LABELS.items()),
        filter_day=day.isoformat() if day else "", filtered=bool(ticker or day or state is not CaseFilter.ALL),
        page=page, has_prev=page > 1, has_next=page * PAGE_SIZE < total,
        arms=arm_views, markouts=tuple(summaries),
        p2=_baseline(cases, cfg, data.latest_at, day),
        cases=tuple(_case_view(row, arms, cfg.max_notional_usd) for row in page_rows),
        detail=detail, events=tuple(_event_view(e, arms) for e in events[-_PAGE_EVENTS:]),
        events_truncated=len(events) > _PAGE_EVENTS, detail_missing=bool(opportunity and detail is None),
    )
