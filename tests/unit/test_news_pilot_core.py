"""Daily ownership, actual frozen evaluation and honest imported result clocks."""
import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, update
from webapp.board.db import make_engine
from webapp.live_alpha import store as live
from webapp.news_pilot import report, service, storage
from webapp.news_research import storage as physical

from tests.unit.test_news_adapter import NOW, news, scan

AT = datetime(2026, 11, 6, 12, tzinfo=UTC)
DAY = NOW.date()


@pytest.fixture(autouse=True)
def isolated_service_status(monkeypatch):
    monkeypatch.setattr(service, 'STATE', {})


@pytest.fixture
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'pilot.db'}")
    live.ensure_live_tables(e)
    physical.ensure(e)
    storage.ensure(e)
    yield e
    e.dispose()


def test_actual_adapter_evaluator_daily_summary_and_post_work_recording(engine):
    scan(engine)
    news(engine)
    finished = AT + timedelta(seconds=12)
    assert service.process_day(engine, DAY, AT, clock=lambda: finished) == 'completed'
    assert storage.read(engine, AT)[0]['status'] == 'pending'
    row = storage.read(engine, finished)[0]
    assert row['status'] == 'completed' and row['recorded_at'] == finished.isoformat()
    s = row['summary']
    assert s['source_scans'] == 1 and s['base_true'] == 1 and s['roster_slots'] == 1
    assert s['complete_common_sessions'] == s['eb_paired_days'] == 0
    assert s['unit_missing'] == {'entry_missing_observed_price':1}
    assert len(s['arms']) == 5 and len(s['stresses']) == 4
    assert all(a['priced'] == 0 and a['net_complete_days'] is None for a in s['arms'])
    assert s['winner'] is None and s['inference'] == 'NOT_PERFORMED'
    assert s['new_fitted_trials'] == 0 and s['fixed_arm_cost_evaluations'] == 20
    assert service.process_day(engine, DAY, finished, clock=lambda: finished) == 'already_claimed'
    assert len(storage.read(engine, finished)) == 1


def test_pending_claim_never_replays_after_restart(engine, monkeypatch):
    storage.claim(engine, DAY, AT)
    monkeypatch.setattr(service, 'export_session', lambda *_a, **_k: pytest.fail('pending day replayed'))
    assert service.process_day(engine, DAY, AT) == 'already_claimed'
    assert storage.read(engine, AT)[0]['status'] == 'pending'


def test_actual_source_cap_failure_is_durable_and_not_retried(engine, monkeypatch):
    from webapp import news_export
    scan(engine)
    monkeypatch.setattr(news_export, 'MAX_SCANS', 0)
    assert service.process_day(engine, DAY, AT, clock=lambda: AT) == 'failed'
    row = storage.read(engine, AT)[0]
    assert row['status'] == 'failed' and row['reason'] == 'source_limit_exceeded' and row['summary'] is None
    monkeypatch.setattr(service, 'export_session', lambda *_a, **_k: pytest.fail('failed day replayed'))
    assert service.process_day(engine, DAY, AT) == 'already_claimed'


def test_concurrent_processing_and_database_claim_are_unique(engine, monkeypatch):
    scan(engine)
    original = service.export_session
    entered, release = threading.Event(), threading.Event()
    def blocked(*a, **k):
        entered.set()
        assert release.wait(5)
        return original(*a, **k)
    monkeypatch.setattr(service, 'export_session', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.process_day, engine, DAY, AT, clock=lambda: AT)
        assert entered.wait(5)
        try:
            assert service.process_day(engine, DAY, AT) == 'busy'
        finally:
            release.set()
        assert future.result() == 'completed'
    with ThreadPoolExecutor(max_workers=2) as pool:
        keys = list(pool.map(lambda _: storage.claim(engine, date(2026,11,5), AT), range(2)))
    assert sum(key is not None for key in keys) == 1


def test_result_first_write_preserved_and_private_errors_are_categorized(engine):
    key = storage.claim(engine, DAY, AT)
    assert storage.fail(engine, key, 'PRIVATE SECRET SQL ERROR', AT)
    assert not storage.fail(engine, key, 'source_limit_exceeded', AT + timedelta(seconds=1))
    assert storage.read(engine, AT + timedelta(seconds=1))[0]['reason'] == 'evaluation_failed'


