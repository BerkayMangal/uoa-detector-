"""Owner enrollment and narrowly scoped continuous-device wire endpoints."""
from __future__ import annotations

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
from webapp.theta_validation import devices, pairing, service
from webapp.theta_validation.router import MAX_UPLOAD_BYTES, _body, _failure


def _device(request: Request) -> str:
    value = request.scope.get("theta_device_id")
    if not isinstance(value, str) or devices.DEVICE_ID.fullmatch(value) is None:
        raise HTTPException(401, "device_unauthorized")
    return value


def _owner(request: Request) -> None:
    if "theta_device_id" in request.scope or "theta_connector_request_ids" in request.scope:
        raise HTTPException(401, "owner_auth_required")


def _error(error: ValueError) -> HTTPException:
    reason = str(error)
    if reason == "device_unauthorized":
        return HTTPException(401, reason)
    if reason == "device_not_found":
        return HTTPException(404, reason)
    if reason == "device_pairing_token_expired":
        return HTTPException(410, reason)
    if reason == "device_pairing_token_consumed":
        return HTTPException(409, reason)
    return _failure(error)


def build_router(engine_provider: Callable[[], Engine], same_origin: Callable[[Request], bool],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> APIRouter:
    router = APIRouter(prefix="/api/theta", tags=["theta-devices"])

    @router.post("/devices/pairings")
    async def issue(request: Request) -> dict[str, Any]:
        _owner(request)
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        if await _body(request, 4096) != {}:
            raise HTTPException(422, "invalid_device_pairing_request")
        grant = await run_in_threadpool(devices.issue, engine_provider(), clock())
        return {"pairing_token": grant.token, "expires_at": grant.expires_at.isoformat(),
                "scope": "future_theta_validation_jobs_until_revoked"}

    @router.post("/device/redeem")
    async def redeem(request: Request) -> Response:
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        body = await _body(request, 4096)
        if not isinstance(body, dict) or set(body) != {"pairing_token"} or not isinstance(body["pairing_token"], str):
            raise HTTPException(422, "invalid_device_pairing_request")
        source = (Path(__file__).resolve().parents[2] / "scripts" / "theta_auto_connector.py").read_text()
        try:
            pairing.personalize_source(source, "0" * 64, seo.ORIGIN)
            issued = await run_in_threadpool(devices.redeem, engine_provider(), body["pairing_token"], clock())
            rendered = pairing.personalize_source(source, issued.token, seo.ORIGIN)
        except ValueError as error:
            raise _error(error) from None
        return Response(rendered, media_type="text/x-python", headers={
            "Content-Disposition": 'attachment; filename="uoa-theta-otomatik-' + secrets.token_hex(6) + '.py"',
            "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, nosnippet",
        })

    @router.get("/devices/status")
    def status(request: Request) -> dict[str, Any]:
        _owner(request)
        return devices.status(engine_provider(), clock())

    @router.post("/devices/{device_id}/revoke")
    async def revoke(device_id: str, request: Request) -> dict[str, Any]:
        _owner(request)
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        if await _body(request, 4096) != {}:
            raise HTTPException(422, "invalid_device_revoke_request")
        try:
            return await run_in_threadpool(devices.revoke, engine_provider(), device_id, clock())
        except ValueError as error:
            raise _error(error) from None

    @router.get("/device/jobs")
    def jobs(request: Request, limit: int = Query(default=4, ge=1, le=4)) -> dict[str, Any]:
        try:
            return devices.jobs(engine_provider(), _device(request), clock(), limit)
        except ValueError as error:
            raise _error(error) from None

    @router.post("/device/heartbeat")
    async def heartbeat(request: Request) -> dict[str, Any]:
        device_id = _device(request)
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        body = await _body(request, 4096)
        try:
            spec = devices.HeartbeatSpec.model_validate(body)
            return await run_in_threadpool(devices.heartbeat, engine_provider(), device_id, spec, clock())
        except ValidationError:
            raise HTTPException(422, "invalid_device_heartbeat") from None
        except ValueError as error:
            raise _error(error) from None

    @router.post("/device/results/{request_id}")
    async def upload(request_id: str, request: Request) -> dict[str, Any]:
        device_id = _device(request)
        if not same_origin(request):
            raise HTTPException(403, "same_origin_required")
        if devices.RESULT_PATH.fullmatch(request.url.path) is None:
            raise HTTPException(404, "request_not_found")
        body = await _body(request, MAX_UPLOAD_BYTES)
        try:
            spec = service.UploadSpec.model_validate(body)
            return await run_in_threadpool(devices.ingest, engine_provider(), device_id, request_id, spec, clock())
        except ValidationError:
            raise HTTPException(422, "invalid_theta_upload") from None
        except ValueError as error:
            raise _error(error) from None

    return router
