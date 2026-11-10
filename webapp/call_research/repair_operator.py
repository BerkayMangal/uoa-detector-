"""Explicit one-shot native operator; atomic claims, private source receipts, no orders."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import DateTime, String, Table, Text, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column

from webapp.alpha_lab.db import append, digest, dumps
from webapp.board.db import AlfaBase, make_engine, session_factory
from webapp.call_research import daily_repair

_LOG = logging.getLogger(__name__)


class RepairRecord(AlfaBase):
    __tablename__ = "alfa_daily_repair_run"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)


def record(
    engine: Engine, request_id: str, kind: str, body: object, at: datetime, suffix: str = ""
) -> int:
    return append(
        engine,
        cast(Table, RepairRecord.__table__),
        [
            dict(
                key=digest((request_id, kind, suffix)),
                request_id=request_id,
                kind=kind,
                observed_at=at,
                payload_json=dumps(body),
            )
        ],
    )


async def once(
    engine: Engine,
    request_id: str,
    root: Path,
    *,
    run: Callable[[Engine, Path], Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
        raise ValueError("invalid_operator_request_id")
    cast(Table, RepairRecord.__table__).create(engine, checkfirst=True)
    started = datetime.now(UTC)
    if not record(engine, request_id, "claimed", {"status": "claimed", "at": started}, started):
        return False
    output = root / request_id
    result: dict[str, Any]
    try:
        result = await (run or daily_repair.run)(engine, output)
    except Exception as error:
        result = dict(status="stopped", reason=type(error).__name__)
    try:
        # These bounded gzip packets contain native source/quota/time evidence, no auth headers.
        for path in sorted(output.glob("*.json.gz")):
            data = path.read_bytes()
            record(
                engine,
                request_id,
                "source",
                dict(
                    filename=path.name,
                    sha256=hashlib.sha256(data).hexdigest(),
                    encoding="gzip+base64",
                    packet=base64.b64encode(data).decode(),
                ),
                datetime.now(UTC),
                path.name,
            )
        result["source_packets_persisted"] = len(tuple(output.glob("*.json.gz")))
        result["request_id"] = request_id
        record(engine, request_id, "result", result, datetime.now(UTC))
        _LOG.info("daily repair %s: %s", request_id, result.get("status"))
    except Exception as error:
        # Do not hide a claimed-but-incomplete run or log SQL/connection payloads.
        _LOG.error("daily repair receipt unavailable: %s", type(error).__name__)
    return True


async def requested(database_url: str) -> None:
    request_id = os.environ.get("DAILY_REPAIR_REQUEST", "").strip()
    if not request_id:
        return
    engine = make_engine(database_url)
    try:
        await once(engine, request_id, Path("/tmp/uoa-daily-repair"))
    except Exception as error:
        _LOG.error("daily repair operator unavailable: %s", type(error).__name__)
    finally:
        engine.dispose()


def status(engine: Engine, now: datetime) -> dict[str, Any]:
    # SELECT-only maintenance projection; source blobs never leave this module.
    if not inspect(engine).has_table(RepairRecord.__tablename__):
        return {"status": "not_requested"}
    with session_factory(engine)() as session:
        latest = session.scalar(
            select(RepairRecord)
            .where(
                RepairRecord.kind == "claimed",
                RepairRecord.observed_at <= now,
            )
            .order_by(RepairRecord.observed_at.desc())
            .limit(1)
        )
        if latest is None:
            return {"status": "not_requested"}
        finished = session.scalar(
            select(RepairRecord)
            .where(
                RepairRecord.request_id == latest.request_id,
                RepairRecord.kind == "result",
                RepairRecord.observed_at <= now,
            )
            .limit(1)
        )
    if finished is None:
        return {
            "status": "claimed_result_pending",
            "request_id": latest.request_id,
            "started_at": latest.observed_at,
        }
    body = json.loads(finished.payload_json)
    safe = {
        k: body.get(k)
        for k in (
            "status",
            "request_id",
            "expected_day",
            "before_ready",
            "after_ready",
            "expected",
            "http_calls",
            "remaining",
            "started_at",
            "finished_at",
            "source_packets_persisted",
            "reason",
        )
    }
    safe["inserted_rows"] = sum(r.get("inserted", 0) for r in body.get("rows", []))
    safe["new_receipts"] = sum(r.get("receipts", 0) for r in body.get("rows", []))
    safe["conflicting_keys"] = sum(r.get("conflicts", 0) for r in body.get("rows", []))
    safe["problems"] = [
        {k: r.get(k) for k in ("ticker", "status", "reason", "conflicts", "absent_sessions")}
        for r in body.get("rows", [])
        if r.get("status") != "stored" or r.get("conflicts")
    ]
    return safe
