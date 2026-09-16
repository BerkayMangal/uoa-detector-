"""Shared Unusual Whales call surface for the board modules (Phase 5.2.B-jobs).

Two things every board fetch needs, in one place so the refresher, the daily-job
clock and the data layers agree without importing each other:

- ``JsonClient``: what a board job needs from ``UnusualWhalesClient`` — one
  ``request_json`` coroutine. Jobs take the protocol, so the refresher's single
  long-lived client (decision P17) satisfies them structurally under
  ``mypy --strict`` without a cast, and a test fake needs nothing else.
- ``is_key_failure``: HTTP 401/403 (or an auth error whose status cannot be
  read) means the key itself failed and every other call would fail the same
  way; any other 4xx is one degraded fetch. ``UnusualWhalesNotFoundError`` is a
  subclass of the auth error and is never a key failure: it means no data.

Nothing here makes a call or touches the database.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, Protocol

from uoa_detector.sources.unusual_whales.client import UnusualWhalesNotFoundError

if TYPE_CHECKING:
    from uoa_detector.sources.unusual_whales.client import UnusualWhalesAuthError

# HTTP statuses that mean the key itself failed (every call would fail the same way).
KEY_FAILURE_STATUSES: Final = frozenset({401, 403})
_HTTP_STATUS: Final = re.compile(r"HTTP (\d{3})")


class JsonClient(Protocol):
    """What a board job needs from ``UnusualWhalesClient``."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...


def is_key_failure(error: UnusualWhalesAuthError) -> bool:
    """True for HTTP 401/403, or an auth error whose status cannot be read.

    False for another 4xx (one degraded fetch) and always False for a
    not-found error, which means no data for that element.
    """
    if isinstance(error, UnusualWhalesNotFoundError):
        return False
    found = _HTTP_STATUS.search(str(error))
    return found is None or int(found.group(1)) in KEY_FAILURE_STATUSES
