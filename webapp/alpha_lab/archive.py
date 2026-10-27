"""Batch capture before legacy alert reduction; no additional vendor requests."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast

from sqlalchemy import Table

from webapp.alpha_lab import db
from webapp.alpha_lab.flow import timestamp
from webapp.board.db import make_engine

STATE: dict[str, object] = {}


class FlowArchive:
    def __init__(self, database_url: str) -> None:
        self.engine = make_engine(database_url)
        self.ready = False

    def write(self, ticker: str, records: list[dict[str, object]], observed_at: datetime) -> None:
        if not self.ready:
            db.ensure(self.engine)
            self.ready = True
        rows = []
        for record in records:
            native = record.get("id")
            identity = (
                (ticker, str(native))
                if native is not None
                else (
                    ticker,
                    str(record.get("option_chain", "")),
                    str(record.get("created_at", "")),
                )
            )
            alert_id = db.digest(identity)
            rows.append(
                dict(
                    key=db.digest((alert_id, record)),
                    alert_id=alert_id,
                    ticker=ticker,
                    observed_at=observed_at,
                    payload_json=db.dumps(record),
                )
            )
        db.append(self.engine, cast(Table, db.Alert.__table__), rows)
        times = [at for r in records if (at := timestamp(r.get("created_at"))) is not None]
        db.append(
            self.engine,
            cast(Table, db.Poll.__table__),
            [
                dict(
                    key=db.digest((ticker, observed_at.isoformat())),
                    ticker=ticker,
                    observed_at=observed_at,
                    payload_json=db.dumps(
                        {
                            "records": len(records),
                            "oldest": min(times).isoformat() if times else None,
                            "saturated": len(records) >= 100,
                            "transport": "UW stock flow-alerts",
                        }
                    ),
                )
            ],
        )
        STATE.update(
            status="ok", last_success_at=observed_at.isoformat(), received=len(records), error=None
        )

    def failed(self, exc: BaseException) -> None:
        previous = STATE.get("failures", 0)
        failures = previous if isinstance(previous, int) else 0
        STATE.update(status="failed", error=type(exc).__name__, failures=failures + 1)

    async def record(
        self, ticker: str, records: list[dict[str, object]], observed_at: datetime
    ) -> None:
        try:
            await asyncio.to_thread(self.write, ticker, records, observed_at)
        except (Exception, asyncio.CancelledError) as exc:
            self.failed(exc)
            STATE["at"] = observed_at.isoformat()
            raise

    def close(self) -> None:
        self.engine.dispose()
