"""Native-shaped seeded aggregates remain private and strictly read-only on GET."""
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine
from webapp.news_pilot import service, storage
from webapp.news_research import experiment

from tests.unit._webapp_auth import set_web_auth

NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)


def test_stored_actual_pilot_api_is_private_select_only_and_does_not_run_evaluation(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'pilot.db'}")
    storage.ensure(engine)
    assert service.import_seed(engine, NOW) == 2
    monkeypatch.setattr(main, '_board_reader', lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, '_now', lambda: NOW)
    monkeypatch.setattr(experiment, 'evaluate', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('GET evaluated sources')))
    writes = []
    def sql(_c, _cur, stmt, *_args):
        if stmt.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','ALTER','DROP')):
            writes.append(stmt)
    event.listen(engine, 'before_cursor_execute', sql)
    c = TestClient(main.app)
    path = '/api/arastirma/pilot'
    headers = set_web_auth(monkeypatch)
    assert c.get(path).status_code == 401
    r = c.get(path, headers=headers)
    assert r.status_code == 200
    v = r.json()
    assert v['completed_days'] == 2 and v['source_scans'] == 286
    assert v['opportunity_rows'] == 3651 and v['roster_slots'] == 8
    assert v['complete_common_sessions'] == 0 and v['eb_paired_days'] == 0
    assert v['winner'] is None and v['inference'] == 'NOT_PERFORMED'
    assert all(a['net'] is None for row in v['rows'] for a in row['arms'])
    assert not writes
    assert 'no-store' in r.headers['cache-control'] and 'noindex' in r.headers['x-robots-tag']
    engine.dispose()


def test_empty_pilot_api_never_creates_storage_on_get(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(main, '_board_reader', lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(main, '_now', lambda: NOW)
    writes = []
    event.listen(engine, 'before_cursor_execute', lambda _c, _cur, stmt, *_a: writes.append(stmt) if stmt.lstrip().upper().startswith(('CREATE','INSERT','UPDATE','DELETE')) else None)
    r = TestClient(main.app).get('/api/arastirma/pilot', headers=set_web_auth(monkeypatch))
    assert r.status_code == 200 and r.json()['status'] == 'STORAGE_UNAVAILABLE'
    assert r.json()['source_scans'] is None and not writes
    engine.dispose()
