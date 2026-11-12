"""Private owner/connector endpoints; GETs never collect data or create tables."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from webapp.theta_validation import service, storage

MAX_UPLOAD_BYTES = 8 * 1024 * 1024


async def _body(request: Request, maximum: int) -> object:
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(413, "theta_upload_too_large")
        chunks.append(chunk)
    try:
        return json.loads(
            b"".join(chunks),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
        )
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "invalid_json") from None


def _failure(error: ValueError) -> HTTPException:
    reason = str(error)
    code = (
        404
        if reason == "request_not_found"
        else 503
        if reason == "storage_unavailable"
        else (
            409
            if reason
            in {
                "completed_result_is_immutable",
                "attempt_limit_reached",
                "concurrent_receipt_retry_required",
                "request_hash_mismatch",
            }
            else 422
        )
    )
    return HTTPException(code, reason)


def build_router(
    engine_provider: Callable[[], Engine],
    same_origin: Callable[[Request], bool],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """The parent app supplies its existing Basic-auth middleware and CSRF guard."""
    router = APIRouter(prefix="/api/theta", tags=["theta-validation"])

    @router.get("/status")
    def status() -> dict[str, Any]:
        return storage.view(engine_provider(), now=clock())

    @router.get("/sources")
    def sources() -> dict[str, Any]:
        return service.sources(engine_provider(), clock())

    @router.get("/jobs")
    def jobs(limit: int = Query(default=4, ge=1, le=4)) -> dict[str, Any]:
        return service.pending_jobs(engine_provider(), clock(), limit)

    @router.post("/requests")
    async def create(request: Request) -> dict[str, Any]:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        body = await _body(request, 4096)
        try:
            spec = service.RequestSpec.model_validate(body)
            return await run_in_threadpool(service.create_request, engine_provider(), spec, clock())
        except ValidationError:
            raise HTTPException(422, "invalid_theta_request") from None
        except ValueError as error:
            raise _failure(error) from None

    @router.post("/results/{request_id}")
    async def upload(request_id: str, request: Request) -> dict[str, Any]:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        body = await _body(request, MAX_UPLOAD_BYTES)
        try:
            spec = service.UploadSpec.model_validate(body)
            return await run_in_threadpool(
                service.ingest, engine_provider(), request_id, spec, clock()
            )
        except ValidationError:
            raise HTTPException(422, "invalid_theta_upload") from None
        except ValueError as error:
            raise _failure(error) from None

    @router.post("/requests/{request_id}/retry")
    async def retry(request_id: str, request: Request) -> dict[str, Any]:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        try:
            return await run_in_threadpool(
                storage.retry_request, engine_provider(), request_id, clock()
            )
        except ValueError as error:
            raise _failure(error) from None

    return router
