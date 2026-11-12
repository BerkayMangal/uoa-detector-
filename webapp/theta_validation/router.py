"""Private owner/connector endpoints; GETs never collect data or create tables."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from webapp import seo
from webapp.theta_validation import pairing, service, storage

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
    def jobs(request: Request, limit: int = Query(default=4, ge=1, le=4)) -> dict[str, Any]:
        allowed = request.scope.get("theta_connector_request_ids")
        return service.pending_jobs(engine_provider(), clock(), limit, allowed)

    @router.post("/pairings")
    async def issue_pairing(request: Request) -> dict[str, Any]:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        if await _body(request, 4096) != {}:
            raise HTTPException(422, "invalid_pairing_request")
        try:
            grant = await run_in_threadpool(pairing.issue, engine_provider(), clock())
        except ValueError as error:
            raise _failure(error) from None
        if not grant.request_count:
            raise HTTPException(409, "no_mature_pending_requests")
        return {"pairing_token": grant.token, "expires_at": grant.expires_at.isoformat(),
                "request_count": grant.request_count}

    @router.post("/pairing/redeem")
    async def redeem_pairing(request: Request) -> Response:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        body = await _body(request, 4096)
        if not isinstance(body, dict) or set(body) != {"pairing_token"} or not isinstance(body["pairing_token"], str):
            raise HTTPException(422, "invalid_pairing_request")
        source = (Path(__file__).resolve().parents[2] / "scripts" / "theta_mac_connector.py").read_text()
        try:
            # Validate the packaged template before consuming the one-use grant.
            pairing.personalize_source(source, "0" * 64, seo.ORIGIN)
            connector = await run_in_threadpool(pairing.redeem, engine_provider(), body["pairing_token"], clock())
            result = pairing.personalize_source(source, connector.token, seo.ORIGIN)
        except ValueError as error:
            code = 410 if str(error) == "pairing_token_expired" else 409 if str(error) == "pairing_token_consumed" else 422
            raise HTTPException(code, str(error)) from None
        return Response(result, media_type="text/x-python", headers={
            "Content-Disposition": 'attachment; filename="uoa-theta-hazir-' + secrets.token_hex(6) + '.py"',
            "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
        })

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