def test_absent_storage_read_is_select_only_and_future_claim_is_hidden(tmp_path, engine):
    empty = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    writes = []
    event.listen(empty, 'before_cursor_execute', lambda _c, _cur, sql, *_a:
                 writes.append(sql) if sql.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE')) else None)
    assert not storage.available(empty) and storage.read(empty, AT) == [] and writes == []
    storage.claim(engine, DAY, AT + timedelta(seconds=1))
    assert storage.read(engine, AT) == []
    empty.dispose()


def test_tampered_result_is_unverified_not_a_measured_zero(engine):
    key = storage.claim(engine, DAY, AT)
    storage.fail(engine, key, 'source_limit_exceeded', AT)
    with engine.begin() as connection:
        connection.execute(update(storage.PilotResult).where(storage.PilotResult.key == key).values(status='completed'))
    row = storage.read(engine, AT)[0]
    assert row['status'] == 'failed' and row['reason'] == 'result_identity_unverified'
    assert row['summary'] is None


def test_real_seed_integrity_import_once_and_new_record_clock(engine):
    rows = report.load_seed(AT)
    assert {r['day'] for r in rows} == {'2026-11-04','2026-11-05'}
    assert sum(r['source_scans'] for r in rows) == 286
    assert service.import_seed(engine, AT) == 2
    assert service.import_seed(engine, AT + timedelta(seconds=1)) == 0
    saved = storage.read(engine, AT)
    assert len(saved) == 2 and all(r['recorded_at'] == AT.isoformat() for r in saved)
    assert all(r['summary']['since'] != r['recorded_at'] for r in saved)
    assert all(r['summary']['complete_common_sessions'] == 0 for r in saved)


@pytest.mark.parametrize('damage', ['checksum','profile','code','future','bounds','inference','counts','source_duplicate'])
def test_seed_corruption_future_dates_and_identity_refuse(tmp_path, damage):
    payload = json.loads(report.seed_path.read_text())
    if damage == 'profile':
        payload['profile_hash'] = 'wrong'
    if damage == 'code':
        payload['rows'][0]['implementation_hash'] = 'wrong'
    if damage == 'future':
        payload['rows'][0]['day'] = '2026-11-09'
    if damage == 'bounds':
        payload['rows'][0]['as_of'] = '2026-11-07T00:00:00+00:00'
    if damage == 'inference':
        payload['rows'][0]['inference'] = 'VALIDATED'
    if damage == 'counts':
        payload['rows'][0]['base_true'] = True
    if damage == 'source_duplicate':
        payload['rows'][1]['source_identity'] = payload['rows'][0]['source_identity']
    path = tmp_path/'seed.json'
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    path.with_suffix('.sha256').write_text('0'*64 if damage == 'checksum' else hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        report.load_seed(AT, path)


def test_loop_imports_seed_without_replay_and_disabled_does_no_io(engine, monkeypatch):
    monkeypatch.setenv('NEWS_PILOT_ENABLED', 'true')
    monkeypatch.setattr(service, 'configured_engine', lambda _: engine)
    monkeypatch.setattr(service, 'export_session', lambda *_a, **_k: pytest.fail('seed day replayed'))
    asyncio.run(service.loop('fixture', clock=lambda: AT, max_cycles=1))
    assert service.safe_status()['status'] == 'already_claimed'
    assert service.safe_status()['imported'] == 2
    monkeypatch.setenv('NEWS_PILOT_ENABLED','false')
    monkeypatch.setattr(service, 'configured_engine', lambda _: pytest.fail('disabled opened engine'))
    asyncio.run(service.loop('fixture', clock=lambda: AT, max_cycles=1))
    assert service.safe_status() == {'status':'disabled'}


@pytest.mark.parametrize('now,expected', [
    (datetime(2026,11,6,4,59,tzinfo=UTC),date(2026,11,4)),
    (datetime(2026,11,6,5,tzinfo=UTC),date(2026,11,5)),
    (datetime(2026,11,9,12,tzinfo=UTC),date(2026,11,6)),
    (datetime(2026,11,4,12,tzinfo=UTC),None),
])
def test_completed_et_day_bounds_and_weekend(now, expected):
    assert report.latest_completed_day(now) == expected


def test_native_database_engine_has_separate_pool_and_statement_protection(monkeypatch):
    options = {}
    def construct(url, **kwargs):
        options.update(kwargs)
        return 'isolated-engine'
    monkeypatch.setattr(service, 'create_engine', construct)
    assert service.configured_engine('postgresql://fixture:fixture@example.invalid/db') == 'isolated-engine'
    assert options['pool_size'] == 2 and options['max_overflow'] == 0
    assert 'statement_timeout=120000' in options['connect_args']['options']
    assert 'lock_timeout=1000' in options['connect_args']['options']
