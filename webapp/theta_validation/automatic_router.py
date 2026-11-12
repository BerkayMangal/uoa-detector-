"""Owner-only automatic-cohort reads and explicit stored-packet reruns."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from webapp.theta_validation import automatic, storage
from webapp.theta_validation.router import _body


def _failure(error: ValueError) -> HTTPException:
    reason = str(error)
    return HTTPException(503 if reason == "storage_unavailable" else 413 if reason in {
        "cohort_row_limit_exceeded", "cohort_export_too_large"} else 422, reason)


def build_router(engine_provider: Callable[[], Engine], same_origin: Callable[[Request], bool],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> APIRouter:
    router = APIRouter(prefix="/api/theta/automatic", tags=["theta-automatic"])

    @router.get("/status")
    def status(day: date | None = None) -> dict[str, Any]:
        try:
            return automatic.status(engine_provider(), clock(), day)
        except ValueError as error:
            raise _failure(error) from None

    @router.get("/cohort")
    def cohort(day: date, include_packets: bool = Query(default=False)) -> Response:
        try:
            result = automatic.cohort(engine_provider(), day, include_packets=include_packets)
        except ValueError as error:
            raise _failure(error) from None
        return Response(storage.canonical(result), media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="theta-cohort-{day.isoformat()}.json"',
            "Cache-Control": "private, no-store"})

    @router.post("/reruns")
    async def rerun(request: Request) -> dict[str, Any]:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        body = await _body(request, 4096)
        if not isinstance(body, dict) or set(body) != {"day"} or not isinstance(body["day"], str):
            raise HTTPException(422, "invalid_rerun_request")
        try:
            day = date.fromisoformat(body["day"])
            return await run_in_threadpool(automatic.rerun, engine_provider(), day, clock())
        except ValueError as error:
            raise _failure(error) from None

    return router
