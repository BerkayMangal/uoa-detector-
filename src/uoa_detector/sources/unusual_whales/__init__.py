"""Unusual Whales source adapter.

Phase 3.3.3 converted this from a single file to a package directory
to host the production HTTP client, live WS source, and six provider
implementations. The Phase 2 stubs (``UnusualWhalesConfig`` and
``UnusualWhalesFlowSource``) are preserved in ``legacy_stub.py`` and
re-exported here for backwards compatibility with the existing
``sources/__init__.py`` re-exports and any test code that imports
them by name. They will be removed when Phase 3.4 wires the real
adapter into the orchestrator and replaces the stub call sites.

Public surface:
  - ``UnusualWhalesClient`` (Phase 3.3.3.2): authenticated HTTP client
  - ``UnusualWhalesConfig`` (Phase 2 stub, deprecated)
  - ``UnusualWhalesFlowSource`` (Phase 2 stub, deprecated)
"""

from uoa_detector.sources.unusual_whales.client import (
    DEFAULT_BASE_URL,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from uoa_detector.sources.unusual_whales.legacy_stub import (
    UnusualWhalesConfig,
    UnusualWhalesFlowSource,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "UnusualWhalesAuthError",
    "UnusualWhalesClient",
    "UnusualWhalesConfig",
    "UnusualWhalesError",
    "UnusualWhalesFlowSource",
    "UnusualWhalesRateLimitError",
    "UnusualWhalesTransientError",
]
