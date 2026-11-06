"""New source and paired wire captures remain separate, bounded and read-only."""
import asyncio
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import event, update
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables
from webapp.live_alpha import store as live
from webapp.news_research import storage as news_storage
from webapp.shadow_capture import bindings, collector, config, storage, view
from webapp.shadow_capture.model import digest

from tests.unit.test_news_adapter import NOW, news, scan, snapshot
from tests.unit.test_shadow_collector import Client, Clock


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv('SHADOW_CAPTURE_ENABLED', 'true')
    e = make_engine(f"sqlite:///{tmp_path / 'shadow-view.db'}")
    live.ensure_live_tables(e)
    news_storage.ensure(e)
    storage.ensure(e)
    ensure_quota_tables(e)
    storage.activate(e, lambda: NOW - timedelta(hours=1))
    yield e
    collector._HALTS.clear()
    collector._COOLDOWNS.clear()
    e.dispose()


@pytest.fixture
def admitted(engine):
    scan(engine)
    news(engine)
    at = NOW + timedelta(seconds=2)
    assert bindings.discover(engine, at, clock=lambda: at)['admitted'] == 1
    return storage.list_events(engine, 'candidate', at, day=NOW.date())[0]


def capture(engine, seconds, prices=None):
    clock = Clock(NOW + timedelta(seconds=seconds))
    client = Client(clock)
    if prices is not None:
        client.prices = prices
    asyncio.run(collector.tick(engine, client, clock(), clock=clock))
    return clock()


def phase(report, name):
    return next(p for p in report.rows[0].phases if p.phase == name)


