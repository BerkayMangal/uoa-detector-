"""Private readiness is a SELECT-only inventory, never a vendor/research trigger."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine

from tests.unit._webapp_auth import set_web_auth


def test_news_readiness_requires_auth_and_performs_no_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=engine))
    headers = set_web_auth(monkeypatch)
    writes: list[str] = []

    def executed(_c: object, _cursor: object, sql: str, _params: object, _ctx: object, _many: bool) -> None:
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")):
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", executed)
    client = TestClient(main.app)
    assert client.get('/api/arastirma/haber').status_code == 401
    response = client.get('/api/arastirma/haber', headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'STORAGE_UNAVAILABLE'
    assert body['winner'] is None and body['causal_opportunities'] is None
    assert body['vendor_calls_added'] == 0 and body['inference'] == 'NOT_PERFORMED'
    assert body['packet_count'] is None and writes == []
    assert 'no-store' in response.headers['cache-control']
    assert 'noindex' in response.headers['x-robots-tag']
