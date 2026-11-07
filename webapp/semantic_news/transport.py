"""One bounded authenticated request, no redirects, hidden retries or tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from webapp.semantic_news import config
from webapp.semantic_news.model import Metadata, Request, aware, canonical
from webapp.semantic_news.validation import decode


@dataclass(frozen=True)
class Packet:
    status: str
    requested_at: datetime
    received_at: datetime
    http_status: int | None
    raw_body: bytes | None
    wire_attempted: bool


@dataclass(frozen=True)
class MetaResult:
    status: str
    value: Metadata | None


class Client(Protocol):
    async def metadata(self) -> MetaResult: ...
    async def analyze(
        self, request: Request, *, deadline: datetime, before_request: Callable[[], Awaitable[None]]
    ) -> Packet: ...
    async def aclose(self) -> None: ...


class Transport:
    def __init__(
        self,
        token: str,
        *,
        clock: Callable[[], datetime] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("semantic_credential_missing")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=config.settings().request_timeout_seconds,
            headers={"Authorization": "Bearer " + token, "User-Agent": "uoa-semantic-news/1.0"},
        )

    async def metadata(self) -> MetaResult:
        try:
            raw = bytearray()
            async with self._client.stream("GET", config.settings().endpoint + "/meta") as response:
                if response.status_code != 200:
                    return MetaResult("metadata_refused", None)
                async for part in response.aiter_bytes():
                    if len(raw) + len(part) > config.settings().max_body_bytes:
                        return MetaResult("metadata_oversize", None)
                    raw.extend(part)
            value = Metadata.model_validate(decode(bytes(raw)))
            return MetaResult("configured" if value.configured else "disabled", value)
        except Exception:
            return MetaResult("metadata_unavailable", None)

    async def analyze(
        self, request: Request, *, deadline: datetime, before_request: Callable[[], Awaitable[None]]
    ) -> Packet:
        at = aware(self._clock())
        raw = canonical(request.model_dump(mode="json"))
        if len(raw) > config.settings().max_input_bytes or not request.valid(at):
            return Packet("invalid_input", at, at, None, None, False)
        # The caller's durable claim and budget exist before this await.
        await before_request()
        requested = aware(self._clock())
        remaining = (aware(deadline) - requested).total_seconds()
        if requested < at or remaining <= 0:
            return Packet("late_or_clock_error", requested, requested, None, None, False)
        attempted = False
        try:
            attempted = True
            body = bytearray()
            async with self._client.stream(
                "POST",
                config.settings().endpoint,
                content=raw,
                headers={"Content-Type": "application/json"},
                timeout=min(config.settings().request_timeout_seconds, remaining),
            ) as response:
                code = response.status_code
                async for part in response.aiter_bytes():
                    if len(body) + len(part) > config.settings().max_body_bytes:
                        return Packet("oversize", requested, aware(self._clock()), code, None, True)
                    body.extend(part)
            received = aware(self._clock())
            if received < requested:
                return Packet("clock_error", requested, received, code, bytes(body), True)
            status = (
                "ok"
                if code == 200
                else "auth"
                if code in (401, 403)
                else "rate_limited"
                if code == 429
                else "provider_refused"
            )
            return Packet(status, requested, received, code, bytes(body), True)
        except Exception:
            return Packet(
                "unknown_provider_outcome", requested, aware(self._clock()), None, None, attempted
            )

    async def aclose(self) -> None:
        await self._client.aclose()
