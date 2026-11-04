"""Bounded cache-only GET to two fixed HTTPS endpoints; no redirects or retries."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .config import Endpoint, settings
from .model import Parsed, aware, etag_value, parse


@dataclass(frozen=True)
class Response:
    requested_at: datetime
    received_at: datetime
    status_code: int | None
    category: str
    raw: bytes = b""
    etag: str | None = None
    parsed: Parsed | None = None


async def fetch(endpoint: Endpoint, token: str, *, etag: str | None = None,
                transport: httpx.AsyncBaseTransport | None = None,
                clock: Callable[[], datetime] | None = None) -> Response:
    """A separate client per provider prevents cross-host header/cookie reuse."""
    clock = clock or (lambda: datetime.now(UTC))
    requested = aware(clock())
    if not token or token.strip() != token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        return Response(requested, aware(clock()), None, "credential_unavailable")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Encoding": "identity"}
    if etag is not None:
        etag_value(etag)
        headers["If-None-Match"] = etag
    code: int | None = None
    try:
        async with asyncio.timeout(settings().request_timeout_seconds), httpx.AsyncClient(
            timeout=settings().request_timeout_seconds, follow_redirects=False, transport=transport
        ) as client, client.stream("GET", endpoint.url, headers=headers) as response:
                code = response.status_code
                response_etag = response.headers.get("etag")
                if response_etag is not None:
                    try:
                        etag_value(response_etag)
                    except ValueError:
                        return Response(requested, aware(clock()), code, "invalid_etag")
                if code == 304:
                    return Response(requested, aware(clock()), code, "not_modified", etag=response_etag)
                if code != 200:
                    return Response(requested, aware(clock()), code, "redirect_rejected" if 300 <= code < 400 else "http_error")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return Response(requested, aware(clock()), code, "content_encoding_rejected")
                length = response.headers.get("content-length")
                if length and (not length.isdecimal() or int(length) > settings().max_body_bytes):
                    return Response(requested, aware(clock()), code, "body_limit")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > settings().max_body_bytes:
                        return Response(requested, aware(clock()), code, "body_limit")
                    raw.extend(chunk)
                received = aware(clock())
                try:
                    parsed = parse(bytes(raw), endpoint, received)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    return Response(requested, received, code, "invalid_body", bytes(raw), response_etag)
                return Response(requested, received, code, "ok", bytes(raw), response_etag, parsed)
    except (httpx.TimeoutException, TimeoutError):
        return Response(requested, aware(clock()), code, "request_timeout")
    except httpx.HTTPError:
        return Response(requested, aware(clock()), code, "transport_error")
