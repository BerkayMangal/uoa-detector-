"""Native repair claims permit one attempt and retain private source evidence."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from webapp.board.db import make_engine
from webapp.call_research import repair_operator as op
from webapp.history_capture.capture import seal


@pytest.fixture
def engine(tmp_path):
    return make_engine(f"sqlite:///{tmp_path / 'operator.db'}")


def test_status_on_empty_database_does_not_create_tables(engine):
    assert op.status(engine, datetime.now(UTC)) == {"status": "not_requested"}
    assert inspect(engine).get_table_names() == []


async def test_concurrent_claim_and_redeploy_do_not_repeat_http(engine, tmp_path, monkeypatch):
    calls = []

    async def run(_engine, output):
        calls.append(output)
        await asyncio.sleep(0)
        output.mkdir(parents=True)
        seal(
            output / "NFLX.json.gz",
            dict(payload={"data": []}, received_at=datetime.now(UTC).isoformat()),
        )
        return dict(status="complete", before_ready=35, after_ready=82, expected=82, http_calls=47)

    monkeypatch.setattr(op.daily_repair, "run", run)
    result = await asyncio.gather(
        op.once(engine, "request-1", tmp_path), op.once(engine, "request-1", tmp_path)
    )
    assert sorted(result) == [False, True] and len(calls) == 1
    assert not await op.once(engine, "request-1", tmp_path)
    status = op.status(engine, datetime.now(UTC))
    assert status["status"] == "complete" and status["after_ready"] == 82
    assert status["source_packets_persisted"] == 1
    assert not {"packet", "encoding", "payload_json"} & set(status)
    with Session(engine) as s:
        sources = s.scalars(select(op.RepairRecord).where(op.RepairRecord.kind == "source")).all()
        assert len(sources) == 1
        b = json.loads(sources[0].payload_json)
        raw = json.loads(gzip.decompress(base64.b64decode(b["packet"])))
        assert raw["payload"] == {"data": []} and "Authorization" not in str(raw)


async def test_crashed_request_stays_claimed_and_no_secret_escapes(engine, tmp_path, monkeypatch):
    calls = []

    async def broken(*args):
        calls.append(1)
        raise RuntimeError("private-password-sentinel")

    monkeypatch.setattr(op.daily_repair, "run", broken)
    assert await op.once(engine, "failed", tmp_path)
    assert not await op.once(engine, "failed", tmp_path)
    status = op.status(engine, datetime.now(UTC))
    assert status["status"] == "stopped" and status["reason"] == "RuntimeError" and calls == [1]
    assert "password" not in json.dumps(status)


async def test_invalid_request_does_not_create_a_table_or_path(engine, tmp_path):
    with pytest.raises(ValueError, match="invalid_operator_request_id"):
        await op.once(engine, "../../other", tmp_path)
    assert inspect(engine).get_table_names() == []
