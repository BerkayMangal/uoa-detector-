"""Real immutable source → bounded operator export → frozen five-arm evaluator."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main, news_export
from webapp.board.db import make_engine
from webapp.live_alpha import store
from webapp.news_research import adapter, experiment, storage

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_news_adapter import NOW, news, scan, snapshot

DAY = NOW.date()
LATER = datetime(2026, 11, 6, 8, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'export.db'}")
    store.ensure_live_tables(e)
    storage.ensure(e)
    yield e
    e.dispose()


def test_export_preserves_real_receipt_gates_and_frozen_evaluator_missing_prices(engine):
    scan(engine)
    news(engine)
    result = news_export.export_session(engine, DAY, LATER)
    assert result.strategy_tests_executed == 0 and result.source_scans == 1
    assert result.audit.dataset is not None
    item = result.audit.dataset.opportunities[0]
    assert item.base is True and item.rs is True and item.news is True
    assert result.audit.news_provenance[0].packet_key
    assert result.audit.news_provenance[0].available_at <= item.at
    report = experiment.evaluate(result.audit.dataset)
    assert report.roster_count == 1
    assert report.base.complete_common_sessions == 0
    assert report.winner is None and report.inference == 'NOT_PERFORMED'
    assert all(a.priced == 0 and a.incomplete_days == 1 for a in report.base.arms)
    later = news_export.export_session(engine, DAY, LATER + timedelta(days=1))
    assert later.audit.dataset.identity == result.audit.dataset.identity


def test_missing_receipt_is_unknown_and_never_becomes_no_news(engine):
    scan(engine)
    result = news_export.export_session(engine, DAY, LATER)
    assert result.audit.dataset.opportunities[0].news is None


def test_completed_source_marks_price_all_five_arms_without_fitting(engine):
    scan(engine)
    news(engine)
    for key, at, spot, spy in [('entry', NOW + timedelta(minutes=2), 101., 500.5),
                                ('exit', NOW + timedelta(minutes=63), 103., 501.5)]:
        data = snapshot(at)
        data['flow_observations'] = {}
        prices = data['news_research_context']['prices']
        prices['NVDA']['spot'] = spot
        prices['SPY']['spot'] = spy
        scan(engine, key, at, data)
    exported = news_export.export_session(engine, DAY, LATER)
    report = experiment.evaluate(exported.audit.dataset)
    assert report.roster_count == 1 and report.base.complete_common_sessions == 1
    assert len(report.base.arms) == 5 and all(a.priced == 1 for a in report.base.arms)
    unit = report.base.units[0]
    assert unit.gross > unit.net > 0 and unit.cost > 0
    assert report.base.total_paired_excess == 0
    assert report.inference == 'NOT_PERFORMED' and report.winner is None


def test_first_partial_context_session_keeps_legacy_unknown_roster(engine):
    data = snapshot(NOW - timedelta(minutes=5))
    data.pop('news_research_context')
    scan(engine, 'legacy', NOW - timedelta(minutes=5), data)
    scan(engine)
    news(engine)
    result = news_export.export_session(engine, DAY, LATER)
    roster = experiment.roster(result.audit.dataset)
    assert len(roster) == 1 and roster[0].base is None
    assert result.audit.legacy_scans == 1


@pytest.mark.parametrize('day,now,reason', [
    (date(2026,11,6), LATER, 'session_not_finished'),
    (date(2026,11,9), LATER, 'session_not_finished'),
    (date(2026,11,1), LATER, 'not_a_verified_session'),
    (DAY, LATER.replace(tzinfo=None), 'aware_clock_required'),
])
def test_invalid_session_bounds_refuse_before_any_query(engine, day, now, reason):
    sql = []
    event.listen(engine, 'before_cursor_execute', lambda _c, _cur, stmt, *_a: sql.append(stmt))
    with pytest.raises(news_export.ExportRefusedError, match=reason):
        news_export.export_session(engine, day, now)
    assert not sql


def test_calendar_bounds_follow_dst_and_adjacent_windows_do_not_overlap():
    a, b = news_export.bounds(date(2026,10,30), LATER)
    c, d = news_export.bounds(date(2026,11,2), LATER)
    assert a.hour == 4 and (b + timedelta(microseconds=1)).hour == 4
    assert c.hour == 5 and (d + timedelta(microseconds=1)).hour == 5
    e, _ = news_export.bounds(date(2026,11,3), LATER)
    assert d < e and e - d == timedelta(microseconds=1)


def test_storage_or_context_unavailable_never_becomes_empty_success(tmp_path, engine):
    empty = make_engine(f"sqlite:///{tmp_path / 'absent.db'}")
    with pytest.raises(news_export.ExportRefusedError, match='scan_storage_unavailable'):
        news_export.export_session(empty, DAY, LATER)
    with pytest.raises(news_export.ExportRefusedError, match='physical_context_unavailable'):
        news_export.export_session(engine, DAY, LATER)
    scan(engine)
    with pytest.raises(news_export.ExportRefusedError, match='physical_context_unavailable'):
        news_export.export_session(engine, DAY - timedelta(days=1), LATER)
    empty.dispose()


@pytest.mark.parametrize('cap', ['MAX_SCANS', 'MAX_SOURCE_CHARACTERS'])
def test_preflight_caps_refuse_before_snapshot_materialization(engine, monkeypatch, cap):
    scan(engine)
    monkeypatch.setattr(news_export, cap, 0)
    monkeypatch.setattr(adapter, 'audit', lambda *_a, **_k: pytest.fail('materialized capped source'))
    with pytest.raises(news_export.ExportRefusedError, match='source_limit_exceeded'):
        news_export.export_session(engine, DAY, LATER)


def test_response_cap_source_refusal_and_busy_all_release_the_lock(engine, monkeypatch):
    scan(engine)
    with monkeypatch.context() as patch:
        patch.setattr(news_export, 'MAX_RESPONSE_BYTES', 1)
        with pytest.raises(news_export.ExportRefusedError, match='response_limit_exceeded'):
            news_export.export_session(engine, DAY, LATER)
    with monkeypatch.context() as patch:
        def fail(*_a, **_k):
            raise ValueError('private source content must not be in response')
        patch.setattr(adapter, 'audit', fail)
        with pytest.raises(news_export.ExportRefusedError, match=r'^source_export_refused$'):
            news_export.export_session(engine, DAY, LATER)
    with news_export._LOCK, pytest.raises(news_export.ExportRefusedError, match='export_busy'):
        news_export.export_session(engine, DAY, LATER)
    assert news_export.export_session(engine, DAY, LATER).source_scans == 1


def test_concurrent_export_is_rejected_while_actual_adapter_is_running(engine, monkeypatch):
    scan(engine)
    original = adapter.audit
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(adapter, 'audit', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(news_export.export_session, engine, DAY, LATER)
        assert entered.wait(5)
        try:
            with pytest.raises(news_export.ExportRefusedError, match='export_busy'):
                news_export.export_session(engine, DAY, LATER)
        finally:
            release.set()
        assert future.result().source_scans == 1
    assert not news_export._LOCK.locked()


def test_conflicting_completed_source_prices_refuse_without_replacement(engine):
    scan(engine)
    scan(engine, 'conflict', NOW + timedelta(seconds=1), snapshot())
    # The same source timestamp now has a different price in a later snapshot.
    data = snapshot()
    data['news_research_context']['prices']['NVDA']['spot'] = 123
    scan(engine, 'bad-price', NOW + timedelta(seconds=2), data)
    with pytest.raises(news_export.ExportRefusedError, match='source_export_refused'):
        news_export.export_session(engine, DAY, LATER)


def test_day_export_is_private_origin_checked_select_only_and_no_get_job(engine, monkeypatch):
    scan(engine)
    news(engine)
    monkeypatch.setattr(main, '_board_reader', lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, '_now', lambda: LATER)
    headers = set_web_auth(monkeypatch)
    writes = []
    def executed(_c, _cursor, sql, *_args):
        if sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','ALTER','DROP')):
            writes.append(sql)
    event.listen(engine, 'before_cursor_execute', executed)
    c = TestClient(main.app)
    path = '/api/arastirma/haber/export'
    body = {'day': DAY.isoformat()}
    assert c.post(path, json=body).status_code == 401
    assert c.get(path, headers=headers).status_code == 405
    assert c.post(path, json=body, headers=headers).status_code == 403
    assert c.post(path, json=body, headers={**headers,'Origin':'https://foreign.test'}).status_code == 403
    r = c.post(path, json=body, headers={**headers,'Origin':'http://testserver'})
    assert r.status_code == 200
    assert r.json()['audit']['dataset']['opportunities'][0]['news'] is True
    assert 'Company wins contract' not in r.text and 'snapshot_json' not in r.text
    assert r.json()['strategy_tests_executed'] == 0 and not writes
    assert 'no-store' in r.headers['cache-control'] and 'noindex' in r.headers['x-robots-tag']
    assert c.post(path, json={**body,'override':True}, headers=headers).status_code == 422
    r = c.post(path, json={'day':'2026-11-06'}, headers={**headers,'Origin':'http://testserver'})
    assert r.status_code == 409 and r.json()['detail'] == 'session_not_finished'


def test_exact_midnight_scan_belongs_only_to_next_export(engine):
    scan(engine)
    _, end = news_export.bounds(DAY, LATER)
    scan(engine, 'midnight', end + timedelta(microseconds=1), snapshot())
    old = news_export.export_session(engine, DAY, LATER)
    new = news_export.export_session(engine, DAY + timedelta(days=1), LATER)
    assert old.source_scans == new.source_scans == 1
