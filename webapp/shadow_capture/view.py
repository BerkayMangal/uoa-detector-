"""Small, verified shadow-reference projection; GET never captures or evaluates."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from webapp.opening.model import clock as market_clock
from webapp.shadow_capture import bindings, config, storage
from webapp.shadow_capture.model import Record, aware, digest

PHASES = ('entry', 'm15', 'm60')
PHASE_LABELS = {'entry': 'Giriş referansı', 'm15': '+15 dakika', 'm60': '+60 dakika'}
LONDON = ZoneInfo('Europe/London')
ET = ZoneInfo('America/New_York')
_TICKER = re.compile(r'[A-Z][A-Z0-9.-]*', re.ASCII)


@dataclass(frozen=True)
class Phase:
    phase: str
    label: str
    state: str
    state_label: str
    reason: str = ''
    target_at: datetime | None = None
    deadline: datetime | None = None
    confirmed_at: datetime | None = None
    own_price: float | None = None
    spy_price: float | None = None
    own_trade_at: datetime | None = None
    spy_trade_at: datetime | None = None
    own_pct: float | None = None
    spy_pct: float | None = None
    excess_pp: float | None = None
    reference_quantity: int | None = None
    model_net_usd: float | None = None


@dataclass(frozen=True)
class Row:
    key: str
    ticker: str
    slot: int
    source_at: datetime
    discovered_at: datetime
    available_at: datetime
    rs: bool | None
    news: bool | None
    phases: tuple[Phase, ...]


@dataclass(frozen=True)
class Coverage:
    phase: str
    label: str
    observed: int
    pending: int
    missing: int
    not_applicable: int
    unverified: int


@dataclass(frozen=True)
class Report:
    as_of: datetime
    day: date
    status: str
    reason: str
    method: str = 'shadow_price_v1'
    configured_enabled: bool = True
    activation_at: datetime | None = None
    profile_hash: str | None = None
    implementation_hash: str | None = None
    candidate_count: int | None = None
    reserved_candidates: int | None = None
    ignored_candidates: int = 0
    invalid_records: int = 0
    limited: bool = False
    halt_reason: str | None = None
    cooldown_until: datetime | None = None
    coverage: tuple[Coverage, ...] = ()
    rows: tuple[Row, ...] = ()
    inference: str = 'NOT_PERFORMED'
    winner: None = None


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError('source_clock_missing')
    return aware(datetime.fromisoformat(value))


def _positive(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError('source_price_invalid')
    return float(value)


def _hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _candidate(row: Record, active: Record, day: date, now: datetime) -> None:
    data = row.payload
    slot, ticker = data.get('slot'), data.get('ticker')
    if (type(slot) is not int or not 1 <= slot <= config.settings().candidate_slots
            or not isinstance(ticker, str) or not _TICKER.fullmatch(ticker) or ticker == 'SPY'
            or row.kind != 'candidate' or row.day != day or row.parent_key != active.key
            or data.get('identity') != config.identity() or data.get('activation_key') != active.key
            or data.get('base') is not True
            or any(type(data.get(name)) is not bool for name in ('rs','news') if data.get(name) is not None)
            or not isinstance(data.get('scan_id'), str) or not data['scan_id']
            or not _hex(data.get('snapshot_hash')) or row.confirmed_at is None
            or active.confirmed_at is None):
        raise ValueError('candidate_unverified')
    if (row.key != digest(('candidate_slot', config.settings().version, day.isoformat(), slot))
            or row.unique_owner != digest(('candidate_ticker', config.settings().version, day.isoformat(), ticker))):
        raise ValueError('candidate_identity_unverified')
    source = _time(data.get('scan_finished_at'))
    if not active.confirmed_at <= source <= row.at <= row.confirmed_at <= now:
        raise ValueError('candidate_clock_unverified')


def _pending(phase: str, reason: str = '') -> Phase:
    return Phase(phase, PHASE_LABELS[phase], 'pending',
                 'Giriş kaydı bekleniyor' if phase != 'entry' else 'Kayıt bekleniyor', reason)


def _planned(row: Record, phase: str, now: datetime, entry: Phase | None) -> Phase:
    anchor = row.confirmed_at if phase == 'entry' else entry.confirmed_at if entry and entry.state == 'observed' else None
    if anchor is None:
        return _pending(phase, 'entry_reference_pending')
    wait = timedelta(seconds=config.settings().entry_delay_seconds) if phase == 'entry' else timedelta(minutes=int(phase[1:]))
    due = anchor + wait
    closing = market_clock(anchor).closing
    if closing is None:
        return Phase(phase, PHASE_LABELS[phase], 'unverified', 'Takvim doğrulanamadı', 'calendar_unavailable')
    deadline = min(due + timedelta(seconds=config.settings().window_seconds), closing)
    state = 'not_applicable' if due >= closing else 'missing' if now >= deadline else 'pending'
    label = {'not_applicable':'Seans dışında','missing':'Zamanında ölçülemedi','pending':'Kayıt bekleniyor'}[state]
    return Phase(phase, PHASE_LABELS[phase], state, label, 'target_not_recorded',
                 due.astimezone(LONDON), deadline.astimezone(LONDON))


def _phase(engine: Engine, row: Record, phase: str, now: datetime, entry: Phase | None = None) -> Phase:
    # Import only the read-only verifier, never the collector tick or transport.
    from webapp.shadow_capture.collector import verified_outcome

    key = digest((config.settings().version, 'target', row.key, phase))
    target = storage.get(engine, key, now)
    if target is None:
        if storage.get(engine, key, now, require_receipt=False) is not None:
            return Phase(phase, PHASE_LABELS[phase], 'unverified', 'Kayıt doğrulanamadı', 'target_receipt_unverified')
        outcome_key = digest((config.settings().version, 'outcome', key))
        if storage.get(engine, outcome_key, now, require_receipt=False) is not None:
            return Phase(phase, PHASE_LABELS[phase], 'unverified', 'Kayıt doğrulanamadı', 'outcome_parent_missing')
        return _planned(row, phase, now, entry)
    data = target.payload
    try:
        due, deadline = _time(data.get('target_at')), _time(data.get('deadline'))
        if (target.kind != 'target' or target.parent_key != row.key or target.day != row.day
                or data.get('candidate_key') != row.key or data.get('ticker') != row.payload['ticker']
                or data.get('phase') != phase or data.get('identity') != config.identity()
                or data.get('activation_key') != row.payload['activation_key']
                or type(data.get('eligible')) is not bool or target.confirmed_at is None):
            raise ValueError('target_unverified')
        expected = _planned(row, phase, now, entry)
        if (expected.target_at is None or expected.deadline is None
                or due != expected.target_at or deadline != expected.deadline
                or data['eligible'] != (expected.state != 'not_applicable')):
            raise ValueError('target_schedule_unverified')
    except (ValueError, TypeError):
        return Phase(phase, PHASE_LABELS[phase], 'unverified', 'Kayıt doğrulanamadı', 'target_unverified')
    result = verified_outcome(engine, target, now)
    if result is None:
        outcome_key = digest((config.settings().version, 'outcome', key))
        raw = storage.get(engine, outcome_key, now, require_receipt=False)
        state = 'unverified' if raw is not None else 'missing' if now >= deadline else 'pending'
        label = {'unverified':'Kayıt doğrulanamadı', 'missing':'Zamanında ölçülemedi', 'pending':'Kayıt bekleniyor'}[state]
        return Phase(phase, PHASE_LABELS[phase], state, label, 'outcome_unverified' if raw else '',
                     due.astimezone(LONDON), deadline.astimezone(LONDON))
    status, reason = result.payload.get('status'), result.payload.get('reason')
    if not isinstance(status, str) or status not in ('observed','missing','not_applicable') or not isinstance(reason, str):
        return Phase(phase, PHASE_LABELS[phase], 'unverified', 'Kayıt doğrulanamadı', 'outcome_unverified')
    if status != 'observed':
        return Phase(phase, PHASE_LABELS[phase], status,
                     'Seans dışında' if status == 'not_applicable' else 'Zamanında ölçülemedi',
                     reason, due.astimezone(LONDON), deadline.astimezone(LONDON))
    try:
        own, spy = _positive(result.payload.get('own_price')), _positive(result.payload.get('spy_price'))
        own_at, spy_at = _time(result.payload.get('own_trade_at')), _time(result.payload.get('spy_trade_at'))
        if result.confirmed_at is None or result.confirmed_at > now:
            raise ValueError('outcome_receipt_unverified')
    except (ValueError, TypeError):
        return Phase(phase, PHASE_LABELS[phase], 'unverified', 'Kayıt doğrulanamadı', 'outcome_unverified')
    return Phase(phase, PHASE_LABELS[phase], 'observed', 'Eşlenmiş fiyat kayıtlı', reason,
                 due.astimezone(LONDON), deadline.astimezone(LONDON), result.confirmed_at.astimezone(LONDON),
                 own, spy, own_at.astimezone(LONDON), spy_at.astimezone(LONDON))


def _return(entry: Phase, end: Phase) -> Phase:
    if entry.state != 'observed' or end.state != 'observed':
        return end
    if any(v is None for v in (entry.own_price, entry.spy_price, end.own_price, end.spy_price)):
        return end
    assert entry.own_price is not None and entry.spy_price is not None
    assert end.own_price is not None and end.spy_price is not None
    own = (end.own_price / entry.own_price - 1) * 100
    spy = (end.spy_price / entry.spy_price - 1) * 100
    rate = Decimal(str(config.settings().slippage_bps_per_side)) / Decimal(10000)
    opening, closing = Decimal(str(entry.own_price)), Decimal(str(end.own_price))
    quantity = int((Decimal(str(config.settings().reference_usd)) / (opening*(1+rate))).to_integral_value(rounding=ROUND_FLOOR))
    net = float(quantity*(closing*(1-rate)-opening*(1+rate))) if quantity > 0 else None
    if not all(math.isfinite(v) for v in (own,spy,own-spy)) or (net is not None and not math.isfinite(net)):
        return replace(end, state='unverified', state_label='Hesap doğrulanamadı', reason='nonfinite_return')
    return replace(end, own_pct=own, spy_pct=spy, excess_pp=own-spy,
                   reference_quantity=quantity if quantity > 0 else None, model_net_usd=net)


def build(engine: Engine, now: datetime, day: date | None = None) -> Report:
    at = aware(now)
    selected = day or at.astimezone(ET).date()
    initial = Report(at, selected, 'STORAGE_UNAVAILABLE', 'Yeni aday ölçüm kayıtları henüz okunamıyor.',
                     configured_enabled=config.enabled())
    try:
        if not storage.available(engine):
            return initial
        active = storage.active(engine, at)
        if active is None or active.confirmed_at is None:
            return replace(initial, status='NOT_ACTIVATED', reason='Yeni kayıt hattının etkinleşmesi henüz doğrulanamadı.')
        initial = replace(initial, activation_at=active.confirmed_at.astimezone(LONDON),
                          profile_hash=config.profile_hash(), implementation_hash=config.implementation_hash())
        if selected > at.astimezone(ET).date():
            return replace(initial, status='FUTURE', reason='Seçilen gün henüz gelmedi.')
        if selected < active.confirmed_at.astimezone(ET).date():
            return replace(initial, status='BEFORE_ACTIVATION', reason='Bu gün yeni kayıt hattı devreye girmemişti.')
        cfg = config.settings()
        records = storage.list_events(engine, 'candidate', at, day=selected, parent_key=active.key,
                                      limit=cfg.candidate_slots+1, require_receipt=False)
        reserved = storage.count(engine, 'candidate', at, day=selected)
        rows = []
        invalid = 0
        for record in records[:cfg.candidate_slots]:
            try:
                _candidate(record, active, selected, at)
                if not bindings.valid_candidate(engine, record, active, at):
                    raise ValueError('original_source_unverified')
                entry = _phase(engine, record, 'entry', at)
                later = []
                for phase in PHASES[1:]:
                    measured = _phase(engine, record, phase, at, entry)
                    if entry.state in ('missing','unverified','not_applicable') and measured.state == 'pending':
                        measured = replace(measured, state='missing', state_label='Giriş ölçülemedi',
                                           reason='entry_reference_unavailable')
                    later.append(_return(entry, measured))
                phases = (entry, *later)
                assert record.confirmed_at is not None
                rows.append(Row(record.key, record.payload['ticker'], record.payload['slot'],
                                _time(record.payload['scan_finished_at']).astimezone(LONDON),
                                record.at.astimezone(LONDON), record.confirmed_at.astimezone(LONDON),
                                record.payload.get('rs'), record.payload.get('news'), phases))
            except (ValueError, TypeError, KeyError):
                invalid += 1
        rows.sort(key=lambda r:r.slot)
        coverage = tuple(Coverage(phase, PHASE_LABELS[phase],
                                 *(sum(p.state == state for row in rows for p in row.phases if p.phase == phase)
                                   for state in ('observed','pending','missing','not_applicable','unverified')))
                         for phase in PHASES)
        state = 'COLLECTING' if rows else 'AWAITING_SOURCE'
        reason = 'Yeni adayların giriş, 15 ve 60 dakika fiyatları ayrı izleniyor.' if rows else 'Kayıt hattı açık; yeni, taze ve taban koşullarını sağlayan aday bekleniyor.'
        if invalid and not rows:
            state, reason = 'UNVERIFIED', 'Kaynak aday kayıtları doğrulanamadı; fiyatlar hesaplara katılmadı.'
        halt_reason = None
        cooldown = None
        for halt in storage.list_events(engine, 'halt', at, day=selected, limit=cfg.source_row_limit):
            code, until = halt.payload.get('reason'), halt.payload.get('until')
            if not isinstance(code, str) or not re.fullmatch('[a-z_]{1,60}', code):
                continue
            if until is None:
                halt_reason = code
                state, reason = 'HALTED', 'Kaynak güvenlik nedeniyle bu gün için durduruldu; yeni fiyat isteği gönderilmez.'
            else:
                try:
                    end = _time(until)
                    if end > at:
                        cooldown = max(cooldown or end, end)
                except ValueError:
                    continue
        if cooldown is not None and halt_reason is None:
            state, reason = 'COOLDOWN', 'Kaynak hız sınırı nedeniyle kısa bekleme uygulanıyor.'
        if not initial.configured_enabled:
            state, reason = 'DISABLED', 'Yeni fiyat toplama kapalı; kaydedilmiş sonuçlar ayrı gösteriliyor.'
        return replace(initial, status=state, reason=reason, candidate_count=len(rows), reserved_candidates=reserved,
                       ignored_candidates=max(0,reserved-len(records)), invalid_records=invalid,
                       limited=len(records)>cfg.candidate_slots, rows=tuple(rows), coverage=coverage,
                       halt_reason=halt_reason, cooldown_until=cooldown.astimezone(LONDON) if cooldown else None)
    except Exception:
        return initial
