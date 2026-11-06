"""Real completed source → prospective pre-filter parents; no old-label repair."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, update
from webapp.board.db import make_engine
from webapp.live_alpha import store as live
from webapp.news_research import storage as physical
from webapp.shadow_capture import bindings, config, storage

from tests.unit.test_news_adapter import LIVE, NOW, news, scan, snapshot


@pytest.fixture
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'shadow-bindings.db'}")
    live.ensure_live_tables(e)
    physical.ensure(e)
    storage.ensure(e)
    storage.activate(e, lambda: NOW - timedelta(hours=1))
    yield e
    e.dispose()


def observed(engine, seconds=2):
    at = NOW + timedelta(seconds=seconds)
    return bindings.discover(engine, at, clock=lambda: at)


def candidates(engine, seconds=3):
    return storage.list_events(engine, 'candidate', NOW + timedelta(seconds=seconds), day=NOW.date())


def multiple(tickers):
    data: dict[str, Any] = snapshot()
    flow = data['flow_observations']['NVDA']
    price = data['news_research_context']['prices']['NVDA']
    data['flow_observations'] = {t: dict(flow) for t in tickers}
    data['news_research_context']['prices'].update({t: dict(price) for t in tickers})
    return data


def test_real_source_and_physical_receipt_bind_first_parent_and_restart_dedupes(engine):
    scan(engine)
    news(engine)
    result = observed(engine)
    assert result['status'] == 'observed' and result['admitted'] == result['candidates'] == 1
    parent = candidates(engine)[0]
    assert parent.key == bindings.candidate_key(NOW.date(), 1)
    assert parent.unique_owner == bindings.candidate_owner(NOW.date(), 'NVDA')
    assert parent.payload['base'] is True and parent.payload['rs'] is True and parent.payload['news'] is True
    assert parent.payload['scan_id'] == 'first' and len(parent.payload['snapshot_hash']) == 64
    assert parent.payload['news_packet_key'] and parent.payload['news_payload_hash']
    assert parent.confirmed_at == NOW + timedelta(seconds=2)
    assert parent.payload['scan_finished_at'] == NOW.isoformat()
    # Independent connection/process state does not admit the same source twice.
    e2 = make_engine(str(engine.url))
    assert observed(e2)['status'] == 'already_observed'
    assert len(candidates(e2)) == 1
    e2.dispose()


def test_known_base_survives_false_auxiliary_filters_without_a_live_buy(engine):
    data = snapshot()
    data['cards'] = [{'ticker':'NVDA', 'recommendation':'WATCH'}]
    data['news_research_context']['prices']['NVDA']['spot'] = 100.3
    data['news_observations']['NVDA']['state'] = 'checked_none'
    scan(engine, data=data)
    physical.append(engine, 'NVDA', {'data':[]}, NOW-timedelta(seconds=30), NOW-timedelta(seconds=20),
                    clock=lambda: NOW-timedelta(seconds=10), application_checked_at=NOW-timedelta(seconds=30))
    assert observed(engine)['admitted'] == 1
    parent = candidates(engine)[0]
    assert parent.payload['rs'] is False and parent.payload['news'] is False


def test_unknown_auxiliary_flags_are_preserved_and_not_a_base_rejection(engine):
    data = snapshot()
    data['news_observations']['NVDA']['state'] = 'failed'
    data['news_research_context']['prices']['NVDA']['benchmark_move'] = 123
    scan(engine, data=data)
    assert observed(engine)['admitted'] == 1
    parent = candidates(engine)[0]
    assert parent.payload['rs'] is None and parent.payload['news'] is None


def test_unknown_base_is_not_promoted_by_display_text(engine):
    data = snapshot()
    data['news_research_context']['prices']['NVDA']['state'] = 'unavailable'
    data['cards'] = [{'ticker':'NVDA', 'recommendation':'BUY', 'readiness':'READY'}]
    scan(engine, data=data)
    result = observed(engine)
    assert result['admitted'] == 0 and result['unknown'] == 1
    assert candidates(engine) == ()


def test_later_news_receipt_cannot_rewrite_original_source_flag(engine):
    scan(engine)
    physical.append(engine, 'NVDA', {'data':[{'headline':'New contract', 'source':'Wire',
        'tickers':['NVDA'], 'created_at':(NOW-timedelta(hours=1)).isoformat()}]},
        NOW-timedelta(seconds=30), NOW-timedelta(seconds=20),
        clock=lambda: NOW+timedelta(seconds=20), application_checked_at=NOW-timedelta(seconds=30))
    assert observed(engine, seconds=30)['admitted'] == 1
    assert candidates(engine, seconds=31)[0].payload['news'] is None


@pytest.mark.parametrize('damage', [
    'stale', 'warmup', 'closed', 'failed', 'long_cycle',
    'profile', 'live_profile', 'legacy', 'malformed', 'universe', 'too_many',
])
def test_stale_unverified_or_outside_window_sources_cannot_create_parents(engine, damage):
    data: dict[str, Any] = snapshot()
    at, started, mode, status = NOW, NOW-timedelta(seconds=5), 'LIVE', 'ok'
    observation = NOW+timedelta(seconds=2)
    if damage == 'stale':
        observation = NOW+timedelta(seconds=61)
    elif damage == 'warmup':
        at = NOW.replace(hour=14, minute=40)
        started, observation, data = at-timedelta(seconds=5), at+timedelta(seconds=2), snapshot(at)
    elif damage == 'closed':
        mode = 'CLOSED'
    elif damage == 'failed':
        status = 'failed'
    elif damage == 'long_cycle':
        started = NOW-timedelta(seconds=301)
    elif damage == 'profile':
        data['news_research_context']['profile_hash'] = 'bad'
    elif damage == 'live_profile':
        data['profile_sha256'] = 'bad'
    elif damage == 'legacy':
        data.pop('news_research_context')
    elif damage == 'malformed':
        data['news_research_context'] = []
    elif damage == 'universe':
        data['flow_observations'] = {'NVDA?':{}}
    else:
        data = multiple([f'X{i}' for i in range(config.settings().source_row_limit+1)])
    live.write_scan(engine, live.ScanRow('damaged', started, at, mode, status, None, data),
                    LIVE.policy_version)
    result = bindings.discover(engine, observation, clock=lambda: observation)
    assert result['admitted'] == 0
    assert not storage.list_events(engine, 'candidate', observation, require_receipt=False)
    if damage == 'too_many':
        assert result['limited'] is True


def test_latest_failed_source_is_not_replaced_by_older_qualifying_scan(engine):
    scan(engine, 'old', NOW-timedelta(seconds=10), snapshot(NOW-timedelta(seconds=10)))
    live.write_scan(engine, live.ScanRow('latest', NOW-timedelta(seconds=5), NOW,
                    'LIVE', 'failed', None, snapshot()),
                    LIVE.policy_version)
    assert observed(engine)['status'] == 'source_ineligible'
    assert candidates(engine) == ()


def test_future_scan_and_backwards_physical_clock_do_not_create_parents(engine):
    scan(engine, at=NOW+timedelta(seconds=3), data=snapshot(NOW+timedelta(seconds=3)))
    assert observed(engine)['status'] == 'no_source'
    assert bindings.discover(engine, NOW+timedelta(seconds=2), clock=lambda: NOW)['status'] == 'clock_error'
    assert not candidates(engine)


def test_four_slots_are_global_atomic_and_first_tickers_cannot_be_replaced(engine):
    scan(engine, data=multiple(['AF','AE','AD','AC','AB','AA','SPY']))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: observed(engine), range(2)))
    parents = candidates(engine)
    assert sum(int(r['admitted']) for r in results) == 4
    assert len(parents) == 4 and {p.payload['ticker'] for p in parents} == {'AA','AB','AC','AD'}
    assert {p.payload['slot'] for p in parents} == {1,2,3,4}
    original = {p.key:p.payload for p in parents}
    scan(engine, 'new-source', NOW+timedelta(seconds=10), multiple(['AA','NEW']))
    assert observed(engine, seconds=12)['admitted'] == 0
    assert {p.key:p.payload for p in candidates(engine, seconds=13)} == original


def test_late_post_commit_clock_leaves_parent_unconfirmed_without_repair(engine):
    scan(engine)
    moments = iter([NOW+timedelta(seconds=1), NOW+timedelta(seconds=2), NOW+timedelta(seconds=61)])
    result = bindings.discover(engine, NOW+timedelta(seconds=1), clock=lambda: next(moments))
    assert result['status'] == 'publication_refused'
    assert candidates(engine, seconds=62) == ()
    raw = storage.list_events(engine, 'candidate', NOW+timedelta(seconds=62), require_receipt=False)
    assert len(raw) == 1 and raw[0].confirmed_at is None
    assert observed(engine, seconds=62)['status'] == 'source_expired'
    assert storage.get(engine, raw[0].key, NOW+timedelta(seconds=63)) is None


def test_discovery_writes_only_new_namespace_and_excludes_benchmark(engine):
    scan(engine, data=multiple(['NVDA','SPY']))
    news(engine)
    writes = []
    def seen(_c, _cur, sql, *_args):
        if sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','ALTER','DROP')):
            writes.append(sql)
    event.listen(engine, 'before_cursor_execute', seen)
    assert observed(engine)['admitted'] == 1
    assert candidates(engine)[0].payload['ticker'] == 'NVDA'
    assert writes and all('alfa_shadow_' in sql for sql in writes)


def test_source_start_must_follow_durable_activation_without_a_duration_confound(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'activation.db'}")
    live.ensure_live_tables(e)
    physical.ensure(e)
    storage.ensure(e)
    storage.activate(e, lambda: NOW-timedelta(seconds=2))
    scan(e)
    assert observed(e)['status'] == 'source_ineligible'
    assert candidates(e) == ()
    e.dispose()


def test_historical_validation_preserves_original_flags_without_current_freshness(engine):
    scan(engine)
    news(engine)
    assert observed(engine)['admitted'] == 1
    parent = candidates(engine)[0]
    for later in (NOW+timedelta(minutes=61), NOW+timedelta(days=1)):
        activation = storage.active(engine, later)
        assert bindings.valid_candidate(engine, parent, activation, later)
    assert not bindings.valid_candidate(engine, parent, activation, NOW+timedelta(seconds=1))


def test_modified_original_snapshot_or_forged_flags_fail_historical_binding(engine):
    scan(engine)
    news(engine)
    assert observed(engine)['admitted'] == 1
    at = NOW+timedelta(minutes=61)
    parent, activation = candidates(engine)[0], storage.active(engine, at)
    assert not bindings.valid_candidate(engine, replace(parent, payload=parent.payload | {'rs':False}), activation, at)
    assert not bindings.valid_candidate(engine, replace(parent, confirmed_at=parent.confirmed_at.replace(tzinfo=None)), activation, at)
    with engine.begin() as connection:
        connection.execute(update(live.LiveScan).where(live.LiveScan.scan_id == 'first').values(snapshot_json='{}'))
    assert not bindings.valid_candidate(engine, parent, activation, at)


def test_slow_pre_publication_clock_never_inserts_a_stale_parent(engine):
    scan(engine)
    moments = iter([NOW+timedelta(seconds=1), NOW+timedelta(seconds=61)])
    result = bindings.discover(engine, NOW+timedelta(seconds=1), clock=lambda: next(moments))
    assert result['status'] == 'publication_refused'
    assert not storage.list_events(engine, 'candidate', NOW+timedelta(seconds=62), require_receipt=False)


def test_unconfirmed_existing_parent_cannot_be_republished_on_a_fresh_repeat(engine):
    scan(engine)
    moments = iter([NOW+timedelta(seconds=1), NOW+timedelta(seconds=2), NOW+timedelta(seconds=1)])
    assert bindings.discover(engine, NOW+timedelta(seconds=1), clock=lambda: next(moments))['status'] == 'publication_refused'
    assert observed(engine, seconds=3)['admitted'] == 0
    assert not candidates(engine, seconds=4)
    assert storage.count(engine, 'candidate', NOW+timedelta(seconds=4), day=NOW.date()) == 1


@pytest.mark.parametrize('reason', ['processed', 'stale', 'closed', 'day_full'])
def test_idle_poll_reads_source_metadata_without_loading_snapshot(engine, reason):
    scan(engine, data=multiple(['AA','AB','AC','AD']) if reason == 'day_full' else snapshot())
    seconds = 2
    if reason in {'processed', 'day_full'}:
        assert observed(engine)['admitted'] >= 1
    if reason == 'day_full':
        scan(engine, 'next-source', NOW+timedelta(seconds=10), snapshot(NOW+timedelta(seconds=10)))
        seconds = 12
    elif reason == 'stale':
        seconds = 61
    elif reason == 'closed':
        with engine.begin() as connection:
            connection.execute(update(live.LiveScan).values(market_mode='CLOSED'))
    reads = []
    def seen(_c, _cur, sql, *_args):
        if sql.lstrip().upper().startswith('SELECT') and 'alfa_live_scan' in sql:
            reads.append(sql)
    event.listen(engine, 'before_cursor_execute', seen)
    result = observed(engine, seconds)
    assert result['status'] == {'processed':'already_observed', 'stale':'source_expired',
                                'closed':'source_ineligible', 'day_full':'candidate_limit'}[reason]
    assert result['admitted'] == 0
    assert reads and all('snapshot_json' not in sql for sql in reads)


def test_scan_must_start_after_opening_warmup(engine):
    finished = NOW.replace(hour=14, minute=45, second=1)
    live.write_scan(engine, live.ScanRow('warmup-start', finished-timedelta(seconds=2),
                    finished, 'LIVE', 'ok', None, snapshot(finished)), LIVE.policy_version)
    at = finished+timedelta(seconds=1)
    assert bindings.discover(engine, at, clock=lambda: at)['status'] == 'source_ineligible'
    assert not storage.list_events(engine, 'candidate', at, require_receipt=False)


def test_second_confirmation_clock_cannot_publish_after_source_expiry(engine):
    scan(engine)
    moments = iter([NOW+timedelta(seconds=1), NOW+timedelta(seconds=2),
                    NOW+timedelta(seconds=3), NOW+timedelta(seconds=61)])
    result = bindings.discover(engine, NOW+timedelta(seconds=1), clock=lambda: next(moments))
    assert result['status'] == 'publication_refused'
    raw = storage.list_events(engine, 'candidate', NOW+timedelta(seconds=62), require_receipt=False)
    assert len(raw) == 1 and raw[0].confirmed_at is None
    assert not candidates(engine, seconds=62)