def test_absent_storage_get_is_not_an_empty_zero_result_and_creates_no_tables(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'absent.db'}")
    writes = []
    event.listen(e, 'before_cursor_execute', lambda _c, _cur, sql, *_a: writes.append(sql) if sql.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE')) else None)
    r = view.build(e, NOW)
    assert r.status == 'STORAGE_UNAVAILABLE' and r.candidate_count is None
    assert not writes and r.winner is None and r.inference == 'NOT_PERFORMED'
    e.dispose()


def test_unactivated_storage_does_not_activate_on_get(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'not-active.db'}")
    storage.ensure(e)
    r = view.build(e, NOW)
    assert r.status == 'NOT_ACTIVATED' and r.activation_at is None and r.candidate_count is None
    assert storage.active(e, NOW) is None
    e.dispose()


def test_activated_empty_is_waiting_for_real_source_and_not_success_or_loss(engine):
    r = view.build(engine, NOW)
    assert r.status == 'AWAITING_SOURCE' and r.candidate_count == 0 and not r.rows
    assert r.activation_at == NOW - timedelta(hours=1)
    assert all(c.observed == c.missing == c.pending == 0 for c in r.coverage)
    assert r.winner is None and r.inference == 'NOT_PERFORMED'


def test_admitted_real_source_flags_and_separate_pending_phases(engine, admitted):
    r = view.build(engine, NOW + timedelta(seconds=3))
    assert r.status == 'COLLECTING' and r.candidate_count == 1
    assert r.rows[0].ticker == 'NVDA' and r.rows[0].rs is True and r.rows[0].news is True
    assert tuple(p.phase for p in r.rows[0].phases) == ('entry','m15','m60')
    assert all(p.state == 'pending' and p.own_pct is None for p in r.rows[0].phases)
    assert phase(r,'entry').target_at == admitted.confirmed_at + timedelta(seconds=60)


def test_actual_source_to_entry_and_m15_capture_has_independent_reference_arithmetic(engine, admitted):
    capture(engine, 64)
    at = capture(engine, 966, {'NVDA':103., 'SPY':510.})
    r = view.build(engine, at)
    a, b, later = (phase(r, name) for name in ('entry','m15','m60'))
    assert a.state == b.state == 'observed' and later.state == 'pending'
    assert a.own_pct is None and a.own_price == 100
    assert b.own_pct == pytest.approx(3) and b.spy_pct == pytest.approx(2)
    assert b.excess_pp == pytest.approx(1) and b.reference_quantity == 24
    assert b.model_net_usd == pytest.approx(69.564)
    assert r.coverage[1].observed == 1 and r.coverage[2].observed == 0
    assert r.winner is None and r.inference == 'NOT_PERFORMED'


def test_m15_and_m60_remain_separate_and_flat_prices_still_have_cost(engine, admitted):
    capture(engine, 64)
    capture(engine, 966, {'NVDA':103., 'SPY':510.})
    at = capture(engine, 3666, {'NVDA':100., 'SPY':500.})
    r = view.build(engine, at)
    assert phase(r,'m15').own_pct == pytest.approx(3)
    assert phase(r,'m60').own_pct == phase(r,'m60').excess_pp == pytest.approx(0)
    assert phase(r,'m60').model_net_usd == pytest.approx(-2.4)


def test_negative_reference_and_benchmark_excess_have_the_correct_sign(engine, admitted):
    capture(engine, 64)
    at = capture(engine, 966, {'NVDA':98., 'SPY':495.})
    measured = phase(view.build(engine, at), 'm15')
    assert measured.own_pct == pytest.approx(-2) and measured.spy_pct == pytest.approx(-1)
    assert measured.excess_pp == pytest.approx(-1)
    assert measured.model_net_usd == pytest.approx(-50.376)


def test_corrupted_packet_receipt_cannot_be_presented_as_a_measured_return(engine, admitted):
    capture(engine,64)
    at = capture(engine,966,{'NVDA':103.,'SPY':510.})
    packets = storage.list_events(engine,'packet',at,day=NOW.date())
    assert packets
    with engine.begin() as conn:
        conn.execute(update(storage.ShadowReceipt).where(storage.ShadowReceipt.parent_key==packets[0].key).values(checksum='bad'))
    r = view.build(engine,at)
    assert phase(r,'entry').state == 'unverified'
    assert phase(r,'m15').own_pct is None and phase(r,'m15').model_net_usd is None


def test_receipted_orphan_numbers_are_unverified_without_target_or_raw_quotes(engine, admitted):
    at = NOW + timedelta(seconds=64)
    target_key = digest((config.settings().version, 'target', admitted.key, 'entry'))
    storage.put(engine, key=digest((config.settings().version, 'outcome', target_key)),
                kind='outcome', at=at, day=NOW.date(), parent_key=target_key,
                payload={'identity':config.identity(), 'activation_key':admitted.parent_key,
                         'candidate_key':admitted.key, 'ticker':'NVDA', 'phase':'entry',
                         'status':'observed', 'reason':'', 'own_price':100., 'spy_price':500.,
                         'own_trade_at':at.isoformat(), 'spy_trade_at':at.isoformat()},
                clock=lambda:at)
    measured = phase(view.build(engine, at), 'entry')
    assert measured.state == 'unverified' and measured.reason == 'outcome_parent_missing'
    assert measured.own_price is None and measured.own_pct is None


def test_receipted_but_misdated_target_is_unknown_instead_of_plausible_pending(engine, admitted):
    at = NOW + timedelta(seconds=3)
    due = at + timedelta(minutes=30)
    key = digest((config.settings().version, 'target', admitted.key, 'entry'))
    storage.put(engine, key=key, kind='target', at=at, day=NOW.date(), parent_key=admitted.key,
                payload={'identity':config.identity(), 'activation_key':admitted.parent_key,
                         'candidate_key':admitted.key, 'ticker':'NVDA', 'phase':'entry',
                         'target_at':due.isoformat(), 'deadline':(due+timedelta(minutes=5)).isoformat(),
                         'eligible':True, 'reason':''}, clock=lambda:at)
    measured = phase(view.build(engine, at), 'entry')
    assert measured.state == 'unverified' and measured.own_price is None


def test_corrupted_candidate_receipt_is_not_a_qualified_empty_success(engine, admitted):
    with engine.begin() as conn:
        conn.execute(update(storage.ShadowReceipt).where(storage.ShadowReceipt.parent_key==admitted.key).values(checksum='bad'))
    r = view.build(engine,NOW+timedelta(seconds=3))
    assert r.status == 'UNVERIFIED' and r.invalid_records == 1
    assert r.candidate_count == 0 and r.reserved_candidates == 1


def test_original_source_corruption_invalidates_shadow_parent_without_looking_at_new_prices(engine, admitted):
    with engine.begin() as conn:
        conn.execute(update(live.LiveScan).where(live.LiveScan.scan_id=='first').values(snapshot_json='{}'))
    r = view.build(engine,NOW+timedelta(seconds=3))
    assert r.status == 'UNVERIFIED' and r.invalid_records == 1 and not r.rows


def test_missing_entry_never_becomes_pending_exits_forever_or_zero_return(engine, admitted):
    r = view.build(engine, NOW+timedelta(minutes=10))
    assert all(p.state == 'missing' for p in r.rows[0].phases)
    assert all(p.own_pct is None and p.model_net_usd is None for p in r.rows[0].phases)


def test_unknown_original_auxiliary_flags_stay_unknown(engine):
    data = snapshot()
    data['news_observations']['NVDA']['state']='failed'
    data['news_research_context']['prices']['NVDA']['benchmark_move']=123
    scan(engine,data=data)
    at = NOW+timedelta(seconds=2)
    assert bindings.discover(engine,at,clock=lambda:at)['admitted']==1
    row = view.build(engine,at).rows[0]
    assert row.rs is None and row.news is None


def test_get_view_does_no_writes_or_capture_work_even_for_verified_outcomes(engine, admitted, monkeypatch):
    capture(engine,64)
    at = capture(engine,966,{'NVDA':103.,'SPY':510.})
    writes=[]
    event.listen(engine,'before_cursor_execute',lambda _c,_cur,sql,*_a:writes.append(sql) if sql.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE','ALTER','DROP')) else None)
    async def forbidden(*_a,**_k):
        pytest.fail('GET ran capture')
    monkeypatch.setattr(collector,'tick',forbidden)
    r=view.build(engine,at)
    assert r.rows and not writes
    assert phase(r,'m15').state=='observed'


def test_prior_and_future_days_do_not_become_zero_observations(engine):
    assert view.build(engine,NOW,NOW.date()-timedelta(days=1)).status=='BEFORE_ACTIVATION'
    assert view.build(engine,NOW,NOW.date()+timedelta(days=1)).status=='FUTURE'


def test_other_activation_candidates_are_not_merged(engine,admitted,monkeypatch):
    monkeypatch.setattr(config,'implementation_hash',lambda:'b'*64)
    at=NOW+timedelta(seconds=4)
    storage.activate(engine,lambda:at)
    r=view.build(engine,at)
    assert not r.rows and r.candidate_count==0 and r.ignored_candidates==1


def test_halt_and_cooldown_remain_explicit_without_sending_a_request(engine):
    at=NOW
    storage.put(engine,key=digest(('test','cooldown')),kind='halt',at=at,day=at.date(),
                payload={'identity':config.identity(),'reason':'rate_limited','until':(at+timedelta(seconds=60)).isoformat()},clock=lambda:at)
    r=view.build(engine,at)
    assert r.status=='COOLDOWN' and r.cooldown_until==at+timedelta(seconds=60)
    storage.put(engine,key=digest(('test','halt')),kind='halt',at=at,day=at.date(),
                payload={'identity':config.identity(),'reason':'auth','until':None},clock=lambda:at)
    assert view.build(engine,at).status=='HALTED'


def test_disabled_capture_is_not_presented_as_active_but_old_results_remain(engine,admitted,monkeypatch):
    monkeypatch.setenv('SHADOW_CAPTURE_ENABLED','false')
    r=view.build(engine,NOW+timedelta(seconds=3))
    assert r.status=='DISABLED' and not r.configured_enabled and r.candidate_count==1


def test_template_shows_paired_references_and_explicit_research_scope(engine,admitted):
    capture(engine,64)
    at=capture(engine,966,{'NVDA':103.,'SPY':510.})
    r=view.build(engine,at)
    env=Environment(loader=FileSystemLoader(Path(__file__).parents[2]/'webapp/templates'),autoescape=True)
    html=env.get_template('_shadow_capture.html').render(news_shadow=r)
    assert 'NVDA' in html and '+3.00' in html and '+1.00' in html
    assert 'Araştırma kaydı' in html and 'CALL/PUT getirisi değildir' in html
    assert 'ölçülmedi' in html and 'Yeni adayların fiyat kayıtları' in html
    assert asdict(r)['winner'] is None
